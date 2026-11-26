package me.Integer.localwhisper

import android.Manifest
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.ContentUris
import android.content.ContentValues
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Environment
import android.os.Handler
import android.os.Looper
import android.provider.MediaStore
import android.provider.OpenableColumns
import android.util.Log
import android.widget.Toast
import androidx.core.app.NotificationCompat
import androidx.core.content.ContextCompat
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.MultipartBody
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody
import okio.BufferedSink
import okio.source
import org.json.JSONObject
import java.io.File
import java.util.concurrent.TimeUnit

/**
 * Handles phone <-> PC file uploads and downloads via /api/files.
 */
object FileHandoff {

    private const val TAG = "LW-FileHandoff"
    private const val CHANNEL_ID = "localwhisper_handoff"
    private const val MAX_FILE_BYTES = 200L * 1024L * 1024L

    private val httpClient = OkHttpClient.Builder()
        .connectTimeout(10, TimeUnit.SECONDS)
        .writeTimeout(120, TimeUnit.SECONDS)
        .readTimeout(120, TimeUnit.SECONDS)
        .build()

    suspend fun downloadOfferedFile(context: Context, offer: JSONObject): Uri? = withContext(Dispatchers.IO) {
        val baseUrl = ServerPreferences(context).buildBaseUrl() ?: return@withContext null
        val relUrl = offer.optString("url", "")
        if (!relUrl.startsWith("/api/files/")) return@withContext null
        val fileName = offer.optString("name", "file").ifBlank { "file" }
        val mimeType = offer.optString("mime_type", "application/octet-stream")
            .ifBlank { "application/octet-stream" }

        val request = Request.Builder().url(baseUrl.trimEnd('/') + relUrl).get().build()
        val resolver = context.contentResolver
        val values = ContentValues().apply {
            put(MediaStore.MediaColumns.DISPLAY_NAME, fileName)
            put(MediaStore.MediaColumns.MIME_TYPE, mimeType)
            put(MediaStore.MediaColumns.RELATIVE_PATH, Environment.DIRECTORY_DOWNLOADS + "/LocalWhisper")
            put(MediaStore.MediaColumns.IS_PENDING, 1)
        }
        val itemUri = resolver.insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, values)
            ?: return@withContext null

