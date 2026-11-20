package me.Integer.localwhisper

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.MultipartBody
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.asRequestBody
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.io.IOException
import java.util.concurrent.TimeUnit

data class PcClientInfo(
    val id: String,
    val name: String
) {
    override fun toString(): String = name
}

data class HealthResponse(
    val status: String,
    val model: String,
    val agentModel: String,
    val device: String,
    val gpuName: String,
    val clients: List<PcClientInfo>
)

data class TranscribeResponse(
    val text: String,
    val language: String,
    val durationMs: Int
)

data class SendResponse(
    val status: String,
    val clientId: String,
    val clientName: String
)

data class AgentToolCallInfo(
    val tool: String,
    val status: String,
    val summary: String
)

data class AndroidClientAction(
    val intentType: String,
    val label: String,
    val hour: Int,
    val minute: Int,
    val seconds: Int,
    val urlOrQuery: String
)

data class PendingConfirmationInfo(
    val confirmationId: String,
    val toolName: String,
    val description: String
)

data class AgentResponse(
    val status: String,
    val sessionId: String,
    val thinking: String,
    val reply: String,
    val toolCalls: List<AgentToolCallInfo>,
    val androidActions: List<AndroidClientAction>,
    val pendingConfirmation: PendingConfirmationInfo?
)

class LocalWhisperApi {

    private val httpClient: OkHttpClient = OkHttpClient.Builder()
        .connectTimeout(5, TimeUnit.SECONDS)
        .writeTimeout(30, TimeUnit.SECONDS)
        .readTimeout(90, TimeUnit.SECONDS)
        .build()

    suspend fun checkHealth(baseUrl: String): Result<HealthResponse> = withContext(Dispatchers.IO) {
        val request = Request.Builder()
            .url("$baseUrl/api/health")
            .get()
            .build()
        executeJsonRequest(request) { json ->
            HealthResponse(
                status = json.optString("status", "unknown"),
                model = json.optString("model", "unknown"),
                agentModel = json.optString("agent_model", "qwen3:8b"),
                device = json.optString("device", "unknown"),
                gpuName = json.optString("gpu_name", "unknown"),
                clients = parseClients(json.optJSONArray("connected_clients"))
            )
        }
    }

    suspend fun fetchClients(baseUrl: String): Result<List<PcClientInfo>> = withContext(Dispatchers.IO) {
        val request = Request.Builder()
            .url("$baseUrl/api/clients")
            .get()
            .build()
        executeJsonRequest(request) { json ->
            parseClients(json.optJSONArray("clients"))
        }
    }

    suspend fun transcribeAudio(
        baseUrl: String,
        wavFile: File,
        language: String?
    ): Result<TranscribeResponse> = withContext(Dispatchers.IO) {
        if (!wavFile.isFile || wavFile.length() <= 44L) {
            return@withContext Result.failure(IllegalArgumentException("Recorded audio is empty"))
        }

        val multipartBuilder = MultipartBody.Builder()
            .setType(MultipartBody.FORM)
            .addFormDataPart(
                "audio",
                wavFile.name,
                wavFile.asRequestBody(AUDIO_WAV_MEDIA_TYPE)
            )

        val trimmedLanguage = language?.trim().orEmpty()
        if (trimmedLanguage.isNotEmpty()) {
            multipartBuilder.addFormDataPart("language", trimmedLanguage)
        }

        val request = Request.Builder()
            .url("$baseUrl/api/transcribe")
            .post(multipartBuilder.build())
            .build()

        executeJsonRequest(request) { json ->
            TranscribeResponse(
                text = json.optString("text", ""),
                language = json.optString("language", ""),
                durationMs = json.optInt("duration_ms", 0)
            )
        }
    }

    suspend fun sendKeystrokes(
        baseUrl: String,
        text: String,
        clientId: String?
    ): Result<SendResponse> = withContext(Dispatchers.IO) {
        if (text.isEmpty()) {
            return@withContext Result.failure(IllegalArgumentException("Text must not be empty"))
        }

        val payload = JSONObject().apply {
            put("text", text)
            if (!clientId.isNullOrBlank()) {
                put("client_id", clientId)
            }
        }

        val request = Request.Builder()
            .url("$baseUrl/api/send")
            .post(payload.toString().toRequestBody(JSON_MEDIA_TYPE))
            .build()

        executeJsonRequest(request) { json ->
            SendResponse(
                status = json.optString("status", "sent"),
                clientId = json.optString("client_id", ""),
                clientName = json.optString("client_name", "PC")
            )
        }
    }

