package me.Integer.localwhisper

import android.Manifest
import android.app.role.RoleManager
import android.content.DialogInterface
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Bundle
import android.provider.Settings
import android.util.Log
import android.widget.TextView
import android.widget.Toast
import androidx.annotation.ColorRes
import androidx.appcompat.app.AppCompatActivity
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat
import androidx.core.view.ViewCompat
import androidx.core.view.WindowCompat
import androidx.core.view.WindowInsetsCompat
import androidx.lifecycle.lifecycleScope
import com.google.android.material.dialog.MaterialAlertDialogBuilder
import kotlinx.coroutines.launch
import me.Integer.localwhisper.databinding.ActivityMainBinding
import me.Integer.localwhisper.databinding.DialogLanguageBinding
import me.Integer.localwhisper.databinding.DialogServerAddressBinding

class MainActivity : AppCompatActivity() {

    private lateinit var binding: ActivityMainBinding
    private lateinit var prefs: ServerPreferences
    private val api = LocalWhisperApi()
    private var isTestingConnection = false

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        WindowCompat.setDecorFitsSystemWindows(window, false)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)
        prefs = ServerPreferences(this)

        applyWindowInsets()
        setupRows()
        refreshServerRows()
        resetConnectionStatus()
    }

    override fun onResume() {
        super.onResume()
        updatePermissionRow()
        updateDefaultAssistantRow()
        updateKeyboardModeRow()
    }

    // ------------------------------------------------------------ Layout

    private fun applyWindowInsets() {
        val horizontal = dpToPx(12)
        val baseTop = dpToPx(24)
        val baseBottom = dpToPx(24)
        ViewCompat.setOnApplyWindowInsetsListener(binding.rootScroll) { _, insets ->
            val bars = insets.getInsets(WindowInsetsCompat.Type.systemBars())
            binding.contentContainer.setPadding(
                horizontal + bars.left,
                baseTop + bars.top,
                horizontal + bars.right,
                baseBottom + bars.bottom
            )
            insets
        }
    }

    private fun setupRows() {
        binding.serverAddressRow.setOnClickListener { showServerAddressDialog() }
        binding.languageRow.setOnClickListener { showLanguageDialog() }
        binding.testConnectionRow.setOnClickListener { testConnection() }
        binding.micPermissionRow.setOnClickListener { handleMicPermissionRequest() }
        binding.defaultAssistantRow.setOnClickListener { openAssistantSettings() }
        binding.launchOverlayRow.setOnClickListener { launchVoiceOverlay() }
        binding.keyboardModeRow.setOnClickListener { toggleStartInKeyboardMode() }
    }

    private fun refreshServerRows() {
        binding.serverAddressValue.text =
            getString(R.string.server_address_format, prefs.serverHost, prefs.serverPort)
        val language = prefs.language
        binding.languageValue.text =
            if (language.isEmpty()) getString(R.string.row_language_auto) else language
    }

    // ------------------------------------------------------------ Dialogs

    private fun showServerAddressDialog() {
        val dialogBinding = DialogServerAddressBinding.inflate(layoutInflater)
        dialogBinding.dialogHostInput.setText(prefs.serverHost)
        dialogBinding.dialogPortInput.setText(prefs.serverPort.toString())

        val dialog = MaterialAlertDialogBuilder(this)
            .setTitle(R.string.row_server_address)
            .setView(dialogBinding.root)
            .setNegativeButton(R.string.dialog_cancel, null)
            .setPositiveButton(R.string.dialog_save, null)
            .create()
        // Override the positive button so invalid input keeps the dialog open.
        dialog.setOnShowListener {
            dialog.getButton(DialogInterface.BUTTON_POSITIVE).setOnClickListener {
                if (saveServerAddress(dialogBinding)) {
                    dialog.dismiss()
                }
            }
        }
        dialog.show()
    }

    private fun saveServerAddress(dialogBinding: DialogServerAddressBinding): Boolean {
        val host = ServerPreferences.sanitizeHost(
            dialogBinding.dialogHostInput.text?.toString().orEmpty()
        )
        val port = dialogBinding.dialogPortInput.text?.toString()?.trim()?.toIntOrNull()
        val hostValid = host.isNotEmpty()
        val portValid = port != null && port in 1..65535

        dialogBinding.dialogHostLayout.error =
            if (hostValid) null else getString(R.string.invalid_host)
        dialogBinding.dialogPortLayout.error =
            if (portValid) null else getString(R.string.invalid_port)
        if (!hostValid || port == null || !portValid) {
            return false
        }

        prefs.serverHost = host
        prefs.serverPort = port
        refreshServerRows()
        resetConnectionStatus()
        return true
    }

    private fun showLanguageDialog() {
        val dialogBinding = DialogLanguageBinding.inflate(layoutInflater)
        dialogBinding.dialogLanguageInput.setText(prefs.language)
        MaterialAlertDialogBuilder(this)
            .setTitle(R.string.row_language)
            .setView(dialogBinding.root)
            .setNegativeButton(R.string.dialog_cancel, null)
            .setPositiveButton(R.string.dialog_save) { _, _ ->
                prefs.language = dialogBinding.dialogLanguageInput.text?.toString().orEmpty()
                refreshServerRows()
            }
            .show()
    }

    // ------------------------------------------------------------ Connection

    private fun testConnection() {
        if (isTestingConnection) return
        val baseUrl = prefs.buildBaseUrl()
        if (baseUrl == null) {
            showTestResult(getString(R.string.invalid_server_config), R.color.lw_error)
            showConnectionStatus(getString(R.string.main_status_offline), R.color.lw_error)
            return
        }

        isTestingConnection = true
        showTestResult(getString(R.string.testing_connection), R.color.lw_accent)
        showConnectionStatus(getString(R.string.main_status_checking), R.color.lw_accent)

        lifecycleScope.launch {
            val result = api.checkHealth(baseUrl)
            isTestingConnection = false
            result.onSuccess { health ->
                showTestResult(formatHealthSummary(health), R.color.lw_text_secondary)
                showConnectionStatus(getString(R.string.main_status_connected), R.color.lw_success)
            }.onFailure { exc ->
                val reason = exc.message ?: baseUrl
                showTestResult(getString(R.string.connection_failed, reason), R.color.lw_error)
                showConnectionStatus(getString(R.string.main_status_offline), R.color.lw_error)
            }
        }
    }

    private fun resetConnectionStatus() {
        showConnectionStatus(getString(R.string.main_status_untested), R.color.lw_text_secondary)
        showTestResult(getString(R.string.row_test_connection_hint), R.color.lw_text_secondary)
    }

    private fun showConnectionStatus(text: String, @ColorRes colorRes: Int) {
        setColoredText(binding.connectionStatusLine, text, colorRes)
    }

    private fun showTestResult(text: String, @ColorRes colorRes: Int) {
        setColoredText(binding.testConnectionSubtitle, text, colorRes)
    }

    private fun setColoredText(view: TextView, text: String, @ColorRes colorRes: Int) {
        view.text = text
        view.setTextColor(ContextCompat.getColor(this, colorRes))
    }

    private fun formatHealthSummary(health: HealthResponse): String {
        val clientNames = if (health.clients.isEmpty()) {
            getString(R.string.health_no_clients)
        } else {
            health.clients.joinToString(", ") { it.name }
        }
        return getString(
            R.string.health_summary,
            health.gpuName,
            health.device,
            health.model,
            clientNames
        )
    }

    // ------------------------------------------------------------ Assistant

    private fun updateDefaultAssistantRow() {
        val isActive = isDefaultAssistant()
        val text = getString(
            if (isActive) R.string.default_assistant_active else R.string.default_assistant_inactive
        )
        val color = if (isActive) R.color.lw_success else R.color.lw_accent
        setColoredText(binding.defaultAssistantSubtitle, text, color)
    }

    private fun isDefaultAssistant(): Boolean {
        val roleManager = getSystemService(RoleManager::class.java) ?: return false
        return try {
            roleManager.isRoleAvailable(RoleManager.ROLE_ASSISTANT) &&
                roleManager.isRoleHeld(RoleManager.ROLE_ASSISTANT)
        } catch (exc: Exception) {
            Log.w(TAG, "Could not query assistant role", exc)
            false
        }
    }

    private fun openAssistantSettings() {
        val intents = listOf(
            Intent(Settings.ACTION_VOICE_INPUT_SETTINGS),
            Intent(Settings.ACTION_MANAGE_DEFAULT_APPS_SETTINGS)
        )
        for (intent in intents) {
            try {
                startActivity(intent)
                return
            } catch (exc: Exception) {
                Log.d(TAG, "Settings screen ${intent.action} unavailable, trying next", exc)
            }
        }
        Toast.makeText(this, R.string.default_assistant_fallback_toast, Toast.LENGTH_LONG).show()
    }

    private fun launchVoiceOverlay() {
        // Keyboard mode does not record audio, so the mic permission is only needed for voice.
        if (!hasRecordAudioPermission() && !prefs.startInKeyboardMode) {
            handleMicPermissionRequest()
            return
        }
        val intent = Intent(this, AssistantActivity::class.java).apply {
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TASK)
        }
        startActivity(intent)
    }

    // ------------------------------------------------------------ Keyboard mode

    private fun toggleStartInKeyboardMode() {
        prefs.startInKeyboardMode = !prefs.startInKeyboardMode
        updateKeyboardModeRow()
    }

    private fun updateKeyboardModeRow() {
        binding.keyboardModeSwitch.isChecked = prefs.startInKeyboardMode
    }

    // ------------------------------------------------------------ Permission

    private fun handleMicPermissionRequest() {
        if (hasRecordAudioPermission()) {
            Toast.makeText(this, R.string.mic_permission_already_granted, Toast.LENGTH_SHORT).show()
            updatePermissionRow()
            return
        }
        ActivityCompat.requestPermissions(
            this,
            arrayOf(Manifest.permission.RECORD_AUDIO),
            RECORD_AUDIO_REQUEST_CODE
        )
    }

    private fun updatePermissionRow() {
        val granted = hasRecordAudioPermission()
        binding.micPermissionSwitch.isChecked = granted
        val text = getString(
            if (granted) R.string.mic_permission_granted else R.string.mic_permission_missing
        )
        val color = if (granted) R.color.lw_accent else R.color.lw_text_secondary
        setColoredText(binding.micPermissionSubtitle, text, color)
    }

    private fun hasRecordAudioPermission(): Boolean {
        return ContextCompat.checkSelfPermission(
            this,
            Manifest.permission.RECORD_AUDIO
        ) == PackageManager.PERMISSION_GRANTED
    }

    override fun onRequestPermissionsResult(
        requestCode: Int,
        permissions: Array<out String>,
        grantResults: IntArray
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode != RECORD_AUDIO_REQUEST_CODE) {
            return
        }
        val granted = grantResults.isNotEmpty() &&
            grantResults[0] == PackageManager.PERMISSION_GRANTED
        if (!granted) {
            Toast.makeText(this, R.string.mic_permission_denied, Toast.LENGTH_SHORT).show()
        }
        updatePermissionRow()
    }

    private fun dpToPx(dp: Int): Int {
        return (dp * resources.displayMetrics.density).toInt()
    }

    companion object {
        private const val TAG = "LW-Main"
        private const val RECORD_AUDIO_REQUEST_CODE = 1001
    }
}
