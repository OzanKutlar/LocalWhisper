package me.Integer.localwhisper

import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.lifecycle.lifecycleScope
import kotlinx.coroutines.launch

/**
 * "Send to PC" target in Android's native Share sheet.
 * Uploads shared files/photos to the PC's Downloads\LocalWhisper folder, opens shared URLs
 * in the PC browser, and copies shared text to the PC clipboard.
 */
class ShareToPcActivity : AppCompatActivity() {

    private val api = LocalWhisperApi()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        @Suppress("DEPRECATION")
        overridePendingTransition(0, 0)
        handleShareIntent(intent)
    }

    private fun handleShareIntent(intent: Intent?) {
        if (intent == null) {
            finish()
            return
        }
        val prefs = ServerPreferences(this)
        val baseUrl = prefs.buildBaseUrl()
        if (baseUrl == null) {
            Toast.makeText(this, R.string.invalid_server_config, Toast.LENGTH_LONG).show()
            finish()
            return
        }

        val uris = extractSharedUris(intent)
        val sharedText = intent.getCharSequenceExtra(Intent.EXTRA_TEXT)?.toString().orEmpty().trim()

        lifecycleScope.launch {
            try {
                if (uris.isNotEmpty()) {
                    var sentCount = 0
                    for (uri in uris.take(10)) {
                        val res = FileHandoff.uploadUriToPc(this@ShareToPcActivity, uri)
                        if (res.optBoolean("ok", false)) sentCount++
                    }
                    val msg = if (sentCount > 0) {
                        getString(R.string.share_to_pc_files_sent_format, sentCount)
                    } else {
                        getString(R.string.share_to_pc_failed)
                    }
                    Toast.makeText(this@ShareToPcActivity, msg, Toast.LENGTH_SHORT).show()
                } else if (sharedText.isNotEmpty()) {
                    val targetClient = prefs.selectedClientId.takeIf { it != "local-phone" && it.isNotBlank() }
                    val result = api.transferClipboard(baseUrl, sharedText, target = "pc", clientId = targetClient)
                    val msg = if (result.isSuccess) {
                        getString(R.string.share_to_pc_text_sent)
                    } else {
                        result.exceptionOrNull()?.message ?: getString(R.string.share_to_pc_failed)
                    }
                    Toast.makeText(this@ShareToPcActivity, msg, Toast.LENGTH_SHORT).show()
                }
            } finally {
                finish()
                @Suppress("DEPRECATION")
                overridePendingTransition(0, 0)
            }
        }
    }

    private fun extractSharedUris(intent: Intent): List<Uri> {
        return when (intent.action) {
            Intent.ACTION_SEND -> {
                val single = if (Build.VERSION.SDK_INT >= 33) {
                    intent.getParcelableExtra(Intent.EXTRA_STREAM, Uri::class.java)
                } else {
                    @Suppress("DEPRECATION")
                    intent.getParcelableExtra(Intent.EXTRA_STREAM)
                }
                listOfNotNull(single)
            }
            Intent.ACTION_SEND_MULTIPLE -> {
                val multi = if (Build.VERSION.SDK_INT >= 33) {
                    intent.getParcelableArrayListExtra(Intent.EXTRA_STREAM, Uri::class.java)
                } else {
                    @Suppress("DEPRECATION")
                    intent.getParcelableArrayListExtra(Intent.EXTRA_STREAM)
                }
                multi.orEmpty()
            }
            else -> emptyList()
        }
    }
}
