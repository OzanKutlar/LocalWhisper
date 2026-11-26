package me.Integer.localwhisper

import org.json.JSONArray
import org.json.JSONObject
import java.util.Locale

data class StoredPhoneNotification(
    val id: String,
    val sbnKey: String,
    val packageName: String,
    val appName: String,
    val title: String,
    val text: String,
    val messages: List<String>,
    val postedAtMs: Long,
    val canReply: Boolean
) {
    fun toJson(nowMs: Long = System.currentTimeMillis()): JSONObject {
        val ageSeconds = ((nowMs - postedAtMs).coerceAtLeast(0L)) / 1000L
        val msgArray = JSONArray()
        for (msg in messages) {
            msgArray.put(msg)
        }
        return JSONObject().apply {
            put("id", id)
            put("package_name", packageName)
            put("app_name", appName)
            put("title", title)
            put("text", text)
            if (messages.isNotEmpty()) {
                put("messages", msgArray)
            }
            put("can_reply", canReply)
            put("posted_at_ms", postedAtMs)
            put("age_seconds", ageSeconds)
        }
    }

    fun compactLine(nowMs: Long = System.currentTimeMillis()): String {
        val ageSec = ((nowMs - postedAtMs).coerceAtLeast(0L)) / 1000L
        val ageLabel = when {
            ageSec < 60 -> "${ageSec}s ago"
            ageSec < 3600 -> "${ageSec / 60}m ago"
            else -> "${ageSec / 3600}h ago"
        }
        val replyTag = if (canReply) " [can_reply]" else ""
        val body = if (messages.isNotEmpty()) messages.joinToString(" | ") else text
        return "$id [$appName · $ageLabel]$replyTag ${title.ifBlank { "(no title)" }}: $body"
    }
}

/**
 * Pure in-memory store for recent allowed phone notifications (n1..nN).
 * Never persists notification content to disk.
 */
object NotificationStore {

    const val MAX_NOTIFICATIONS = 100
    const val TTL_MS = 24L * 3600L * 1000L
    private const val OWN_PACKAGE = "me.Integer.localwhisper"

    val DEFAULT_ALLOWED_PACKAGES: Set<String> = linkedSetOf(
        "com.whatsapp",
        "com.whatsapp.w4b",
        "org.telegram.messenger",
        "org.thoughtcrime.securesms",
        "com.google.android.apps.messaging",
        "com.samsung.android.messaging",
        "com.Slack",
        "com.microsoft.teams",
        "com.discord",
        "com.google.android.gm",
        "com.microsoft.office.outlook"
    )

    private val lock = Any()
    private val byKey = LinkedHashMap<String, StoredPhoneNotification>()
    private var nextNumber = 1

    fun shouldAcceptPackage(packageName: String, allowedPackages: Set<String>): Boolean {
        val clean = packageName.trim()
        if (clean.isEmpty() || clean == OWN_PACKAGE || clean == "android" || clean == "com.android.systemui") {
            return false
        }
        return allowedPackages.any { it.equals(clean, ignoreCase = true) }
    }

    fun upsert(
        sbnKey: String,
        packageName: String,
        appName: String,
        title: String,
        text: String,
        messages: List<String>,
        postedAtMs: Long,
        canReply: Boolean,
        nowMs: Long = System.currentTimeMillis()
    ): StoredPhoneNotification? {
        val cleanKey = sbnKey.trim()
        val cleanTitle = title.trim()
        val cleanText = text.trim()
        val cleanMessages = messages.map { it.trim() }.filter { it.isNotEmpty() }.takeLast(6)
        if (cleanKey.isEmpty() || (cleanTitle.isEmpty() && cleanText.isEmpty() && cleanMessages.isEmpty())) {
            return null
        }
        synchronized(lock) {
            pruneExpiredLocked(nowMs)
            val existing = byKey.remove(cleanKey)
            val id = existing?.id ?: "n${nextNumber++}"
            val entry = StoredPhoneNotification(
                id = id,
                sbnKey = cleanKey,
                packageName = packageName.trim(),
                appName = appName.trim().ifEmpty { packageName.trim() },
                title = cleanTitle.take(120),
                text = cleanText.take(400),
                messages = cleanMessages.map { it.take(240) },
                postedAtMs = postedAtMs,
                canReply = canReply
            )
            byKey[cleanKey] = entry
            while (byKey.size > MAX_NOTIFICATIONS) {
                val oldestKey = byKey.keys.firstOrNull() ?: break
                byKey.remove(oldestKey)
            }
            return entry
        }
    }

    fun removeBySbnKey(sbnKey: String): StoredPhoneNotification? {
        synchronized(lock) {
            return byKey.remove(sbnKey.trim())
        }
    }

    fun getById(id: String, nowMs: Long = System.currentTimeMillis()): StoredPhoneNotification? {
        val wanted = id.trim().lowercase(Locale.US)
        if (wanted.isEmpty()) return null
        synchronized(lock) {
            pruneExpiredLocked(nowMs)
            return byKey.values.firstOrNull { it.id == wanted }
        }
    }

    fun list(
        appQuery: String = "",
        search: String = "",
        sinceMinutes: Int = 0,
        nowMs: Long = System.currentTimeMillis()
    ): List<StoredPhoneNotification> {
        val cleanApp = appQuery.trim().lowercase(Locale.US)
        val cleanSearch = search.trim().lowercase(Locale.US)
        val cutoffMs = if (sinceMinutes > 0) nowMs - sinceMinutes.toLong() * 60_000L else 0L
        synchronized(lock) {
            pruneExpiredLocked(nowMs)
            return byKey.values
                .filter { entry ->
                    (cutoffMs <= 0L || entry.postedAtMs >= cutoffMs) &&
                        (cleanApp.isEmpty() ||
                            entry.appName.lowercase(Locale.US).contains(cleanApp) ||
                            entry.packageName.lowercase(Locale.US).contains(cleanApp)) &&
                        (cleanSearch.isEmpty() ||
                            entry.title.lowercase(Locale.US).contains(cleanSearch) ||
                            entry.text.lowercase(Locale.US).contains(cleanSearch) ||
                            entry.messages.any { it.lowercase(Locale.US).contains(cleanSearch) })
                }
                .sortedByDescending { it.postedAtMs }
        }
    }

    fun listJson(
        appQuery: String = "",
        search: String = "",
        sinceMinutes: Int = 0,
        nowMs: Long = System.currentTimeMillis()
    ): JSONObject {
        val matched = list(appQuery, search, sinceMinutes, nowMs)
        val arr = JSONArray()
        val summaryLines = ArrayList<String>(matched.size)
        for (item in matched) {
            arr.put(item.toJson(nowMs))
            summaryLines.add(item.compactLine(nowMs))
        }
        return JSONObject().apply {
            put("ok", true)
            put("count", matched.size)
            put("notifications", arr)
            put(
                "summary",
                if (summaryLines.isEmpty()) "(no matching phone notifications)"
                else summaryLines.joinToString("\n")
            )
        }
    }

    fun clear() {
        synchronized(lock) {
            byKey.clear()
            nextNumber = 1
        }
    }

    private fun pruneExpiredLocked(nowMs: Long) {
        val iter = byKey.entries.iterator()
        while (iter.hasNext()) {
            val entry = iter.next().value
            if (nowMs - entry.postedAtMs > TTL_MS) {
                iter.remove()
            }
        }
    }
}
