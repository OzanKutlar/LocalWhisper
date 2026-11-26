package me.Integer.localwhisper

import android.content.Context
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import android.os.SystemClock
import android.util.Log
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.async
import kotlinx.coroutines.awaitAll
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.sync.Semaphore
import kotlinx.coroutines.sync.withPermit
import kotlinx.coroutines.withContext
import okhttp3.OkHttpClient
import okhttp3.Request
import org.json.JSONObject
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.Inet4Address
import java.net.InetAddress
import java.net.SocketTimeoutException
import java.util.concurrent.TimeUnit

/** A LocalWhisper server that answered on the local network. */
data class DiscoveredServer(
    val host: String,
    val port: Int,
    val name: String,
    val asrBackend: String
)

/** Parses discovery replies and /api/health answers. Pure, so it can be unit tested. */
object DiscoveryReplyParser {

    const val SERVICE_NAME = "localwhisper"
    const val MAX_REPLY_BYTES = 2048
    private const val MAX_HEALTH_CHARS = 64_000
    private const val MAX_NAME_CHARS = 64
    private val WHITESPACE = Regex("\\s+")

    fun parse(data: ByteArray, length: Int, senderIp: String): DiscoveredServer? {
        if (length <= 0 || length > data.size || length > MAX_REPLY_BYTES || senderIp.isBlank()) return null
        val json = parseJson(String(data, 0, length, Charsets.UTF_8)) ?: return null
        if (json.optString("service", "") != SERVICE_NAME) return null
        val port = json.optInt("port", -1)
        if (port !in 1..65535) return null
        return DiscoveredServer(
            host = senderIp,
            port = port,
            name = cleanName(json.optString("name", ""), senderIp),
            asrBackend = json.optString("asr_backend", "")
        )
    }

    /** Accepts only /api/health bodies that look like a LocalWhisper server. */
    fun parseHealth(body: String, host: String, port: Int): DiscoveredServer? {
        if (host.isBlank() || port !in 1..65535 || body.length > MAX_HEALTH_CHARS) return null
        val json = parseJson(body) ?: return null
        if (json.optString("status", "") != "ok" || !json.has("connected_clients")) return null
        return DiscoveredServer(
            host = host,
            port = port,
            name = cleanName(json.optString("server_name", ""), host),
            asrBackend = json.optString("asr_backend", "")
        )
    }

    private fun parseJson(text: String): JSONObject? {
        return try {
            JSONObject(text)
        } catch (_: Exception) {
            null
        }
    }

    private fun cleanName(raw: String, fallback: String): String {
        val clean = raw.trim().replace(WHITESPACE, " ").take(MAX_NAME_CHARS)
        return clean.ifEmpty { fallback }
    }
}

/** IPv4 arithmetic for discovery. Pure, so it can be unit tested. */
object SubnetMath {

    private const val SWEEP_HOST_MIN = 1
    private const val SWEEP_HOST_MAX = 254

    /** Directed broadcast address of [address]/[prefixLength], or null for invalid input. */
    fun broadcastAddress(address: ByteArray, prefixLength: Int): ByteArray? {
        if (address.size != 4 || prefixLength !in 1..30) return null
        val mask = -1 shl (32 - prefixLength)
        return fromInt(toInt(address) or mask.inv())
    }

    /** Other hosts in the /24 around [address]; bigger subnets are limited to that /24 to bound the sweep. */
    fun sweepHosts(address: ByteArray): List<String> {
        if (address.size != 4) return emptyList()
        val prefix = "${u(address[0])}.${u(address[1])}.${u(address[2])}."
        val own = u(address[3])
        return (SWEEP_HOST_MIN..SWEEP_HOST_MAX).filter { it != own }.map { prefix + it }
    }

    fun toInt(address: ByteArray): Int =
        (u(address[0]) shl 24) or (u(address[1]) shl 16) or (u(address[2]) shl 8) or u(address[3])

    fun fromInt(value: Int): ByteArray = byteArrayOf(
        (value ushr 24).toByte(),
        (value ushr 16).toByte(),
        (value ushr 8).toByte(),
        value.toByte()
    )

    private fun u(value: Byte): Int = value.toInt() and 0xFF
}

/**
 * Finds LocalWhisper servers on the Wi-Fi network: a UDP broadcast first, then a bounded
 * HTTP sweep of /api/health across the /24 when nothing answers (for example UDP blocked).
 */
object ServerDiscovery {

    const val DISCOVERY_PORT = 8766
    const val DEFAULT_TIMEOUT_MS = 2_000L
    private const val TAG = "LW-Discovery"
    private const val MAGIC = "LOCALWHISPER_DISCOVER/1"
    private const val GLOBAL_BROADCAST = "255.255.255.255"
    private const val SEND_ROUNDS = 2
    private const val MAX_RECEIVED_PACKETS = 64
    private const val MIN_TIMEOUT_MS = 200L
    private const val MAX_TIMEOUT_MS = 10_000L
    private const val SWEEP_CONCURRENCY = 32
    private const val SWEEP_CONNECT_TIMEOUT_MS = 400L
    private const val SWEEP_CALL_TIMEOUT_MS = 1_200L

    private class LocalIpv4(val address: ByteArray, val prefixLength: Int)

    suspend fun discover(
        context: Context,
        httpPort: Int,
        timeoutMs: Long = DEFAULT_TIMEOUT_MS
    ): List<DiscoveredServer> = withContext(Dispatchers.IO) {
        val local = findLocalIpv4(context.applicationContext)
        val viaBroadcast = broadcastDiscover(local, timeoutMs.coerceIn(MIN_TIMEOUT_MS, MAX_TIMEOUT_MS))
        if (viaBroadcast.isNotEmpty() || local == null) {
            return@withContext viaBroadcast
        }
        Log.i(TAG, "No UDP reply; sweeping the local subnet over HTTP")
        sweepSubnet(local, httpPort)
    }

