package me.Integer.localwhisper

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.View
import android.widget.ArrayAdapter
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.lifecycle.lifecycleScope
import com.google.android.material.chip.Chip
import kotlinx.coroutines.Job
import kotlinx.coroutines.launch
import me.Integer.localwhisper.databinding.ActivityAssistantBinding
import java.io.File
import java.util.Locale

class AssistantActivity : AppCompatActivity() {

    private enum class UiState { SETUP_REQUIRED, RECORDING, TRANSCRIBING, REVIEW, SENDING }

    private lateinit var binding: ActivityAssistantBinding
    private lateinit var prefs: ServerPreferences
    private val api = LocalWhisperApi()
    private var recorder: WavAudioRecorder? = null
    private var activeJob: Job? = null
    private var recordingStartMs: Long = 0L
    private var connectedClients: List<PcClientInfo> = emptyList()
    private var selectedClient: PcClientInfo? = null

    private val timerHandler = Handler(Looper.getMainLooper())
    private val timerRunnable = object : Runnable {
        override fun run() {
            val elapsedSec = ((System.currentTimeMillis() - recordingStartMs) / 1000L).coerceAtLeast(0L)
            val minutes = elapsedSec / 60L
            val seconds = elapsedSec % 60L
            binding.timerText.text = String.format(Locale.US, "%02d:%02d", minutes, seconds)
            timerHandler.postDelayed(this, TIMER_TICK_MS)
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityAssistantBinding.inflate(layoutInflater)
        setContentView(binding.root)
        prefs = ServerPreferences(this)

        setupListeners()
        setupAhkChips()
        beginAssistantFlow()
    }

    override fun onDestroy() {
        super.onDestroy()
        stopTimer()
        activeJob?.cancel()
        recorder?.cancel()
    }

    private fun setupListeners() {
        binding.overlayScrim.setOnClickListener { dismissOverlay() }
        binding.closeButton.setOnClickListener { dismissOverlay() }
        binding.cancelRecordingButton.setOnClickListener { dismissOverlay() }
        binding.cancelTranscribeButton.setOnClickListener { dismissOverlay() }
        binding.stopAndTranscribeButton.setOnClickListener { stopAndTranscribe() }
        binding.rerecordButton.setOnClickListener { startRecording() }
        binding.sendToPcButton.setOnClickListener { sendTranscriptToPc() }
        binding.openMainButton.setOnClickListener {
            startActivity(Intent(this, MainActivity::class.java))
            finish()
        }
        binding.targetPcDropdown.setOnItemClickListener { _, _, position, _ ->
            val chosen = connectedClients.getOrNull(position) ?: return@setOnItemClickListener
            selectedClient = chosen
            prefs.selectedClientId = chosen.id
        }
    }

    private fun setupAhkChips() {
        binding.ahkChipGroup.removeAllViews()
        for (token in AHK_QUICK_TOKENS) {
            val chip = Chip(this).apply {
                text = token
                isCheckable = false
                setOnClickListener { insertTokenAtCursor(token) }
            }
            binding.ahkChipGroup.addView(chip)
        }
    }

    private fun insertTokenAtCursor(token: String) {
        val editText = binding.transcriptInput
        val editable = editText.text ?: return
        val start = editText.selectionStart.coerceAtLeast(0)
        val end = editText.selectionEnd.coerceAtLeast(0)
        val minIdx = minOf(start, end)
        val maxIdx = maxOf(start, end)
        editable.replace(minIdx, maxIdx, token)
    }

    private fun beginAssistantFlow() {
        if (!hasRecordAudioPermission()) {
            showSetupRequired(getString(R.string.assistant_mic_required))
            return
        }
        if (prefs.buildBaseUrl() == null) {
            showSetupRequired(getString(R.string.invalid_server_config))
            return
        }
        startRecording()
    }

    private fun startRecording() {
        clearError()
        activeJob?.cancel()
        recorder?.cancel()
        binding.waveformView.reset()

        val tempWav = File(cacheDir, "localwhisper_recording.wav")
        val newRecorder = WavAudioRecorder(
            context = this,
            onAmplitude = { level -> binding.waveformView.pushAmplitude(level) },
            onMaxDurationReached = { runOnUiThread { stopAndTranscribe() } }
        )
        val startResult = newRecorder.start(tempWav)
        if (startResult.isFailure) {
            val message = startResult.exceptionOrNull()?.message ?: "Unable to start microphone"
            showError(message)
            setUiState(UiState.REVIEW)
            return
        }

        recorder = newRecorder
        recordingStartMs = System.currentTimeMillis()
        binding.timerText.text = "00:00"
        startTimer()
        setUiState(UiState.RECORDING)
    }

    private fun stopAndTranscribe() {
        stopTimer()
        val wavFile = recorder?.stop()
        if (wavFile == null || !wavFile.isFile) {
            showError("No audio captured.")
            setUiState(UiState.REVIEW)
            return
        }

        val baseUrl = prefs.buildBaseUrl()
        if (baseUrl == null) {
            showSetupRequired(getString(R.string.invalid_server_config))
            return
        }

        clearError()
        setUiState(UiState.TRANSCRIBING)

        activeJob?.cancel()
        activeJob = lifecycleScope.launch {
            val clientsJob = launch { refreshConnectedClients(baseUrl) }
            val result = api.transcribeAudio(baseUrl, wavFile, prefs.language)
            wavFile.delete()
            clientsJob.join()

            result.onSuccess { response ->
                binding.transcriptInput.setText(response.text)
                binding.transcriptInput.setSelection(response.text.length)
                setUiState(UiState.REVIEW)
            }.onFailure { exc ->
                showError(exc.message ?: "Transcription failed")
                setUiState(UiState.REVIEW)
            }
        }
    }

    private suspend fun refreshConnectedClients(baseUrl: String) {
        api.fetchClients(baseUrl).onSuccess { clients ->
            connectedClients = clients
            updateClientDropdown()
        }
    }

    private fun updateClientDropdown() {
        if (connectedClients.isEmpty()) {
            selectedClient = null
            binding.targetPcDropdown.setAdapter(null)
            binding.targetPcDropdown.setText(getString(R.string.assistant_no_pc_connected), false)
            return
        }

        val savedId = prefs.selectedClientId
        val preferred = connectedClients.firstOrNull { it.id == savedId } ?: connectedClients.last()
        selectedClient = preferred

        val adapter = ArrayAdapter(
            this,
            android.R.layout.simple_list_item_1,
            connectedClients.map { it.name }
        )
        binding.targetPcDropdown.setAdapter(adapter)
        binding.targetPcDropdown.setText(preferred.name, false)
    }

    private fun sendTranscriptToPc() {
        val text = binding.transcriptInput.text?.toString().orEmpty()
        if (text.isEmpty()) {
            showError(getString(R.string.assistant_empty_transcript))
            return
        }
        val baseUrl = prefs.buildBaseUrl()
        if (baseUrl == null) {
            showError(getString(R.string.invalid_server_config))
            return
        }

        clearError()
        setUiState(UiState.SENDING)

        activeJob?.cancel()
        activeJob = lifecycleScope.launch {
            val result = api.sendKeystrokes(baseUrl, text, selectedClient?.id)
            result.onSuccess { response ->
                Toast.makeText(
                    this@AssistantActivity,
                    getString(R.string.assistant_sent_toast, response.clientName),
                    Toast.LENGTH_SHORT
                ).show()
                finish()
            }.onFailure { exc ->
                showError(exc.message ?: "Failed to send keystrokes")
                setUiState(UiState.REVIEW)
            }
        }
    }

    private fun setUiState(state: UiState) {
        binding.setupRequiredSection.visibility =
            if (state == UiState.SETUP_REQUIRED) View.VISIBLE else View.GONE
        binding.recordingSection.visibility =
            if (state == UiState.RECORDING) View.VISIBLE else View.GONE
        binding.transcribingSection.visibility =
            if (state == UiState.TRANSCRIBING) View.VISIBLE else View.GONE
        binding.reviewSection.visibility =
            if (state == UiState.REVIEW || state == UiState.SENDING) View.VISIBLE else View.GONE

        val isSending = state == UiState.SENDING
        binding.sendToPcButton.isEnabled = !isSending
        binding.rerecordButton.isEnabled = !isSending
        binding.transcriptInput.isEnabled = !isSending

        binding.stateLabel.text = when (state) {
            UiState.SETUP_REQUIRED -> getString(R.string.assistant_state_error)
            UiState.RECORDING -> getString(R.string.assistant_state_listening)
            UiState.TRANSCRIBING -> getString(R.string.assistant_state_transcribing)
            UiState.REVIEW -> getString(R.string.assistant_state_review)
            UiState.SENDING -> getString(R.string.assistant_state_sending)
        }
    }

    private fun showSetupRequired(message: String) {
        binding.setupRequiredMessage.text = message
        setUiState(UiState.SETUP_REQUIRED)
    }

    private fun showError(message: String) {
        binding.errorBanner.text = message
        binding.errorBanner.visibility = View.VISIBLE
    }

    private fun clearError() {
        binding.errorBanner.text = ""
        binding.errorBanner.visibility = View.GONE
    }

    private fun startTimer() {
        stopTimer()
        timerHandler.post(timerRunnable)
    }

    private fun stopTimer() {
        timerHandler.removeCallbacks(timerRunnable)
    }

    private fun dismissOverlay() {
        stopTimer()
        activeJob?.cancel()
        recorder?.cancel()
        finish()
    }

    private fun hasRecordAudioPermission(): Boolean {
        return ContextCompat.checkSelfPermission(
            this,
            Manifest.permission.RECORD_AUDIO
        ) == PackageManager.PERMISSION_GRANTED
    }

    companion object {
        private const val TIMER_TICK_MS = 250L
        private val AHK_QUICK_TOKENS = listOf(
            "{Enter}",
            "{Tab}",
            "{Backspace}",
            "{Esc}",
            "^a",
            "^c",
            "^v"
        )
    }
}
