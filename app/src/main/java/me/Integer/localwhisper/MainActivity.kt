package me.Integer.localwhisper

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.content.res.ColorStateList
import android.os.Bundle
import android.provider.Settings
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat
import androidx.core.view.ViewCompat
import androidx.core.view.WindowInsetsCompat
import androidx.lifecycle.lifecycleScope
import kotlinx.coroutines.launch
import me.Integer.localwhisper.databinding.ActivityMainBinding

class MainActivity : AppCompatActivity() {

    private lateinit var binding: ActivityMainBinding
    private lateinit var prefs: ServerPreferences
    private val api = LocalWhisperApi()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)
        prefs = ServerPreferences(this)

        applyWindowInsets()
        populateSavedSettings()
        setupViews()
        updatePermissionStatus()
        updateConnectionPill(getString(R.string.status_pill_untested), R.color.lw_text_secondary)
    }

    override fun onResume() {
        super.onResume()
        updatePermissionStatus()
    }

    private fun applyWindowInsets() {
        val basePadding = dpToPx(20)
        val baseBottom = dpToPx(28)
        ViewCompat.setOnApplyWindowInsetsListener(binding.rootScroll) { _, insets ->
            val bars = insets.getInsets(WindowInsetsCompat.Type.systemBars())
            binding.contentContainer.setPadding(
                basePadding,
                basePadding + bars.top,
                basePadding,
                baseBottom + bars.bottom
            )
            insets
        }
    }

    private fun populateSavedSettings() {
        binding.serverHostInput.setText(prefs.serverHost)
        binding.serverPortInput.setText(prefs.serverPort.toString())
        binding.languageInput.setText(prefs.language)
    }

    private fun setupViews() {
        binding.saveAndTestButton.setOnClickListener { saveAndTestConnection() }
        binding.requestPermissionButton.setOnClickListener { handleMicPermissionRequest() }
        binding.setDefaultAssistantButton.setOnClickListener { openAssistantSettings() }
        binding.launchVoiceOverlayButton.setOnClickListener { launchVoiceOverlay() }
    }

    private fun saveAndTestConnection() {
        val host = ServerPreferences.sanitizeHost(binding.serverHostInput.text?.toString().orEmpty())
        val port = binding.serverPortInput.text?.toString()?.trim()?.toIntOrNull()
        if (host.isEmpty() || port == null || port !in 1..65535) {
            binding.connectionStatusText.text = getString(R.string.invalid_server_config)
            updateConnectionPill(getString(R.string.status_pill_offline), R.color.lw_error)
            return
        }

        prefs.serverHost = host
        prefs.serverPort = port
        prefs.language = binding.languageInput.text?.toString().orEmpty()

        val baseUrl = prefs.buildBaseUrl() ?: return
        binding.saveAndTestButton.isEnabled = false
        binding.connectionStatusText.text = getString(R.string.testing_connection)
        updateConnectionPill(getString(R.string.status_pill_checking), R.color.lw_accent)

        lifecycleScope.launch {
            val result = api.checkHealth(baseUrl)
            binding.saveAndTestButton.isEnabled = true
            result.onSuccess { health ->
                binding.connectionStatusText.text = formatHealthSummary(health)
                updateConnectionPill(getString(R.string.status_pill_connected), R.color.lw_success)
            }.onFailure { exc ->
                binding.connectionStatusText.text =
                    "Connection failed: ${exc.message ?: "Unable to reach $baseUrl"}"
                updateConnectionPill(getString(R.string.status_pill_offline), R.color.lw_error)
            }
        }
    }

    private fun updateConnectionPill(label: String, dotColorRes: Int) {
        binding.connectionPillText.text = label
        binding.connectionDot.backgroundTintList =
            ColorStateList.valueOf(ContextCompat.getColor(this, dotColorRes))
    }

    private fun formatHealthSummary(health: HealthResponse): String {
        val clientNames = if (health.clients.isEmpty()) {
            "None (start pc_client/run_client.bat)"
        } else {
            health.clients.joinToString(", ") { it.name }
        }
        return buildString {
            appendLine("GPU: ${health.gpuName} (${health.device})")
            appendLine("Model: ${health.model}")
            append("PC Clients: $clientNames")
        }
    }

    private fun launchVoiceOverlay() {
        if (!hasRecordAudioPermission()) {
            handleMicPermissionRequest()
            return
        }
        saveCurrentInputsSilently()
        startActivity(Intent(this, AssistantActivity::class.java))
    }

    private fun saveCurrentInputsSilently() {
        val host = ServerPreferences.sanitizeHost(binding.serverHostInput.text?.toString().orEmpty())
        val port = binding.serverPortInput.text?.toString()?.trim()?.toIntOrNull()
        if (host.isNotEmpty() && port != null && port in 1..65535) {
            prefs.serverHost = host
            prefs.serverPort = port
            prefs.language = binding.languageInput.text?.toString().orEmpty()
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
            } catch (_: Exception) {
                // Try fallback settings screen
            }
        }
        Toast.makeText(this, "Open Settings -> Default apps -> Digital assistant", Toast.LENGTH_LONG).show()
    }

    private fun handleMicPermissionRequest() {
        if (hasRecordAudioPermission()) {
            Toast.makeText(
                this,
                getString(R.string.mic_permission_already_granted),
                Toast.LENGTH_SHORT
            ).show()
            updatePermissionStatus()
            return
        }

        ActivityCompat.requestPermissions(
            this,
            arrayOf(Manifest.permission.RECORD_AUDIO),
            RECORD_AUDIO_REQUEST_CODE
        )
    }

    private fun hasRecordAudioPermission(): Boolean {
        return ContextCompat.checkSelfPermission(
            this,
            Manifest.permission.RECORD_AUDIO
        ) == PackageManager.PERMISSION_GRANTED
    }

    private fun updatePermissionStatus() {
        val granted = hasRecordAudioPermission()
        binding.statusText.text = if (granted) {
            getString(R.string.mic_permission_granted)
        } else {
            getString(R.string.mic_permission_missing)
        }
        binding.micStatusIcon.setImageResource(
            if (granted) R.drawable.ic_check_circle else R.drawable.ic_mic
        )
        val tintColor = ContextCompat.getColor(
            this,
            if (granted) R.color.lw_success else R.color.lw_text_secondary
        )
        binding.micStatusIcon.imageTintList = ColorStateList.valueOf(tintColor)
        binding.requestPermissionButton.isEnabled = !granted
    }

    private fun dpToPx(dp: Int): Int {
        return (dp * resources.displayMetrics.density).toInt()
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
            Toast.makeText(
                this,
                getString(R.string.mic_permission_denied),
                Toast.LENGTH_SHORT
            ).show()
        }
        updatePermissionStatus()
    }

    companion object {
        private const val RECORD_AUDIO_REQUEST_CODE = 1001
    }
}
