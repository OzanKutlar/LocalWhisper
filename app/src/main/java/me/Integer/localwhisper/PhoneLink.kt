package me.Integer.localwhisper

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.net.ConnectivityManager
import android.net.Network
import android.os.Build
import android.os.PowerManager
import android.util.Log
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import org.json.JSONObject
import java.net.URLEncoder
import java.util.concurrent.TimeUnit

enum class PhoneLinkState {
    DISCONNECTED,
    CONNECTING,
    CONNECTED
}

/**
 * Pure protocol helpers for the /ws/phone link (unit-tested in PhoneLinkProtocolTest).
 */
object PhoneLinkProtocol {

    fun buildWsUrl(baseUrl: String, deviceName: String): String? {
        val clean = baseUrl.trim().trimEnd('/')
        if (clean.isEmpty()) return null
        val wsBase = when {
            clean.startsWith("http://") -> "ws://" + clean.removePrefix("http://")
            clean.startsWith("https://") -> "wss://" + clean.removePrefix("https://")
            clean.startsWith("ws://") || clean.startsWith("wss://") -> clean
            else -> "ws://$clean"
        }
        val safeName = deviceName.trim().ifEmpty { "Phone" }.take(48)
        val encoded = try {
            URLEncoder.encode(safeName, "UTF-8")
        } catch (_: Exception) {
            "Phone"
        }
        return "$wsBase/ws/phone?name=$encoded"
    }

    fun buildResultFrame(requestId: String, payload: JSONObject): String {
        val ok = payload.optBoolean("ok", true)
        val error = if (!ok) payload.optString("error", payload.optString("reason", "failed")) else ""
        return JSONObject().apply {
            put("type", "result")
            put("id", requestId)
            put("ok", ok)
            put("result", payload)
            if (error.isNotEmpty()) put("error", error)
        }.toString()
    }

    fun buildEventFrame(event: String, data: JSONObject): String {
        return JSONObject().apply {
            put("type", "event")
            put("event", event)
            put("data", data)
        }.toString()
    }
}

/**
 * Low-battery event-driven WebSocket link to /ws/phone.
 * Holds no wake locks and sends no client-side keep-alive pings.
 */
object PhoneLink {

    private const val TAG = "LW-PhoneLink"
    private val BACKOFF_DELAYS_MS = longArrayOf(2_000L, 5_000L, 12_000L, 25_000L, 45_000L)

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private val httpClient = OkHttpClient.Builder()
        .connectTimeout(6, TimeUnit.SECONDS)
        .readTimeout(0, TimeUnit.MILLISECONDS)
        .build()

    private val _state = MutableStateFlow(PhoneLinkState.DISCONNECTED)
    val state: StateFlow<PhoneLinkState> = _state.asStateFlow()

    @Volatile
    private var appContext: Context? = null

    @Volatile
    private var activeSocket: WebSocket? = null

    @Volatile
    private var watchersRegistered = false

    private var reconnectJob: Job? = null
    private var failureCount = 0

    fun ensureStarted(context: Context) {
        val ctx = context.applicationContext
        appContext = ctx
        registerSystemWatchersIfNeeded(ctx)
        if (_state.value == PhoneLinkState.DISCONNECTED) {
            connect(ctx)
        }
    }

    fun reconnectNow(context: Context) {
        val ctx = context.applicationContext
        appContext = ctx
        registerSystemWatchersIfNeeded(ctx)
        failureCount = 0
        reconnectJob?.cancel()
        activeSocket?.cancel()
        activeSocket = null
        _state.value = PhoneLinkState.DISCONNECTED
        connect(ctx)
    }

    fun sendEvent(event: String, data: JSONObject = JSONObject()): Boolean {
        val socket = activeSocket ?: return false
        if (_state.value != PhoneLinkState.CONNECTED) return false
        return try {
            socket.send(PhoneLinkProtocol.buildEventFrame(event, data))
        } catch (exc: Exception) {
            Log.w(TAG, "Could not send phone event '$event'", exc)
            false
        }
    }

