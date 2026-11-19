package me.Integer.localwhisper

import android.Manifest
import android.annotation.SuppressLint
import android.content.Intent
import android.content.pm.PackageManager
import android.content.res.ColorStateList
import android.graphics.Typeface
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.util.Log
import android.view.HapticFeedbackConstants
import android.view.View
import android.view.ViewGroup
import android.view.animation.DecelerateInterpolator
import android.view.animation.OvershootInterpolator
import android.widget.Toast
import androidx.annotation.DrawableRes
import androidx.annotation.StringRes
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.core.view.ViewCompat
import androidx.core.view.WindowCompat
import androidx.core.view.WindowInsetsCompat
import androidx.lifecycle.lifecycleScope
import com.google.android.material.button.MaterialButton
import com.google.android.material.chip.Chip
import com.google.android.material.dialog.MaterialAlertDialogBuilder
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import me.Integer.localwhisper.databinding.ActivityAssistantBinding
import java.io.File
import java.util.Locale

class AssistantActivity : AppCompatActivity() {

    private enum class UiState { SETUP_REQUIRED, RECORDING, TRANSCRIBING, REVIEW, SENDING, SENT }

    private data class ControlSpec(
        @DrawableRes val iconRes: Int,
        @StringRes val descriptionRes: Int,
        val action: () -> Unit
    )

    private data class ControlSet(
        val left: ControlSpec?,
        val center: ControlSpec?,
        val right: ControlSpec?
    )

    private lateinit var binding: ActivityAssistantBinding
    private lateinit var prefs: ServerPreferences
    private val api = LocalWhisperApi()
    private var recorder: WavAudioRecorder? = null
    private var activeJob: Job? = null
    private var clientsJob: Job? = null
    private var recordingStartMs: Long = 0L
    private var currentState = UiState.SETUP_REQUIRED
    private var connectedClients: List<PcClientInfo> = emptyList()
    private var selectedClient: PcClientInfo? = null
    private var clientsLoaded = false
    private var sentClientName = ""
    private var isDismissing = false
    private var isKeyboardMode = false