    suspend fun runAgent(
        baseUrl: String,
        prompt: String,
        clientId: String?,
        sessionId: String?,
        priorTurns: List<AgentChatTurn> = emptyList(),
        phoneScreen: JSONObject? = null
    ): Result<AgentResponse> = withContext(Dispatchers.IO) {
        if (prompt.isBlank()) {
            return@withContext Result.failure(IllegalArgumentException("Prompt must not be empty"))
        }
        val payload = JSONObject().apply {
            put("prompt", prompt)
            if (!clientId.isNullOrBlank()) {
                put("client_id", clientId)
            }
            if (!sessionId.isNullOrBlank()) {
                put("session_id", sessionId)
            }
            if (priorTurns.isNotEmpty()) {
                val historyArray = JSONArray()
                for (turn in priorTurns) {
                    if (turn.userPrompt.isNotBlank()) {
                        historyArray.put(
                            JSONObject().apply {
                                put("role", "user")
                                put("content", turn.userPrompt)
                            }
                        )
                    }
                    if (turn.reply.isNotBlank()) {
                        historyArray.put(
                            JSONObject().apply {
                                put("role", "assistant")
                                put("content", turn.reply)
                            }
                        )
                    }
                }
                put("history", historyArray)
            }
            if (phoneScreen != null) {
                put("phone_screen", phoneScreen)
            }
        }
        val request = Request.Builder()
            .url("$baseUrl/api/agent")
            .post(payload.toString().toRequestBody(JSON_MEDIA_TYPE))
            .build()
        executeJsonRequest(request, ::parseAgentResponse)
    }

    suspend fun confirmAgentAction(
        baseUrl: String,
        sessionId: String,
        confirmationId: String,
        approved: Boolean,
        clientId: String?
    ): Result<AgentResponse> = withContext(Dispatchers.IO) {
        val payload = JSONObject().apply {
            put("session_id", sessionId)
            put("confirmation_id", confirmationId)
            put("approved", approved)
            if (!clientId.isNullOrBlank()) {
                put("client_id", clientId)
            }
        }
        val request = Request.Builder()
            .url("$baseUrl/api/agent/confirm")
            .post(payload.toString().toRequestBody(JSON_MEDIA_TYPE))
            .build()
        executeJsonRequest(request, ::parseAgentResponse)
    }

    private fun parseAgentResponse(json: JSONObject): AgentResponse {
        val toolArray = json.optJSONArray("tool_calls")
        val tools = ArrayList<AgentToolCallInfo>(toolArray?.length() ?: 0)
        if (toolArray != null) {
            for (i in 0 until toolArray.length()) {
                val item = toolArray.optJSONObject(i) ?: continue
                tools.add(
                    AgentToolCallInfo(
                        tool = item.optString("tool", ""),
                        status = item.optString("status", "ok"),
                        summary = item.optString("summary", item.optString("tool", "tool"))
                    )
                )
            }
        }

        val androidArray = json.optJSONArray("android_actions")
        val actions = ArrayList<AndroidClientAction>(androidArray?.length() ?: 0)
        if (androidArray != null) {
            for (i in 0 until androidArray.length()) {
                val item = androidArray.optJSONObject(i) ?: continue
                actions.add(
                    AndroidClientAction(
                        intentType = item.optString("intent_type", ""),
                        label = item.optString("label", ""),
                        hour = item.optInt("hour", -1),
                        minute = item.optInt("minute", -1),
                        seconds = item.optInt("seconds", 0),
                        urlOrQuery = item.optString("url_or_query", "")
                    )
                )
            }
        }

        val pendingObj = json.optJSONObject("pending_confirmation")
        val pending = if (pendingObj != null) {
            val confId = pendingObj.optString("confirmation_id", "")
            if (confId.isNotEmpty()) {
                PendingConfirmationInfo(
                    confirmationId = confId,
                    toolName = pendingObj.optString("tool_name", ""),
                    description = pendingObj.optString("description", "")
                )
            } else null
        } else null

        return AgentResponse(
            status = json.optString("status", "completed"),
            sessionId = json.optString("session_id", ""),
            thinking = json.optString("thinking", ""),
            reply = json.optString("reply", ""),
            toolCalls = tools,
            androidActions = actions,
            pendingConfirmation = pending
        )
    }

    private fun <T> executeJsonRequest(
        request: Request,
        parser: (JSONObject) -> T
    ): Result<T> {
        return try {
            httpClient.newCall(request).execute().use { response ->
                val bodyText = response.body?.string().orEmpty()
                if (!response.isSuccessful) {
                    val detail = extractErrorDetail(bodyText, response.code)
                    return Result.failure(IOException(detail))
                }
                val json = JSONObject(bodyText)
                Result.success(parser(json))
            }
        } catch (exc: Exception) {
            Result.failure(exc)
        }
    }

    private fun parseClients(array: JSONArray?): List<PcClientInfo> {
        if (array == null) return emptyList()
        val result = ArrayList<PcClientInfo>(array.length())
        for (i in 0 until array.length()) {
            val item = array.optJSONObject(i) ?: continue
            val id = item.optString("id", "")
            val name = item.optString("name", "PC")
            if (id.isNotEmpty()) {
                result.add(PcClientInfo(id = id, name = name))
            }
        }
        return result
    }

    private fun extractErrorDetail(bodyText: String, statusCode: Int): String {
        return try {
            val detail = JSONObject(bodyText).optString("detail", "")
            if (detail.isNotEmpty()) detail else "Server error (HTTP $statusCode)"
        } catch (_: Exception) {
            "Server error (HTTP $statusCode)"
        }
    }

    companion object {
        private val JSON_MEDIA_TYPE = "application/json; charset=utf-8".toMediaType()
        private val AUDIO_WAV_MEDIA_TYPE = "audio/wav".toMediaType()
    }
}
