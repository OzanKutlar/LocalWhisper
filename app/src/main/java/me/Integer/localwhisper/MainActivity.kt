package me.Integer.localwhisper

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Bundle
import android.provider.Settings
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat
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

        populateSavedSettings()
        setupViews()
        updatePermissionStatus()
    }

    override fun onResume() {
        super.onResume()
        updatePermissionStatus()
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
            return
        }

        prefs.serverHost = host
        prefs.serverPort = port
        prefs.language = binding.languageInput.text?.toString().orEmpty()

        val baseUrl = prefs.buildBaseUrl() ?: return
        binding.saveAndTestButton.isEnabled = false
        binding.connectionStatusText.text = getString(R.string.testing_connection)

        lifecycleScope.launch {
            val result = api.checkHealth(baseUrl)
            binding.saveAndTestButton.isEnabled = true
            result.onSuccess { health ->
                binding.connectionStatusText.text = formatHealthSummary(health)
            }.onFailure { exc ->
                binding.connectionStatusText.text =
                    "Connection failed: ${exc.message ?: "Unable to reach $baseUrl"}"
            }
        }
    }

    private fun formatHealthSummary(health: HealthResponse): String {
        val clientNames = if (health.clients.isEmpty()) {
            "None (start pc_client/run_client.bat)"
        } else {
            health.clients.joinToString(", ") { it.name }
        }
        return buildString {
            appendLine("Connected (${health.status})")
            appendLine("Model: ${health.model}")
            appendLine("GPU: ${health.gpuName} [${health.device}]")
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
        binding.requestPermissionButton.isEnabled = !granted
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
