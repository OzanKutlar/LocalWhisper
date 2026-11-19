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

class LocalWhisperApi {

    private val httpClient: OkHttpClient = OkHttpClient.Builder()
        .connectTimeout(5, TimeUnit.SECONDS)
        .writeTimeout(30, TimeUnit.SECONDS)
        .readTimeout(60, TimeUnit.SECONDS)
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