    private val timerHandler = Handler(Looper.getMainLooper())
    private val timerRunnable = object : Runnable {
        override fun run() {
            updateRecordingProgress()
            timerHandler.postDelayed(this, TIMER_TICK_MS)
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        WindowCompat.setDecorFitsSystemWindows(window, false)
        binding = ActivityAssistantBinding.inflate(layoutInflater)
        setContentView(binding.root)
        prefs = ServerPreferences(this)

        applyWindowInsets()
        setupStaticListeners()
        setupRecordingSlider()
        setupAhkChips()
        animateContentIn()
        isKeyboardMode = prefs.startInKeyboardMode
        beginAssistantFlow()
    }

    override fun onDestroy() {
        super.onDestroy()
        stopTimer()
        binding.waveformView.setActive(false)
        activeJob?.cancel()
        clientsJob?.cancel()
        recorder?.cancel()
    }

    // ------------------------------------------------------------ Setup

    private fun applyWindowInsets() {
        val baseMargin = dpToPx(12)
        ViewCompat.setOnApplyWindowInsetsListener(binding.overlayScrim) { _, insets ->
            val bars = insets.getInsets(WindowInsetsCompat.Type.systemBars())
            val ime = insets.getInsets(WindowInsetsCompat.Type.ime())
            val params = binding.sheetCard.layoutParams as ViewGroup.MarginLayoutParams
            params.marginStart = baseMargin + bars.left
            params.marginEnd = baseMargin + bars.right
            params.bottomMargin = baseMargin + maxOf(bars.bottom, ime.bottom)
            binding.sheetCard.layoutParams = params
            insets
        }
    }

    private fun setupStaticListeners() {
        binding.overlayScrim.setOnClickListener { dismissOverlay() }
        binding.closeButton.setOnClickListener { dismissOverlay() }
        binding.openSettingsButton.setOnClickListener { openMainScreen() }
        binding.openMainButton.setOnClickListener { openMainScreen() }
        binding.targetPcText.setOnClickListener { showTargetPicker() }
        binding.keyboardModeButton.setOnClickListener {
            performTapHaptic(it)
            toggleInputMode()
        }
    }

    @SuppressLint("ClickableViewAccessibility")
    private fun setupRecordingSlider() {
        val slider = binding.recordingSlider
        slider.valueFrom = 0f
        slider.valueTo = WavAudioRecorder.MAX_RECORDING_MS / 1000f
        slider.value = 0f
        // Display-only progress: swallow touches so the user cannot drag it.
        slider.setOnTouchListener { _, _ -> true }
        binding.maxTimeText.text = formatClock(WavAudioRecorder.MAX_RECORDING_MS / 1000L)
    }

    private fun setupAhkChips() {
        binding.ahkChipGroup.removeAllViews()
        val chipBg = ColorStateList.valueOf(ContextCompat.getColor(this, R.color.lw_pill_bg))
        val chipText = ContextCompat.getColor(this, R.color.lw_text_primary)

        for (token in AHK_QUICK_TOKENS) {
            val chip = Chip(this).apply {
                text = token
                typeface = Typeface.MONOSPACE
                textSize = 13f
                isCheckable = false
                chipBackgroundColor = chipBg
                chipStrokeWidth = 0f
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

    private fun animateContentIn() {
        binding.sheetCard.alpha = 0f
        binding.sheetCard.translationY = dpToPx(96).toFloat()
        binding.sheetCard.animate()
            .alpha(1f)
            .translationY(0f)
            .setDuration(260L)
            .setInterpolator(DecelerateInterpolator(2.0f))
            .start()
    }

    private fun insertTokenAtCursor(token: String) {
        val editText = binding.transcriptInput
        val editable = editText.text ?: return
        val start = editText.selectionStart.coerceAtLeast(0)
        val end = editText.selectionEnd.coerceAtLeast(0)
        editable.replace(minOf(start, end), maxOf(start, end), token)
    }

    // ------------------------------------------------------------ Flow

    private fun beginAssistantFlow() {
        if (prefs.buildBaseUrl() == null) {
            showSetupRequired(getString(R.string.invalid_server_config))
            return
        }
        if (isKeyboardMode) {
            enterKeyboardMode()
            return
        }
        if (!hasRecordAudioPermission()) {
            showSetupRequired(getString(R.string.assistant_mic_required))
            return
        }
        startRecording()
    }

    // ------------------------------------------------------------ Input mode

    private fun toggleInputMode() {
        if (isKeyboardMode) enterVoiceMode() else enterKeyboardMode()
    }

    /** Stops any recording and lets the user type the text to send instead of dictating it. */
    private fun enterKeyboardMode() {
        if (prefs.buildBaseUrl() == null) {
            showSetupRequired(getString(R.string.invalid_server_config))
            return
        }
        // Keep an existing transcript so it can be edited by hand; otherwise start empty.
        val keepText = currentState == UiState.REVIEW
        isKeyboardMode = true
        stopTimer()
        binding.waveformView.setActive(false)
        activeJob?.cancel()
        recorder?.cancel()
        recorder = null
        clearError()
        if (!keepText) {
            binding.transcriptInput.setText("")
        }
        refreshClientsInBackground()
        setUiState(UiState.REVIEW)
        showKeyboard()
    }

    private fun enterVoiceMode() {
        isKeyboardMode = false
        hideKeyboard()
        if (!hasRecordAudioPermission()) {
            showSetupRequired(getString(R.string.assistant_mic_required))
            return
        }
        startRecording()
    }

    private fun showKeyboard() {
        val input = binding.transcriptInput
        input.requestFocus()
        input.setSelection(input.text?.length ?: 0)
        // Posted so the field is laid out and focused before the IME is requested.
        input.post {
            WindowCompat.getInsetsController(window, input).show(WindowInsetsCompat.Type.ime())
        }
    }

    private fun hideKeyboard() {
        val input = binding.transcriptInput
        input.clearFocus()
        WindowCompat.getInsetsController(window, input).hide(WindowInsetsCompat.Type.ime())
    }

    private fun startRecording() {
        clearError()
        activeJob?.cancel()
        recorder?.cancel()
        binding.waveformView.reset()
        refreshClientsInBackground()

        val tempWav = File(cacheDir, RECORDING_FILE_NAME)
        val newRecorder = WavAudioRecorder(
            context = this,
            onAmplitude = { level -> binding.waveformView.pushAmplitude(level) },
            onMaxDurationReached = { runOnUiThread { stopAndTranscribe() } }
        )
        val startResult = newRecorder.start(tempWav)
        if (startResult.isFailure) {
            val message = startResult.exceptionOrNull()?.message
                ?: getString(R.string.assistant_mic_start_failed)
            showError(message)
            setUiState(UiState.REVIEW)
            return
        }

        recorder = newRecorder
        recordingStartMs = System.currentTimeMillis()
        updateRecordingProgress()
        binding.waveformView.setActive(true)
        startTimer()
        setUiState(UiState.RECORDING)
    }

    private fun stopAndTranscribe() {
        if (currentState != UiState.RECORDING) return
        stopTimer()
        binding.waveformView.setActive(false)
        val wavFile = recorder?.stop()
        if (wavFile == null || !wavFile.isFile) {
            showError(getString(R.string.assistant_no_audio))
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
        refreshClientsInBackground()

        activeJob?.cancel()
        activeJob = lifecycleScope.launch {
            val result = api.transcribeAudio(baseUrl, wavFile, prefs.language)
            deleteRecording(wavFile)
            result.onSuccess { response ->
                binding.transcriptInput.setText(response.text)
                binding.transcriptInput.setSelection(response.text.length)
                setUiState(UiState.REVIEW)
            }.onFailure { exc ->
                showError(exc.message ?: getString(R.string.assistant_transcription_failed))
                setUiState(UiState.REVIEW)
            }
        }
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
        hideKeyboard()
        setUiState(UiState.SENDING)

        activeJob?.cancel()
        activeJob = lifecycleScope.launch {
            val result = api.sendKeystrokes(baseUrl, text, selectedClient?.id)
            result.onSuccess { response ->
                showSentAndDismiss(response.clientName)
            }.onFailure { exc ->
                showError(exc.message ?: getString(R.string.assistant_send_failed))
                setUiState(UiState.REVIEW)
            }
        }
    }

    private suspend fun showSentAndDismiss(clientName: String) {
        sentClientName = clientName
        setUiState(UiState.SENT)
        binding.sentCheckIcon.scaleX = 0.4f
        binding.sentCheckIcon.scaleY = 0.4f
        binding.sentCheckIcon.animate()
            .scaleX(1f)
            .scaleY(1f)
            .setDuration(260L)
            .setInterpolator(OvershootInterpolator(2.0f))
            .start()
        delay(SENT_DISMISS_DELAY_MS)
        dismissOverlay()
    }

    private fun deleteRecording(file: File) {
        if (file.exists() && !file.delete()) {
            Log.w(TAG, "Could not delete temporary recording ${file.absolutePath}")
        }
    }

    // ------------------------------------------------------------ PC clients

    private fun refreshClientsInBackground() {
        val baseUrl = prefs.buildBaseUrl() ?: return
        clientsJob?.cancel()
        clientsJob = lifecycleScope.launch { refreshConnectedClients(baseUrl) }
    }

    private suspend fun refreshConnectedClients(baseUrl: String) {
        api.fetchClients(baseUrl)
            .onSuccess { clients ->
                connectedClients = clients
                selectPreferredClient()
            }
            .onFailure { exc ->
                Log.w(TAG, "Could not fetch PC clients", exc)
                connectedClients = emptyList()
                selectedClient = null
            }
        clientsLoaded = true
        renderSubtitle()
    }

    private fun selectPreferredClient() {
        if (connectedClients.isEmpty()) {
            selectedClient = null
            return
        }
        val savedId = prefs.selectedClientId
        selectedClient = connectedClients.firstOrNull { it.id == savedId } ?: connectedClients.last()
    }

    private fun showTargetPicker() {
        if (connectedClients.isEmpty()) {
            Toast.makeText(this, R.string.assistant_no_pc_connected, Toast.LENGTH_SHORT).show()
            refreshClientsInBackground()
            return
        }
        val names = connectedClients.map { it.name }.toTypedArray()
        val checkedIndex = connectedClients.indexOfFirst { it.id == selectedClient?.id }
        MaterialAlertDialogBuilder(this)
            .setTitle(R.string.assistant_pick_target)
            .setSingleChoiceItems(names, checkedIndex) { dialog, which ->
                selectClient(which)
                dialog.dismiss()
            }
            .setNegativeButton(R.string.assistant_cancel, null)
            .show()
    }

    private fun selectClient(index: Int) {
        val chosen = connectedClients.getOrNull(index) ?: return
        selectedClient = chosen
        prefs.selectedClientId = chosen.id
        renderSubtitle()
    }

    // ------------------------------------------------------------ Rendering

    private fun setUiState(state: UiState) {
        currentState = state
        renderArtwork(state)
        renderBottomSections(state)
        binding.stateLabel.text = getString(stateLabelRes(state))
        renderSubtitle()
        configureControls(state)
        renderModeToggle(state)
        binding.transcriptInput.isEnabled = state != UiState.SENDING
    }

    private fun renderModeToggle(state: UiState) {
        val button = binding.keyboardModeButton
        val available = when (state) {
            UiState.RECORDING, UiState.REVIEW -> true
            // Lets the user type when only the mic permission is missing.
            UiState.SETUP_REQUIRED -> !isKeyboardMode && prefs.buildBaseUrl() != null
            UiState.TRANSCRIBING, UiState.SENDING, UiState.SENT -> false
        }
        button.visibility = if (available) View.VISIBLE else View.GONE
        button.setIconResource(if (isKeyboardMode) R.drawable.ic_mic else R.drawable.ic_keyboard)
        button.contentDescription = getString(
            if (isKeyboardMode) R.string.assistant_voice_mode_desc else R.string.assistant_keyboard_mode_desc
        )
    }

    private fun renderArtwork(state: UiState) {
        binding.waveformView.visibility = visibleIf(state == UiState.RECORDING)
        binding.transcribingSpinner.visibility = visibleIf(state == UiState.TRANSCRIBING)
        binding.transcriptInput.visibility =
            visibleIf(state == UiState.REVIEW || state == UiState.SENDING)
        binding.setupRequiredSection.visibility = visibleIf(state == UiState.SETUP_REQUIRED)
        binding.sentCheckIcon.visibility = visibleIf(state == UiState.SENT)
    }

    private fun renderBottomSections(state: UiState) {
        binding.progressSection.visibility = visibleIf(state == UiState.RECORDING)
        binding.chipScroll.visibility =
            visibleIf(state == UiState.REVIEW || state == UiState.SENDING)
    }

    private fun renderSubtitle() {
        val state = currentState
        val subtitle = binding.targetPcText
        subtitle.isClickable = state == UiState.RECORDING || state == UiState.REVIEW
        subtitle.visibility = if (state == UiState.SETUP_REQUIRED) View.INVISIBLE else View.VISIBLE
        subtitle.text = when (state) {
            UiState.SETUP_REQUIRED -> ""
            UiState.TRANSCRIBING -> getString(R.string.assistant_model_hint)
            UiState.SENT -> getString(R.string.assistant_sent_message, sentClientName)
            UiState.RECORDING, UiState.REVIEW, UiState.SENDING -> targetDescription()
        }
    }

    private fun targetDescription(): String {
        val client = selectedClient
        return when {
            client != null -> getString(R.string.assistant_target_format, client.name)
            clientsLoaded -> getString(R.string.assistant_no_pc_connected)
            else -> getString(R.string.assistant_target_loading)
        }
    }

    @StringRes
    private fun stateLabelRes(state: UiState): Int = when (state) {
        UiState.SETUP_REQUIRED -> R.string.assistant_state_error
        UiState.RECORDING -> R.string.assistant_state_listening
        UiState.TRANSCRIBING -> R.string.assistant_state_transcribing
        UiState.REVIEW ->
            if (isKeyboardMode) R.string.assistant_state_typing else R.string.assistant_state_review
        UiState.SENDING -> R.string.assistant_state_sending
        UiState.SENT -> R.string.assistant_state_sent
    }

    private fun configureControls(state: UiState) {
        val controls = controlsFor(state)
        bindControl(binding.controlLeft, controls.left)
        bindControl(binding.controlCenter, controls.center)
        bindControl(binding.controlRight, controls.right)

        val enabled = state != UiState.SENDING
        for (button in listOf(binding.controlLeft, binding.controlCenter, binding.controlRight)) {
            button.isEnabled = enabled
            button.alpha = if (enabled) 1f else DISABLED_ALPHA
        }
    }

    private fun controlsFor(state: UiState): ControlSet {
        val close = ControlSpec(R.drawable.ic_close, R.string.assistant_cancel) { dismissOverlay() }
        val restart = ControlSpec(R.drawable.ic_restart, R.string.assistant_restart_desc) { startRecording() }
        val dictate = ControlSpec(R.drawable.ic_mic, R.string.assistant_voice_mode_desc) { enterVoiceMode() }
        return when (state) {
            UiState.RECORDING -> ControlSet(
                left = close,
                center = ControlSpec(R.drawable.ic_stop, R.string.assistant_stop_desc) { stopAndTranscribe() },
                right = restart
            )
            UiState.REVIEW, UiState.SENDING -> ControlSet(
                left = if (isKeyboardMode) dictate else restart,
                center = ControlSpec(R.drawable.ic_send, R.string.assistant_send_to_pc) { sendTranscriptToPc() },
                right = close
            )
            UiState.TRANSCRIBING, UiState.SETUP_REQUIRED -> ControlSet(null, close, null)
            UiState.SENT -> ControlSet(null, null, null)
        }
    }

    private fun bindControl(button: MaterialButton, spec: ControlSpec?) {
        if (spec == null) {
            button.visibility = View.INVISIBLE
            button.setOnClickListener(null)
            return
        }
        button.visibility = View.VISIBLE
        button.setIconResource(spec.iconRes)
        button.contentDescription = getString(spec.descriptionRes)
        button.setOnClickListener {
            performTapHaptic(it)
            spec.action()
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

    // ------------------------------------------------------------ Timer

    private fun startTimer() {
        stopTimer()
        timerHandler.post(timerRunnable)
    }

    private fun stopTimer() {
        timerHandler.removeCallbacks(timerRunnable)
    }

    private fun updateRecordingProgress() {
        val elapsedMs = (System.currentTimeMillis() - recordingStartMs).coerceAtLeast(0L)
        binding.timerText.text = formatClock(elapsedMs / 1000L)
        val slider = binding.recordingSlider
        slider.value = (elapsedMs / 1000f).coerceIn(slider.valueFrom, slider.valueTo)
    }

    private fun formatClock(totalSeconds: Long): String {
        val safeSeconds = totalSeconds.coerceAtLeast(0L)
        return String.format(Locale.US, "%d:%02d", safeSeconds / 60L, safeSeconds % 60L)
    }

    // ------------------------------------------------------------ Navigation

    private fun openMainScreen() {
        // NEW_TASK puts settings in the app's normal task. Without it, MainActivity joined this
        // overlay's hidden task and reappeared behind the overlay on the next assistant launch.
        val intent = Intent(this, MainActivity::class.java).apply {
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        }
        startActivity(intent)
        finish()
    }

    private fun dismissOverlay() {
        if (isDismissing) return
        isDismissing = true
        stopTimer()
        binding.waveformView.setActive(false)
        activeJob?.cancel()
        clientsJob?.cancel()
        recorder?.cancel()
        recorder = null
        binding.sheetCard.animate()
            .alpha(0f)
            .translationY((binding.sheetCard.height + dpToPx(24)).toFloat())
            .setDuration(200L)
            .setInterpolator(DecelerateInterpolator())
            .withEndAction {
                finish()
                @Suppress("DEPRECATION")
                overridePendingTransition(0, 0)
            }
            .start()
    }

    // ------------------------------------------------------------ Helpers

    private fun visibleIf(condition: Boolean): Int = if (condition) View.VISIBLE else View.GONE

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
        private const val TAG = "LW-Assistant"
        private const val TIMER_TICK_MS = 250L
        private const val SENT_DISMISS_DELAY_MS = 650L
        private const val DISABLED_ALPHA = 0.4f
        private const val RECORDING_FILE_NAME = "localwhisper_recording.wav"
        private val AHK_QUICK_TOKENS = listOf(
            "{Enter}",
            "{Tab}",
            "{Backspace}",
            "{Esc}",
            "{^a}",
            "{^c}",
            "{^v}"
        )
    }
}
