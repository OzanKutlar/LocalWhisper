package me.Integer.localwhisper

import android.content.Context
import android.content.SharedPreferences
import org.json.JSONArray
import org.json.JSONObject
import java.util.UUID

data class AgentChatTurn(
    val userPrompt: String,
    val thinking: String,
    val toolCalls: List<AgentToolCallInfo>,
    val reply: String
)

data class SavedAgentSession(
    val sessionId: String,
    val title: String,
    val updatedAtMs: Long,
    val turns: List<AgentChatTurn>
)

/**
 * Persists multi-turn Agent chat sessions on the device so conversation history
 * survives after the assistant overlay closes.
 */
class AgentSessionStore(context: Context) {

    private val prefs: SharedPreferences =
        context.applicationContext.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)

    var activeSessionId: String?
        get() = prefs.getString(KEY_ACTIVE_SESSION_ID, null)?.takeIf { it.isNotBlank() }
        set(value) {
            prefs.edit().putString(KEY_ACTIVE_SESSION_ID, value?.trim().orEmpty()).apply()
        }

    fun recordOverlayClosed() {
        prefs.edit().putLong(KEY_LAST_CLOSED_AT_MS, System.currentTimeMillis()).apply()
    }

    /**
     * Returns the previous active session ID if the overlay was reopened within
     * [resumeTimeoutSeconds] of closing; otherwise selects a new chat session (null).
     */
    fun resolveInitialSessionId(resumeTimeoutSeconds: Int): String? {
        val currentId = activeSessionId ?: return null
        if (getSession(currentId) == null) {
            activeSessionId = null
            return null
        }
        if (resumeTimeoutSeconds <= 0) {
            activeSessionId = null
            return null
        }
        val lastClosedAt = prefs.getLong(KEY_LAST_CLOSED_AT_MS, 0L)
        if (lastClosedAt <= 0L) {
            activeSessionId = null
            return null
        }
        val elapsedMs = System.currentTimeMillis() - lastClosedAt
        return if (elapsedMs in 0L..(resumeTimeoutSeconds * 1000L)) {
            currentId
        } else {
            activeSessionId = null
            null
        }
    }

    fun listSessions(): List<SavedAgentSession> {
        val raw = prefs.getString(KEY_SESSIONS_JSON, null) ?: return emptyList()
        return try {
            val array = JSONArray(raw)
            val sessions = ArrayList<SavedAgentSession>(array.length())
            for (i in 0 until array.length()) {
                val obj = array.optJSONObject(i) ?: continue
                parseSession(obj)?.let { sessions.add(it) }
            }
            sessions.sortedByDescending { it.updatedAtMs }
        } catch (_: Exception) {
            emptyList()
        }
    }

    fun getSession(sessionId: String?): SavedAgentSession? {
        if (sessionId.isNullOrBlank()) return null
        return listSessions().firstOrNull { it.sessionId == sessionId }
    }

    fun startNewSession(): String {
        val newId = UUID.randomUUID().toString().replace("-", "").take(12)
        activeSessionId = null
        return newId
    }

    fun recordTurn(sessionId: String, turn: AgentChatTurn): SavedAgentSession {
        val cleanId = sessionId.ifBlank { startNewSession() }
        val all = listSessions().toMutableList()
        val existingIndex = all.indexOfFirst { it.sessionId == cleanId }
        val now = System.currentTimeMillis()

        val updated = if (existingIndex >= 0) {
            val current = all.removeAt(existingIndex)
            val newTurns = (current.turns + turn).takeLast(MAX_TURNS_PER_SESSION)
            current.copy(updatedAtMs = now, turns = newTurns)
        } else {
            SavedAgentSession(
                sessionId = cleanId,
                title = buildTitle(turn.userPrompt),
                updatedAtMs = now,
                turns = listOf(turn)
            )
        }

        all.add(0, updated)
        saveSessions(all.take(MAX_SAVED_SESSIONS))
        activeSessionId = cleanId
        return updated
    }

    fun updateLastTurn(sessionId: String, updatedTurn: AgentChatTurn): SavedAgentSession {
        val all = listSessions().toMutableList()
        val existingIndex = all.indexOfFirst { it.sessionId == sessionId }
        if (existingIndex < 0) {
            return recordTurn(sessionId, updatedTurn)
        }
        val current = all.removeAt(existingIndex)
        val mutableTurns = current.turns.toMutableList()
        if (mutableTurns.isNotEmpty()) {
            val prev = mutableTurns.last()
            mutableTurns[mutableTurns.lastIndex] = updatedTurn.copy(
                userPrompt = updatedTurn.userPrompt.ifBlank { prev.userPrompt },
                thinking = listOf(prev.thinking, updatedTurn.thinking)
                    .filter { it.isNotBlank() }
                    .joinToString("\n\n")
            )
        } else {
            mutableTurns.add(updatedTurn)
        }
        val updated = current.copy(
            updatedAtMs = System.currentTimeMillis(),
            turns = mutableTurns
        )
        all.add(0, updated)
        saveSessions(all.take(MAX_SAVED_SESSIONS))
        activeSessionId = sessionId
        return updated
    }

    fun clearAll() {
        prefs.edit()
            .remove(KEY_SESSIONS_JSON)
            .remove(KEY_ACTIVE_SESSION_ID)
            .remove(KEY_LAST_CLOSED_AT_MS)
            .apply()
    }

    private fun buildTitle(firstPrompt: String): String {
        val oneLine = firstPrompt.trim().replace(Regex("\\s+"), " ")
        if (oneLine.isEmpty()) return "Agent chat"
        return if (oneLine.length <= MAX_TITLE_CHARS) {
            oneLine
        } else {
            oneLine.take(MAX_TITLE_CHARS).trimEnd() + "…"
        }
    }

    private fun saveSessions(sessions: List<SavedAgentSession>) {
        val array = JSONArray()
        for (session in sessions) {
            array.put(serializeSession(session))
        }
        prefs.edit().putString(KEY_SESSIONS_JSON, array.toString()).apply()
    }

    private fun serializeSession(session: SavedAgentSession): JSONObject {
        val turnsArray = JSONArray()
        for (turn in session.turns) {
            val toolsArray = JSONArray()
            for (tool in turn.toolCalls) {
                toolsArray.put(
                    JSONObject().apply {
                        put("tool", tool.tool)
                        put("status", tool.status)
                        put("summary", tool.summary)
                    }
                )
            }
            turnsArray.put(
                JSONObject().apply {
                    put("user_prompt", turn.userPrompt)
                    put("thinking", turn.thinking)
                    put("reply", turn.reply)
                    put("tool_calls", toolsArray)
                }
            )
        }
        return JSONObject().apply {
            put("session_id", session.sessionId)
            put("title", session.title)
            put("updated_at_ms", session.updatedAtMs)
            put("turns", turnsArray)
        }
    }

    private fun parseSession(obj: JSONObject): SavedAgentSession? {
        val id = obj.optString("session_id", "").trim()
        if (id.isEmpty()) return null
        val title = obj.optString("title", "Agent chat")
        val updatedAt = obj.optLong("updated_at_ms", 0L)
        val turnsArray = obj.optJSONArray("turns") ?: JSONArray()
        val turns = ArrayList<AgentChatTurn>(turnsArray.length())
        for (i in 0 until turnsArray.length()) {
            val tObj = turnsArray.optJSONObject(i) ?: continue
            val toolsArray = tObj.optJSONArray("tool_calls")
            val tools = ArrayList<AgentToolCallInfo>(toolsArray?.length() ?: 0)
            if (toolsArray != null) {
                for (j in 0 until toolsArray.length()) {
                    val toolObj = toolsArray.optJSONObject(j) ?: continue
                    tools.add(
                        AgentToolCallInfo(
                            tool = toolObj.optString("tool", ""),
                            status = toolObj.optString("status", "ok"),
                            summary = toolObj.optString("summary", "")
                        )
                    )
                }
            }
            turns.add(
                AgentChatTurn(
                    userPrompt = tObj.optString("user_prompt", ""),
                    thinking = tObj.optString("thinking", ""),
                    toolCalls = tools,
                    reply = tObj.optString("reply", "")
                )
            )
        }
        if (turns.isEmpty()) return null
        return SavedAgentSession(
            sessionId = id,
            title = title,
            updatedAtMs = updatedAt,
            turns = turns
        )
    }

    companion object {
        private const val PREFS_NAME = "localwhisper_agent_sessions"
        private const val KEY_SESSIONS_JSON = "sessions_json"
        private const val KEY_ACTIVE_SESSION_ID = "active_session_id"
        private const val KEY_LAST_CLOSED_AT_MS = "last_overlay_closed_at_ms"
        private const val MAX_SAVED_SESSIONS = 20
        private const val MAX_TURNS_PER_SESSION = 30
        private const val MAX_TITLE_CHARS = 34
    }
}