    @Synchronized
    private fun connect(ctx: Context) {
        if (_state.value == PhoneLinkState.CONNECTING || _state.value == PhoneLinkState.CONNECTED) {
            return
        }
        val baseUrl = ServerPreferences(ctx).buildBaseUrl() ?: return
        val modelName = "${Build.MANUFACTURER} ${Build.MODEL}".trim().ifEmpty { "Android Phone" }
        val wsUrl = PhoneLinkProtocol.buildWsUrl(baseUrl, modelName) ?: return

        _state.value = PhoneLinkState.CONNECTING
        val request = try {
            Request.Builder().url(wsUrl).build()
        } catch (exc: IllegalArgumentException) {
            _state.value = PhoneLinkState.DISCONNECTED
            return
        }

        activeSocket = httpClient.newWebSocket(request, object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                if (webSocket !== activeSocket) return
                failureCount = 0
                _state.value = PhoneLinkState.CONNECTED
                Log.i(TAG, "Phone link connected to $wsUrl")
            }

            override fun onMessage(webSocket: WebSocket, text: String) {
                if (webSocket !== activeSocket) return
                handleIncomingFrame(ctx, webSocket, text)
            }

            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                onSocketLost(ctx, webSocket)
            }

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                onSocketLost(ctx, webSocket)
            }
        })
    }

    private fun handleIncomingFrame(ctx: Context, socket: WebSocket, rawText: String) {
        val json = try {
            JSONObject(rawText)
        } catch (_: Exception) {
            return
        }
        when (json.optString("type", "")) {
            "request" -> {
                val reqId = json.optString("id", "")
                val command = json.optString("command", "")
                val args = json.optJSONObject("arguments") ?: JSONObject()
                if (reqId.isEmpty() || command.isEmpty()) return
                scope.launch {
                    val result = PhoneRpcDispatcher.dispatch(ctx, command, args)
                    try {
                        socket.send(PhoneLinkProtocol.buildResultFrame(reqId, result))
                    } catch (exc: Exception) {
                        Log.w(TAG, "Failed to send RPC result for $command", exc)
                    }
                }
            }
            "file_offer" -> {
                scope.launch {
                    FileHandoff.downloadOfferedFile(ctx, json)
                }
            }
        }
    }

    private fun onSocketLost(ctx: Context, socket: WebSocket) {
        synchronized(this) {
            if (socket !== activeSocket) return
            activeSocket = null
            _state.value = PhoneLinkState.DISCONNECTED
        }
        scheduleBackoffIfInteractive(ctx)
    }

    /**
     * Retries only while the phone screen is on, up to a small bounded number of attempts.
     * When the screen is off, we wait for ACTION_SCREEN_ON or network availability to save battery.
     */
    private fun scheduleBackoffIfInteractive(ctx: Context) {
        val pm = ctx.getSystemService(PowerManager::class.java)
        val interactive = pm?.isInteractive ?: true
        if (!interactive || failureCount >= BACKOFF_DELAYS_MS.size) {
            return
        }
        val delayMs = BACKOFF_DELAYS_MS[failureCount.coerceIn(0, BACKOFF_DELAYS_MS.lastIndex)]
        failureCount++
        reconnectJob?.cancel()
        reconnectJob = scope.launch {
            delay(delayMs)
            if (_state.value == PhoneLinkState.DISCONNECTED) {
                connect(ctx)
            }
        }
    }

    @Synchronized
    private fun registerSystemWatchersIfNeeded(ctx: Context) {
        if (watchersRegistered) return
        watchersRegistered = true

        try {
            val cm = ctx.getSystemService(ConnectivityManager::class.java)
            cm?.registerDefaultNetworkCallback(object : ConnectivityManager.NetworkCallback() {
                override fun onAvailable(network: Network) {
                    failureCount = 0
                    if (_state.value == PhoneLinkState.DISCONNECTED) {
                        connect(ctx)
                    }
                }
            })
        } catch (exc: Exception) {
            Log.w(TAG, "Could not register network callback", exc)
        }

        try {
            val filter = IntentFilter(Intent.ACTION_SCREEN_ON)
            ctx.registerReceiver(object : BroadcastReceiver() {
                override fun onReceive(context: Context?, intent: Intent?) {
                    failureCount = 0
                    if (_state.value == PhoneLinkState.DISCONNECTED) {
                        connect(ctx)
                    }
                }
            }, filter)
        } catch (exc: Exception) {
            Log.w(TAG, "Could not register screen-on receiver", exc)
        }
    }
}
