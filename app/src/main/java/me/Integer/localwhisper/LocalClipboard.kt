package me.Integer.localwhisper

import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.util.Log
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeoutOrNull
import org.json.JSONObject

/**
 * Copies and reads text on the Android system clipboard.
 * When a background RPC asks to read the clipboard on Android 10+, briefly launches
 * ClipboardBridgeActivity to acquire window focus.
 */
object LocalClipboard {

    private const val TAG = "LW-Clipboard"
    private const val CLIP_LABEL = "LocalWhisper"
    private const val BRIDGE_TIMEOUT_MS = 1_800L
    private const val MAX_CLIP_CHARS = 16_000

    @Volatile
    private var pendingReadDeferred: CompletableDeferred<String?>? = null

    fun copy(context: Context, text: String): Boolean {
        if (text.isEmpty()) return false
        val clipboard = context.getSystemService(ClipboardManager::class.java) ?: return false
        return try {
            clipboard.setPrimaryClip(ClipData.newPlainText(CLIP_LABEL, text))
            true
        } catch (exc: Exception) {
            Log.w(TAG, "Could not copy text to the clipboard", exc)
            false
        }
    }

    fun readDirect(context: Context): String? {
        val clipboard = context.getSystemService(ClipboardManager::class.java) ?: return null
        return try {
            val clip = clipboard.primaryClip ?: return null
            if (clip.itemCount <= 0) return null
            clip.getItemAt(0)?.coerceToText(context)?.toString()
        } catch (exc: Exception) {
            Log.d(TAG, "Direct clipboard read unavailable", exc)
            null
        }
    }

    fun completeBridgeRead(text: String?) {
        pendingReadDeferred?.complete(text)
    }

    suspend fun readWithForegroundAssist(context: Context): JSONObject {
        val direct = withContext(Dispatchers.Main) { readDirect(context) }
        val resolved = if (direct != null) {
            direct
        } else {
            val deferred = CompletableDeferred<String?>()
            pendingReadDeferred = deferred
            withContext(Dispatchers.Main) {
                try {
                    val intent = Intent(context, ClipboardBridgeActivity::class.java).apply {
                        addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_NO_ANIMATION)
                    }
                    context.startActivity(intent)
                } catch (exc: Exception) {
                    deferred.complete(null)
                }
            }
            withTimeoutOrNull(BRIDGE_TIMEOUT_MS) { deferred.await() }
        }

        val text = resolved.orEmpty().take(MAX_CLIP_CHARS)
        return JSONObject().apply {
            put("ok", resolved != null)
            put("text", text)
            put("length", text.length)
            put("empty", text.isEmpty())
            if (resolved == null) {
                put("error", "Could not read phone clipboard while locked or in background")
            }
        }
    }
}
