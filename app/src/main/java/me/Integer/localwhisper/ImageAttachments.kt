package me.Integer.localwhisper

import android.content.Context
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.net.Uri
import android.util.Base64
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.json.JSONArray
import org.json.JSONObject
import java.io.ByteArrayOutputStream

data class PromptImageAttachment(
    val label: String,
    val mimeType: String,
    val dataB64: String,
    val width: Int,
    val height: Int
) {
    fun toJson(): JSONObject = JSONObject().apply {
        put("label", label)
        put("mime_type", mimeType)
        put("data_b64", dataB64)
        put("width", width)
        put("height", height)
    }
}

/**
 * Decodes and resizes camera/gallery images into bounded JPEG base64 attachments for the Agent.
 */
object ImageAttachments {

    const val MAX_ATTACHMENTS = 2
    private const val MAX_EDGE_PX = 1280
    private const val JPEG_QUALITY = 82

    fun toJsonArray(items: List<PromptImageAttachment>): JSONArray {
        val arr = JSONArray()
        for (item in items.take(MAX_ATTACHMENTS)) {
            arr.put(item.toJson())
        }
        return arr
    }

    suspend fun encodeUri(
        context: Context,
        uri: Uri,
        label: String = "Photo"
    ): PromptImageAttachment? = withContext(Dispatchers.IO) {
        try {
            val boundsOpts = BitmapFactory.Options().apply { inJustDecodeBounds = true }
            context.contentResolver.openInputStream(uri)?.use { stream ->
                BitmapFactory.decodeStream(stream, null, boundsOpts)
            }
            if (boundsOpts.outWidth <= 0 || boundsOpts.outHeight <= 0) return@withContext null

            var sampleSize = 1
            while (maxOf(boundsOpts.outWidth, boundsOpts.outHeight) / sampleSize > MAX_EDGE_PX * 2) {
                sampleSize *= 2
            }
            val decodeOpts = BitmapFactory.Options().apply { inSampleSize = sampleSize }
            val decoded = context.contentResolver.openInputStream(uri)?.use { stream ->
                BitmapFactory.decodeStream(stream, null, decodeOpts)
            } ?: return@withContext null

            val longest = maxOf(decoded.width, decoded.height)
            val scaled = if (longest > MAX_EDGE_PX) {
                val ratio = MAX_EDGE_PX.toFloat() / longest.toFloat()
                val w = (decoded.width * ratio).toInt().coerceAtLeast(1)
                val h = (decoded.height * ratio).toInt().coerceAtLeast(1)
                Bitmap.createScaledBitmap(decoded, w, h, true).also {
                    if (it !== decoded) decoded.recycle()
                }
            } else {
                decoded
            }

            val out = ByteArrayOutputStream()
            scaled.compress(Bitmap.CompressFormat.JPEG, JPEG_QUALITY, out)
            val finalW = scaled.width
            val finalH = scaled.height
            scaled.recycle()
            val bytes = out.toByteArray()
            if (bytes.isEmpty()) return@withContext null
            PromptImageAttachment(
                label = label.take(48),
                mimeType = "image/jpeg",
                dataB64 = Base64.encodeToString(bytes, Base64.NO_WRAP),
                width = finalW,
                height = finalH
            )
        } catch (_: Exception) {
            null
        }
    }
}
