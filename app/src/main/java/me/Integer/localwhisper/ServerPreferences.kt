package me.Integer.localwhisper

import android.content.Context
import android.content.SharedPreferences

class ServerPreferences(context: Context) {

    private val prefs: SharedPreferences =
        context.applicationContext.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)

    var serverHost: String
        get() = prefs.getString(KEY_SERVER_HOST, DEFAULT_HOST).orEmpty()
        set(value) = prefs.edit().putString(KEY_SERVER_HOST, sanitizeHost(value)).apply()

    var serverPort: Int
        get() = prefs.getInt(KEY_SERVER_PORT, DEFAULT_PORT)
        set(value) {
            val safePort = value.coerceIn(1, 65535)
            prefs.edit().putInt(KEY_SERVER_PORT, safePort).apply()
        }

    /** Display name of the server last picked from network discovery. */
    var serverName: String
        get() = prefs.getString(KEY_SERVER_NAME, "").orEmpty()
        set(value) = prefs.edit().putString(KEY_SERVER_NAME, value.trim().take(64)).apply()

    /** True once the user typed an address or picked a discovered server; stops automatic discovery. */
    var hasUserSetServer: Boolean
        get() = prefs.getBoolean(KEY_USER_SET_SERVER, false)
        set(value) = prefs.edit().putBoolean(KEY_USER_SET_SERVER, value).apply()

    var language: String
        get() = prefs.getString(KEY_LANGUAGE, "").orEmpty()
        set(value) = prefs.edit().putString(KEY_LANGUAGE, value.trim()).apply()

    var selectedClientId: String
        get() = prefs.getString(KEY_CLIENT_ID, "").orEmpty()
        set(value) = prefs.edit().putString(KEY_CLIENT_ID, value.trim()).apply()

    var startInKeyboardMode: Boolean
        get() = prefs.getBoolean(KEY_KEYBOARD_MODE, false)
        set(value) = prefs.edit().putBoolean(KEY_KEYBOARD_MODE, value).apply()

    var startInAgentMode: Boolean
        get() = prefs.getBoolean(KEY_AGENT_MODE, false)
        set(value) = prefs.edit().putBoolean(KEY_AGENT_MODE, value).apply()

    var agentResumeTimeoutSeconds: Int
        get() = sanitizeResumeTimeout(prefs.getInt(KEY_AGENT_RESUME_TIMEOUT_S, DEFAULT_AGENT_RESUME_TIMEOUT_S))
        set(value) {
            prefs.edit()
                .putInt(KEY_AGENT_RESUME_TIMEOUT_S, sanitizeResumeTimeout(value))
                .apply()
        }

    var mirrorNotificationsToPc: Boolean
        get() = prefs.getBoolean(KEY_MIRROR_NOTIFICATIONS, true)
        set(value) = prefs.edit().putBoolean(KEY_MIRROR_NOTIFICATIONS, value).apply()

    var allowedNotificationPackages: Set<String>
        get() {
            val stored = prefs.getStringSet(KEY_ALLOWED_NOTIF_PACKAGES, null)
            return stored?.toSet() ?: NotificationStore.DEFAULT_ALLOWED_PACKAGES
        }
        set(value) {
            prefs.edit().putStringSet(KEY_ALLOWED_NOTIF_PACKAGES, value.toSet()).apply()
        }

    /** Saves a server found by network discovery as the user's chosen server. */
    fun applyDiscoveredServer(server: DiscoveredServer) {
        serverHost = server.host
        serverPort = server.port
        serverName = server.name
        hasUserSetServer = true
    }

    fun buildBaseUrl(): String? {
        val host = sanitizeHost(serverHost)
        val port = serverPort
        if (host.isEmpty() || port !in 1..65535) {
            return null
        }
        return "http://$host:$port"
    }

    companion object {
        private const val PREFS_NAME = "localwhisper_prefs"
        private const val KEY_SERVER_HOST = "server_host"
        private const val KEY_SERVER_PORT = "server_port"
        private const val KEY_LANGUAGE = "language"
        private const val KEY_CLIENT_ID = "selected_client_id"
        private const val KEY_KEYBOARD_MODE = "start_in_keyboard_mode"
        private const val KEY_AGENT_MODE = "start_in_agent_mode"
        private const val KEY_AGENT_RESUME_TIMEOUT_S = "agent_resume_timeout_s"
        private const val KEY_SERVER_NAME = "server_name"
        private const val KEY_USER_SET_SERVER = "user_set_server"
        private const val KEY_MIRROR_NOTIFICATIONS = "mirror_notifications_to_pc"
        private const val KEY_ALLOWED_NOTIF_PACKAGES = "allowed_notification_packages"
        const val DEFAULT_HOST = "192.168.1.100"
        const val DEFAULT_PORT = 8765
        const val DEFAULT_AGENT_RESUME_TIMEOUT_S = 5
        val RESUME_TIMEOUT_OPTIONS_SECONDS = intArrayOf(
            0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55, 60
        )

        fun sanitizeResumeTimeout(rawSeconds: Int): Int {
            val clamped = rawSeconds.coerceIn(0, 60)
            return ((clamped + 2) / 5) * 5
        }

        fun sanitizeHost(raw: String): String {
            return raw.trim()
                .removePrefix("http://")
                .removePrefix("https://")
                .trimEnd('/')
        }
    }
}
