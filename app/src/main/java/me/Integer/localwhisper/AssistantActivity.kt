package me.Integer.localwhisper

import android.Manifest
import android.animation.ObjectAnimator
import android.animation.ValueAnimator
import android.content.Intent
import android.content.pm.PackageManager
import android.content.res.ColorStateList
import android.graphics.Typeface
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.HapticFeedbackConstants
import android.view.View
import android.view.ViewGroup
import android.view.animation.DecelerateInterpolator
import android.view.animation.OvershootInterpolator
import android.widget.ArrayAdapter
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.core.view.ViewCompat
import androidx.core.view.WindowInsetsCompat
import androidx.lifecycle.lifecycleScope
import com.google.android.material.chip.Chip
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import me.Integer.localwhisper.databinding.ActivityAssistantBinding
import java.io.File
import java.util.Locale

class AssistantActivity : AppCompatActivity() {

    private enum class UiState { SETUP_REQUIRED, RECORDING, TRANSCRIBING, REVIEW, SENDING, SENT }

    private lateinit var binding: ActivityAssistantBinding
    private lateinit var prefs: ServerPreferences
    private val api = LocalWhisperApi()
    private var recorder: WavAudioRecorder? = null
    private var activeJob: Job? = null
    private var recordingStartMs: Long = 0L
    private var connectedClients: List<PcClientInfo> = emptyList()
    private var selectedClient: PcClientInfo? = null
    private var dotPulseAnimator: ObjectAnimator? = null
    private var isDismissing = false

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

        applySheetWindowInsets()
        setupListeners()
        setupAhkChips()
        animateSheetIn()
        beginAssistantFlow()
    }

    override fun onDestroy() {
        super.onDestroy()
        stopTimer()
        stopDotPulse()
        binding.waveformView.setActive(false)
        activeJob?.cancel()
        recorder?.cancel()
    }

    private fun applySheetWindowInsets() {
        val baseBottomMargin = dpToPx(14)
        ViewCompat.setOnApplyWindowInsetsListener(binding.overlayScrim) { _, insets ->
            val bars = insets.getInsets(
                WindowInsetsCompat.Type.systemBars() or WindowInsetsCompat.Type.ime()
            )
            val params = binding.sheetCard.layoutParams as ViewGroup.MarginLayoutParams
            params.bottomMargin = baseBottomMargin + bars.bottom
            binding.sheetCard.layoutParams = params
            insets
        }
    }

    private fun animateSheetIn() {
        binding.sheetCard.alpha = 0f
        binding.sheetCard.translationY = dpToPx(64).toFloat()
        binding.sheetCard.animate()
            .alpha(1f)
            .translationY(0f)
            .setDuration(240L)
            .setInterpolator(DecelerateInterpolator(2.0f))
            .start()
    }

    private fun setupListeners() {
        binding.overlayScrim.setOnClickListener { dismissOverlay() }
        binding.closeButton.setOnClickListener { dismissOverlay() }
        binding.cancelRecordingButton.setOnClickListener { dismissOverlay() }
        binding.cancelTranscribeButton.setOnClickListener { dismissOverlay() }
        binding.stopAndTranscribeButton.setOnClickListener {
            performTapHaptic(it)
            stopAndTranscribe()
        }
        binding.rerecordButton.setOnClickListener {
            performTapHaptic(it)
            startRecording()
        }
        binding.sendToPcButton.setOnClickListener {
            performTapHaptic(it)
            sendTranscriptToPc()
        }
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
        val chipBg = ColorStateList.valueOf(ContextCompat.getColor(this, R.color.lw_pill_bg))
        val chipStroke = ColorStateList.valueOf(ContextCompat.getColor(this, R.color.lw_card_stroke))
        val chipText = ContextCompat.getColor(this, R.color.lw_text_primary)

        for (token in AHK_QUICK_TOKENS) {
            val chip = Chip(this).apply {
                text = token
                typeface = Typeface.MONOSPACE
                textSize = 12f
                isCheckable = false
                chipBackgroundColor = chipBg
                chipStrokeColor = chipStroke
                chipStrokeWidth = dpToPx(1).toFloat()
                setTextColor(chipText)
                setEnsureMinTouchTargetSize(false)
                setOnClickListener {
                    performTapHaptic(it)
                    insertTokenAtCursor(token)
                }
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
        binding.waveformView.setActive(true)
        startTimer()
        setUiState(UiState.RECORDING)
    }

    private fun stopAndTranscribe() {
        stopTimer()
        binding.waveformView.setActive(false)
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
                showSentAndDismiss(response.clientName)
            }.onFailure { exc ->
                showError(exc.message ?: "Failed to send keystrokes")
                setUiState(UiState.REVIEW)
            }
        }
    }

    private suspend fun showSentAndDismiss(clientName: String) {
        binding.sentMessageText.text = getString(R.string.assistant_sent_message, clientName)
        setUiState(UiState.SENT)
        binding.sentCheckIcon.scaleX = 0.4f
        binding.sentCheckIcon.scaleY = 0.4f
        binding.sentCheckIcon.animate()
            .scaleX(1f)
            .scaleY(1f)
            .setDuration(240L)
            .setInterpolator(OvershootInterpolator(2.0f))
            .start()
        delay(520L)
        dismissOverlay()
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
        binding.sentSection.visibility =
            if (state == UiState.SENT) View.VISIBLE else View.GONE

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
            UiState.SENT -> getString(R.string.assistant_state_sent)
        }

        val dotColorRes = when (state) {
            UiState.RECORDING -> R.color.lw_record_coral
            UiState.TRANSCRIBING, UiState.SENDING -> R.color.lw_accent
            UiState.REVIEW, UiState.SENT -> R.color.lw_success
            UiState.SETUP_REQUIRED -> R.color.lw_error
        }
        binding.statusDot.backgroundTintList =
            ColorStateList.valueOf(ContextCompat.getColor(this, dotColorRes))

        val shouldPulse = state == UiState.RECORDING ||
            state == UiState.TRANSCRIBING ||
            state == UiState.SENDING
        if (shouldPulse) startDotPulse() else stopDotPulse()
    }

    private fun startDotPulse() {
        if (dotPulseAnimator?.isRunning == true) return
        dotPulseAnimator = ObjectAnimator.ofFloat(binding.statusDot, View.ALPHA, 1f, 0.25f).apply {
            duration = 650L
            repeatMode = ValueAnimator.REVERSE
            repeatCount = ValueAnimator.INFINITE
            start()
        }
    }

    private fun stopDotPulse() {
        dotPulseAnimator?.cancel()
        dotPulseAnimator = null
        binding.statusDot.alpha = 1f
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
        if (isDismissing) return
        isDismissing = true
        stopTimer()
        stopDotPulse()
        binding.waveformView.setActive(false)
        activeJob?.cancel()
        recorder?.cancel()
        binding.sheetCard.animate()
            .alpha(0f)
            .translationY(dpToPx(64).toFloat())
            .setDuration(170L)
            .setInterpolator(DecelerateInterpolator())
            .withEndAction {
                finish()
                @Suppress("DEPRECATION")
                overridePendingTransition(0, 0)
            }
            .start()
    }

    private fun performTapHaptic(view: View) {
        view.performHapticFeedback(HapticFeedbackConstants.KEYBOARD_TAP)
    }

    private fun dpToPx(dp: Int): Int {
        return (dp * resources.displayMetrics.density).toInt()
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