        try {
            httpClient.newCall(request).execute().use { response ->
                if (!response.isSuccessful) {
                    resolver.delete(itemUri, null, null)
                    return@withContext null
                }
                val source = response.body?.source() ?: run {
                    resolver.delete(itemUri, null, null)
                    return@withContext null
                }
                resolver.openOutputStream(itemUri)?.use { out ->
                    val buf = ByteArray(64 * 1024)
                    var total = 0L
                    while (true) {
                        val read = source.read(buf)
                        if (read <= 0) break
                        total += read
                        if (total > MAX_FILE_BYTES) {
                            throw IllegalStateException("File exceeds 200 MB limit")
                        }
                        out.write(buf, 0, read)
                    }
                }
            }
            val doneValues = ContentValues().apply { put(MediaStore.MediaColumns.IS_PENDING, 0) }
            resolver.update(itemUri, doneValues, null, null)
            notifyFileReceived(context, itemUri, fileName, mimeType)
            itemUri
        } catch (exc: Exception) {
            Log.w(TAG, "Failed to download offered file $fileName", exc)
            try {
                resolver.delete(itemUri, null, null)
            } catch (_: Exception) {
            }
            null
        }
    }

    suspend fun uploadUriToPc(
        context: Context,
        uri: Uri,
        fallbackName: String = "shared_file",
        clientId: String? = null
    ): JSONObject = withContext(Dispatchers.IO) {
        val prefs = ServerPreferences(context)
        val baseUrl = prefs.buildBaseUrl()
            ?: return@withContext JSONObject().put("ok", false).put("error", "Invalid server configuration")
        val fileName = queryDisplayName(context, uri) ?: fallbackName
        val mimeType = context.contentResolver.getType(uri) ?: "application/octet-stream"
        val targetClient = clientId ?: prefs.selectedClientId.takeIf { it != "local-phone" && it.isNotBlank() }

        val streamBody = object : RequestBody() {
            override fun contentType() = mimeType.toMediaType()
            override fun writeTo(sink: BufferedSink) {
                val input = context.contentResolver.openInputStream(uri)
                    ?: throw IllegalStateException("Cannot open selected file")
                input.use { stream ->
                    sink.writeAll(stream.source())
                }
            }
        }

        val multipart = MultipartBody.Builder()
            .setType(MultipartBody.FORM)
            .addFormDataPart("file", fileName, streamBody)
            .addFormDataPart("target", "pc")
            .addFormDataPart("source", "phone")
            .apply {
                if (!targetClient.isNullOrBlank()) {
                    addFormDataPart("client_id", targetClient)
                }
            }
            .build()

        val request = Request.Builder()
            .url("$baseUrl/api/files")
            .post(multipart)
            .build()

        httpClient.newCall(request).execute().use { response ->
            val bodyText = response.body?.string().orEmpty()
            if (!response.isSuccessful) {
                return@withContext JSONObject().apply {
                    put("ok", false)
                    put("error", "Upload failed (HTTP ${response.code}): ${bodyText.take(160)}")
                }
            }
            val parsed = try {
                JSONObject(bodyText)
            } catch (_: Exception) {
                JSONObject()
            }
            parsed.put("ok", true)
            parsed
        }
    }

    suspend fun sendPhoneFileToPc(context: Context, arguments: JSONObject): JSONObject {
        val which = arguments.optString("which", "latest_screenshot")
        val clientId = arguments.optString("client_id", "").ifBlank { null }
        if (which == "path") {
            val rawPath = arguments.optString("path", "").trim()
            val file = File(rawPath)
            if (!file.isFile) {
                return JSONObject().put("ok", false).put("error", "File not found on phone: $rawPath")
            }
            return uploadUriToPc(context, Uri.fromFile(file), file.name, clientId)
        }

        if (!hasMediaImagesPermission(context)) {
            return JSONObject().apply {
                put("ok", false)
                put("reason", "media_permission_required")
                put(
                    "error",
                    "Grant Photos/Media permission in LocalWhisper settings so Jarvis can find your latest screenshot or photo."
                )
            }
        }

        val screenshotsOnly = which == "latest_screenshot"
        val found = findLatestMediaImage(context, screenshotsOnly)
            ?: return JSONObject().apply {
                put("ok", false)
                put("reason", "file_not_found")
                put("error", "No matching ${if (screenshotsOnly) "screenshot" else "photo"} found on this phone.")
            }
        return uploadUriToPc(context, found.first, found.second, clientId)
    }

    fun hasMediaImagesPermission(context: Context): Boolean {
        val perm = if (Build.VERSION.SDK_INT >= 33) {
            Manifest.permission.READ_MEDIA_IMAGES
        } else {
            Manifest.permission.READ_EXTERNAL_STORAGE
        }
        return ContextCompat.checkSelfPermission(context, perm) == PackageManager.PERMISSION_GRANTED
    }

    private fun findLatestMediaImage(context: Context, screenshotsOnly: Boolean): Pair<Uri, String>? {
        val projection = arrayOf(
            MediaStore.Images.Media._ID,
            MediaStore.Images.Media.DISPLAY_NAME,
            MediaStore.Images.Media.RELATIVE_PATH
        )
        val sortOrder = "${MediaStore.Images.Media.DATE_ADDED} DESC"
        context.contentResolver.query(
            MediaStore.Images.Media.EXTERNAL_CONTENT_URI,
            projection,
            null,
            null,
            sortOrder
        )?.use { cursor ->
            val idCol = cursor.getColumnIndexOrThrow(MediaStore.Images.Media._ID)
            val nameCol = cursor.getColumnIndexOrThrow(MediaStore.Images.Media.DISPLAY_NAME)
            val pathCol = cursor.getColumnIndex(MediaStore.Images.Media.RELATIVE_PATH)
            var scanned = 0
            while (cursor.moveToNext() && scanned < 60) {
                scanned++
                val name = cursor.getString(nameCol).orEmpty()
                val relPath = if (pathCol >= 0) cursor.getString(pathCol).orEmpty() else ""
                if (screenshotsOnly) {
                    val isShot = name.contains("screenshot", ignoreCase = true) ||
                        relPath.contains("screenshot", ignoreCase = true)
                    if (!isShot) continue
                }
                val id = cursor.getLong(idCol)
                val uri = ContentUris.withAppendedId(MediaStore.Images.Media.EXTERNAL_CONTENT_URI, id)
                return uri to name.ifBlank { "image.jpg" }
            }
        }
        return null
    }

    private fun queryDisplayName(context: Context, uri: Uri): String? {
        if (uri.scheme == "file") return uri.lastPathSegment
        return try {
            context.contentResolver.query(uri, arrayOf(OpenableColumns.DISPLAY_NAME), null, null, null)?.use { c ->
                val idx = c.getColumnIndex(OpenableColumns.DISPLAY_NAME)
                if (idx >= 0 && c.moveToFirst()) c.getString(idx) else null
            }
        } catch (_: Exception) {
            null
        }
    }

    private fun notifyFileReceived(context: Context, uri: Uri, fileName: String, mimeType: String) {
        Handler(Looper.getMainLooper()).post {
            Toast.makeText(
                context,
                context.getString(R.string.file_received_toast_format, fileName),
                Toast.LENGTH_LONG
            ).show()
        }
        val nm = context.getSystemService(NotificationManager::class.java) ?: return
        val channel = NotificationChannel(
            CHANNEL_ID,
            "File Handoff",
            NotificationManager.IMPORTANCE_DEFAULT
        )
        nm.createNotificationChannel(channel)
        if (Build.VERSION.SDK_INT >= 33 &&
            ContextCompat.checkSelfPermission(context, Manifest.permission.POST_NOTIFICATIONS) !=
            PackageManager.PERMISSION_GRANTED
        ) {
            return
        }
        val openIntent = Intent(Intent.ACTION_VIEW).apply {
            setDataAndType(uri, mimeType)
            addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_ACTIVITY_NEW_TASK)
        }
        val pending = PendingIntent.getActivity(
            context,
            fileName.hashCode(),
            openIntent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )
        val notification = NotificationCompat.Builder(context, CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_check_circle)
            .setContentTitle(context.getString(R.string.file_received_notif_title))
            .setContentText(fileName)
            .setAutoCancel(true)
            .setContentIntent(pending)
            .build()
        nm.notify(fileName.hashCode(), notification)
    }
}
