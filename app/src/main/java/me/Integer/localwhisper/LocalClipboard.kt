package me.Integer.localwhisper

import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.util.Log

/** Copies text to the system clipboard; used whenever text cannot be typed directly. */
object LocalClipboard {

    private const val TAG = "LW-Clipboard"
    private const val CLIP_LABEL = "LocalWhisper"

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
}