    /** IPv4 address of the active Wi-Fi or Ethernet network; null on mobile data or when unknown. */
    private fun findLocalIpv4(context: Context): LocalIpv4? {
        val cm = context.getSystemService(ConnectivityManager::class.java) ?: return null
        return try {
            val network = cm.activeNetwork ?: return null
            val caps = cm.getNetworkCapabilities(network)
            val onLan = caps != null && (
                caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) ||
                    caps.hasTransport(NetworkCapabilities.TRANSPORT_ETHERNET)
                )
            if (!onLan) return null
            val links = cm.getLinkProperties(network) ?: return null
            links.linkAddresses.firstNotNullOfOrNull { link ->
                val addr = link.address
                if (addr is Inet4Address && !addr.isLoopbackAddress && !addr.isLinkLocalAddress) {
                    LocalIpv4(addr.address, link.prefixLength)
                } else {
                    null
                }
            }
        } catch (exc: Exception) {
            Log.w(TAG, "Could not read the phone's network address", exc)
            null
        }
    }

    private fun broadcastDiscover(local: LocalIpv4?, timeoutMs: Long): List<DiscoveredServer> {
        val targets = broadcastTargets(local)
        if (targets.isEmpty()) return emptyList()
        val found = LinkedHashMap<String, DiscoveredServer>()
        try {
            DatagramSocket().use { socket ->
                socket.broadcast = true
                sendProbes(socket, targets)
                receiveReplies(socket, timeoutMs, found)
            }
        } catch (exc: Exception) {
            Log.w(TAG, "UDP discovery failed", exc)
        }
        return found.values.toList()
    }

    private fun broadcastTargets(local: LocalIpv4?): List<InetAddress> {
        val targets = ArrayList<InetAddress>(2)
        try {
            targets.add(InetAddress.getByName(GLOBAL_BROADCAST))
            val subnet = local?.let { SubnetMath.broadcastAddress(it.address, it.prefixLength) }
            if (subnet != null) {
                targets.add(InetAddress.getByAddress(subnet))
            }
        } catch (exc: Exception) {
            Log.w(TAG, "Could not build broadcast addresses", exc)
        }
        return targets
    }

    private fun sendProbes(socket: DatagramSocket, targets: List<InetAddress>) {
        val payload = MAGIC.toByteArray(Charsets.US_ASCII)
        for (round in 0 until SEND_ROUNDS) {
            for (target in targets) {
                try {
                    socket.send(DatagramPacket(payload, payload.size, target, DISCOVERY_PORT))
                } catch (exc: Exception) {
                    Log.d(TAG, "Discovery probe to ${target.hostAddress} failed (round $round)", exc)
                }
            }
        }
    }

    private fun receiveReplies(
        socket: DatagramSocket,
        timeoutMs: Long,
        found: MutableMap<String, DiscoveredServer>
    ) {
        val deadline = SystemClock.elapsedRealtime() + timeoutMs
        val buffer = ByteArray(DiscoveryReplyParser.MAX_REPLY_BYTES)
        for (index in 0 until MAX_RECEIVED_PACKETS) {
            val remaining = deadline - SystemClock.elapsedRealtime()
            if (remaining <= 0L) return
            socket.soTimeout = remaining.toInt().coerceAtLeast(1)
            val packet = DatagramPacket(buffer, buffer.size)
            try {
                socket.receive(packet)
            } catch (_: SocketTimeoutException) {
                return
            }
            val host = packet.address?.hostAddress ?: continue
            val server = DiscoveryReplyParser.parse(packet.data, packet.length, host) ?: continue
            if (!found.containsKey(server.host)) {
                found[server.host] = server
            }
        }
    }

    private suspend fun sweepSubnet(local: LocalIpv4, httpPort: Int): List<DiscoveredServer> {
        val port = if (httpPort in 1..65535) httpPort else ServerPreferences.DEFAULT_PORT
        val client = OkHttpClient.Builder()
            .connectTimeout(SWEEP_CONNECT_TIMEOUT_MS, TimeUnit.MILLISECONDS)
            .readTimeout(SWEEP_CALL_TIMEOUT_MS, TimeUnit.MILLISECONDS)
            .callTimeout(SWEEP_CALL_TIMEOUT_MS, TimeUnit.MILLISECONDS)
            .retryOnConnectionFailure(false)
            .build()
        val gate = Semaphore(SWEEP_CONCURRENCY)
        return try {
            coroutineScope {
                SubnetMath.sweepHosts(local.address)
                    .map { host -> async { gate.withPermit { probeHealth(client, host, port) } } }
                    .awaitAll()
                    .filterNotNull()
            }
        } finally {
            client.dispatcher.executorService.shutdown()
            client.connectionPool.evictAll()
        }
    }

    private fun probeHealth(client: OkHttpClient, host: String, port: Int): DiscoveredServer? {
        val request = try {
            Request.Builder().url("http://$host:$port/api/health").get().build()
        } catch (exc: IllegalArgumentException) {
            Log.d(TAG, "Skipping invalid sweep host $host", exc)
            return null
        }
        return try {
            client.newCall(request).execute().use { response ->
                if (!response.isSuccessful) return null
                val body = response.body?.string() ?: return null
                DiscoveryReplyParser.parseHealth(body, host, port)
            }
        } catch (_: Exception) {
            // Most hosts in the sweep have no server; a refused or timed-out connection is expected.
            null
        }
    }
}
