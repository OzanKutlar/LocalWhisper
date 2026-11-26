package me.Integer.localwhisper

import android.os.Bundle
import androidx.appcompat.app.AppCompatActivity

/**
 * Invisible activity that briefly gains focus so Android 10+ allows reading ClipboardManager,
 * then finishes immediately without animation.
 */
class ClipboardBridgeActivity : AppCompatActivity() {

    private var completed = false

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        @Suppress("DEPRECATION")
        overridePendingTransition(0, 0)
    }

    override fun onWindowFocusChanged(hasFocus: Boolean) {
        super.onWindowFocusChanged(hasFocus)
        if (hasFocus && !completed) {
            completed = true
            val text = LocalClipboard.readDirect(this)
            LocalClipboard.completeBridgeRead(text)
            finish()
            @Suppress("DEPRECATION")
            overridePendingTransition(0, 0)
        }
    }

    override fun onDestroy() {
        if (!completed) {
            LocalClipboard.completeBridgeRead(null)
        }
        super.onDestroy()
    }
}
