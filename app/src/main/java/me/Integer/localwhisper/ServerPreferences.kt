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
        const val DEFAULT_HOST = "192.168.1.100"
        const val DEFAULT_PORT = 8765

        fun sanitizeHost(raw: String): String {
            return raw.trim()
                .removePrefix("http://")
                .removePrefix("https://")
                .trimEnd('/')
        }
    }
}
