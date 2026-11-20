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

    private enum class UiState {
        SETUP_REQUIRED,
        RECORDING,
        TRANSCRIBING,
        REVIEW,
        SENDING,
        AGENT_THINKING,
        AGENT_RESULT,
        SENT
    }

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
    private val localTarget by lazy {
        PcClientInfo(id = LOCAL_TARGET_ID, name = getString(R.string.local_target_name))
    }
    private var clientsLoaded = false
    private var sentClientName = ""
    private var isDismissing = false
    private var isKeyboardMode = false
    private var isAgentMode = false
    private var isThinkingExpanded = false
    private var agentSessionId: String? = null
    private var activeConfirmation: PendingConfirmationInfo? = null

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
        if (prefs.selectedClientId == LOCAL_TARGET_ID) {
            selectedClient = localTarget
        }

        applyWindowInsets()
        setupStaticListeners()
        setupRecordingSlider()
        setupAhkChips()
        animateContentIn()
        isKeyboardMode = prefs.startInKeyboardMode
        isAgentMode = prefs.startInAgentMode
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
        binding.agentModeButton.setOnClickListener {
            performTapHaptic(it)
            toggleAgentMode()
        }
        binding.thinkingDrawerCard.setOnClickListener {
            performTapHaptic(it)
            toggleThinkingDrawer()
        }
        binding.approveConfirmButton.setOnClickListener {
            performTapHaptic(it)
            submitAgentConfirmation(approved = true)
        }
        binding.rejectConfirmButton.setOnClickListener {
            performTapHaptic(it)
            submitAgentConfirmation(approved = false)
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

    private fun toggleAgentMode() {
        isAgentMode = !isAgentMode
        renderAgentModeButton(currentState)
        renderSubtitle()
        renderBottomSections(currentState)
        updateTranscriptHint()
        if (currentState == UiState.AGENT_RESULT && !isAgentMode) {
            setUiState(UiState.REVIEW)
        }
    }

    private fun toggleThinkingDrawer() {
        isThinkingExpanded = !isThinkingExpanded
        binding.thinkingBodyText.visibility = visibleIf(isThinkingExpanded)
        binding.thinkingHeaderText.setText(
            if (isThinkingExpanded) {
                R.string.assistant_thinking_header_expanded
            } else {
                R.string.assistant_thinking_header_collapsed
            }
        )
    }

    private fun updateTranscriptHint() {
        binding.transcriptInput.setHint(
            if (isAgentMode) R.string.assistant_agent_prompt_hint else R.string.assistant_transcript_hint
        )
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

    private fun sendTranscript() {
        val text = binding.transcriptInput.text?.toString().orEmpty()
        if (text.isEmpty()) {
            showError(getString(R.string.assistant_empty_transcript))
            return
        }
        if (isAgentMode) {
            sendPromptToAgent(text)
            return
        }
        if (isLocalTargetSelected()) {
            sendToPhone(text)
        } else {
            sendTranscriptToPc(text)
        }
    }

    private fun sendPromptToAgent(prompt: String) {
        val baseUrl = prefs.buildBaseUrl()
        if (baseUrl == null) {
            showError(getString(R.string.invalid_server_config))
            return
        }
        clearError()
        hideKeyboard()
        setUiState(UiState.AGENT_THINKING)

        val targetClientId = if (isLocalTargetSelected()) null else selectedClient?.id
        activeJob?.cancel()
        activeJob = lifecycleScope.launch {
            val result = api.runAgent(
                baseUrl = baseUrl,
                prompt = prompt,
                clientId = targetClientId,
                sessionId = agentSessionId
            )
            result.onSuccess { response ->
                handleAgentResponse(response)
            }.onFailure { exc ->
                showError(exc.message ?: getString(R.string.assistant_send_failed))
                setUiState(UiState.REVIEW)
            }
        }
    }

    private fun submitAgentConfirmation(approved: Boolean) {
        val baseUrl = prefs.buildBaseUrl() ?: return
        val sessionId = agentSessionId ?: return
        val pending = activeConfirmation ?: return

        clearError()
        setUiState(UiState.AGENT_THINKING)

        val targetClientId = if (isLocalTargetSelected()) null else selectedClient?.id
        activeJob?.cancel()
        activeJob = lifecycleScope.launch {
            val result = api.confirmAgentAction(
                baseUrl = baseUrl,
                sessionId = sessionId,
                confirmationId = pending.confirmationId,
                approved = approved,
                clientId = targetClientId
            )
            result.onSuccess { response ->
                handleAgentResponse(response)
            }.onFailure { exc ->
                showError(exc.message ?: getString(R.string.assistant_send_failed))
                setUiState(UiState.AGENT_RESULT)
            }
        }
    }

    private fun handleAgentResponse(response: AgentResponse) {
        if (response.sessionId.isNotEmpty()) {
            agentSessionId = response.sessionId
        }
        activeConfirmation = response.pendingConfirmation

        // Execute any native Android system intents (Idea #7)
        val androidOutcomes = AndroidActionExecutor.executeAll(this, response.androidActions)

        // Bind collapsible Thinking drawer (Idea #9)
        val hasThinking = response.thinking.isNotBlank()
        binding.thinkingDrawerCard.visibility = visibleIf(hasThinking)
        binding.thinkingBodyText.text = response.thinking
        isThinkingExpanded = false
        binding.thinkingBodyText.visibility = View.GONE
        binding.thinkingHeaderText.setText(R.string.assistant_thinking_header_collapsed)

        // Bind Tool Execution Status Pills (Idea #9)
        val allPills = ArrayList<AgentToolCallInfo>(response.toolCalls.size + androidOutcomes.size)
        allPills.addAll(response.toolCalls)
        for (outcome in androidOutcomes) {
            allPills.add(AgentToolCallInfo(tool = "android", status = "ok", summary = outcome))
        }
        populateToolPills(allPills)

        // Bind reply text
        binding.agentReplyText.text = response.reply.ifBlank { getString(R.string.assistant_state_sent) }

        // Bind Confirmation Gate card if a high-impact PC tool is waiting approval (Idea #10)
        val pending = response.pendingConfirmation
        if (pending != null) {
            binding.confirmationDescriptionText.text = pending.description
            binding.confirmationGateCard.visibility = View.VISIBLE
        } else {
            binding.confirmationGateCard.visibility = View.GONE
        }

        setUiState(UiState.AGENT_RESULT)
    }

    private fun populateToolPills(pills: List<AgentToolCallInfo>) {
        binding.toolPillsGroup.removeAllViews()
        if (pills.isEmpty()) {
            binding.toolPillsScroll.visibility = View.GONE
            return
        }
        binding.toolPillsScroll.visibility = View.VISIBLE
        val chipBg = ColorStateList.valueOf(ContextCompat.getColor(this, R.color.lw_pill_bg))
        for (pill in pills) {
            val prefix = when (pill.status) {
                "ok", "queued_on_phone" -> "✓ "
                "pending_confirmation" -> "⏳ "
                "rejected", "error" -> "✕ "
                else -> "• "
            }
            val textColorRes = when (pill.status) {
                "ok", "queued_on_phone" -> R.color.lw_success
                "pending_confirmation" -> R.color.lw_accent
                "rejected", "error" -> R.color.lw_error
                else -> R.color.lw_text_primary
            }
            val chip = Chip(this).apply {
                text = prefix + pill.summary
                textSize = 12f
                isCheckable = false
                isClickable = false
                chipBackgroundColor = chipBg
                chipStrokeWidth = 0f
                setTextColor(ContextCompat.getColor(context, textColorRes))
                setEnsureMinTouchTargetSize(false)
            }
            binding.toolPillsGroup.addView(chip)
        }
    }

    private fun startKeyboardFollowUp() {
        isKeyboardMode = true
        binding.transcriptInput.setText("")
        setUiState(UiState.REVIEW)
        showKeyboard()
    }

    private fun startVoiceFollowUp() {
        isKeyboardMode = false
        startRecording()
    }

    /** Hands the text to LocalTypingService, which types it once the overlay has closed. */
    private fun sendToPhone(text: String) {
        clearError()
        if (LocalTypingService.enqueue(text)) {
            hideKeyboard()
            activeJob?.cancel()
            activeJob = lifecycleScope.launch { showSentAndDismiss(localTarget.name) }
            return
        }
        val copied = LocalClipboard.copy(this, text)
        showError(
            getString(
                if (copied) R.string.local_typing_disabled_copied else R.string.local_clipboard_failed
            )
        )
    }

    private fun sendTranscriptToPc(text: String) {
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
            .onSuccess { clients -> connectedClients = clients }
            .onFailure { exc ->
                Log.w(TAG, "Could not fetch PC clients", exc)
                connectedClients = emptyList()
            }
        selectPreferredClient()
        clientsLoaded = true
        renderSubtitle()
        renderBottomSections(currentState)
    }

    /** Saved target if available, else the newest PC, else this phone. */
    private fun selectPreferredClient() {
        val savedId = prefs.selectedClientId
        selectedClient = if (savedId == LOCAL_TARGET_ID) {
            localTarget
        } else {
            connectedClients.firstOrNull { it.id == savedId }
                ?: connectedClients.lastOrNull()
                ?: localTarget
        }
    }

    private fun showTargetPicker() {
        val targets = listOf(localTarget) + connectedClients
        val names = targets.map { it.name }.toTypedArray()
        val checkedIndex = targets.indexOfFirst { it.id == selectedClient?.id }
        MaterialAlertDialogBuilder(this)
            .setTitle(R.string.assistant_pick_target)
            .setSingleChoiceItems(names, checkedIndex) { dialog, which ->
                selectTarget(targets.getOrNull(which))
                dialog.dismiss()
            }
            .setNegativeButton(R.string.assistant_cancel, null)
            .show()
        if (connectedClients.isEmpty()) {
            refreshClientsInBackground()
        }
    }

    private fun selectTarget(target: PcClientInfo?) {
        if (target == null) return
        selectedClient = target
        prefs.selectedClientId = target.id
        renderSubtitle()
        renderBottomSections(currentState)
    }

    private fun isLocalTargetSelected(): Boolean = selectedClient?.id == LOCAL_TARGET_ID

    // ------------------------------------------------------------ Rendering

    private fun setUiState(state: UiState) {
        currentState = state
        renderArtwork(state)
        renderBottomSections(state)
        binding.stateLabel.text = getString(stateLabelRes(state))
        renderSubtitle()
        configureControls(state)
        renderModeToggle(state)
        renderAgentModeButton(state)
        updateTranscriptHint()
        binding.transcriptInput.isEnabled = state != UiState.SENDING && state != UiState.AGENT_THINKING
    }

    private fun renderAgentModeButton(state: UiState) {
        val button = binding.agentModeButton
        val available = when (state) {
            UiState.RECORDING, UiState.REVIEW, UiState.AGENT_RESULT -> true
            UiState.SETUP_REQUIRED -> prefs.buildBaseUrl() != null
            UiState.TRANSCRIBING, UiState.SENDING, UiState.AGENT_THINKING, UiState.SENT -> false
        }
        button.visibility = if (available) View.VISIBLE else View.GONE
        val tintRes = if (isAgentMode) R.color.lw_accent else R.color.lw_text_secondary
        button.iconTint = ColorStateList.valueOf(ContextCompat.getColor(this, tintRes))
    }

    private fun renderModeToggle(state: UiState) {
        val button = binding.keyboardModeButton
        val available = when (state) {
            UiState.RECORDING, UiState.REVIEW -> true
            // Lets the user type when only the mic permission is missing.
            UiState.SETUP_REQUIRED -> !isKeyboardMode && prefs.buildBaseUrl() != null
            UiState.TRANSCRIBING, UiState.SENDING, UiState.AGENT_THINKING, UiState.AGENT_RESULT, UiState.SENT -> false
        }
        button.visibility = if (available) View.VISIBLE else View.GONE
        button.setIconResource(if (isKeyboardMode) R.drawable.ic_mic else R.drawable.ic_keyboard)
        button.contentDescription = getString(
            if (isKeyboardMode) R.string.assistant_voice_mode_desc else R.string.assistant_keyboard_mode_desc
        )
    }

    private fun renderArtwork(state: UiState) {
        binding.waveformView.visibility = visibleIf(state == UiState.RECORDING)
        binding.transcribingSpinner.visibility =
            visibleIf(state == UiState.TRANSCRIBING || state == UiState.AGENT_THINKING)
        binding.transcriptInput.visibility =
            visibleIf(state == UiState.REVIEW || state == UiState.SENDING)
        binding.agentResultScroll.visibility = visibleIf(state == UiState.AGENT_RESULT)
        binding.setupRequiredSection.visibility = visibleIf(state == UiState.SETUP_REQUIRED)
        binding.sentCheckIcon.visibility = visibleIf(state == UiState.SENT)
    }

    private fun renderBottomSections(state: UiState) {
        binding.progressSection.visibility = visibleIf(state == UiState.RECORDING)
        // AHK key tokens only mean something in raw PC dictation mode.
        binding.chipScroll.visibility = visibleIf(
            (state == UiState.REVIEW || state == UiState.SENDING) &&
                !isLocalTargetSelected() &&
                !isAgentMode
        )
    }

    private fun renderSubtitle() {
        val state = currentState
        val subtitle = binding.targetPcText
        subtitle.isClickable =
            state == UiState.RECORDING || state == UiState.REVIEW || state == UiState.AGENT_RESULT
        subtitle.visibility = if (state == UiState.SETUP_REQUIRED) View.INVISIBLE else View.VISIBLE
        subtitle.text = when (state) {
            UiState.SETUP_REQUIRED -> ""
            UiState.TRANSCRIBING -> getString(R.string.assistant_model_hint)
            UiState.AGENT_THINKING -> getString(R.string.assistant_agent_thinking_hint)
            UiState.SENT -> getString(R.string.assistant_sent_message, sentClientName)
            UiState.RECORDING, UiState.REVIEW, UiState.SENDING, UiState.AGENT_RESULT -> targetDescription()
        }
    }

    private fun targetDescription(): String {
        val client = selectedClient
        val formatRes =
            if (isAgentMode) R.string.assistant_agent_target_format else R.string.assistant_target_format
        return when {
            client != null -> getString(formatRes, client.name)
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
        UiState.AGENT_THINKING -> R.string.assistant_state_thinking
        UiState.AGENT_RESULT ->
            if (activeConfirmation != null) R.string.assistant_state_confirm else R.string.assistant_state_agent_result
        UiState.SENT -> R.string.assistant_state_sent
    }

    private fun configureControls(state: UiState) {
        val controls = controlsFor(state)
        bindControl(binding.controlLeft, controls.left)
        bindControl(binding.controlCenter, controls.center)
        bindControl(binding.controlRight, controls.right)

        val enabled = state != UiState.SENDING && state != UiState.AGENT_THINKING
        for (button in listOf(binding.controlLeft, binding.controlCenter, binding.controlRight)) {
            button.isEnabled = enabled
            button.alpha = if (enabled) 1f else DISABLED_ALPHA
        }
    }

    private fun controlsFor(state: UiState): ControlSet {
        val close = ControlSpec(R.drawable.ic_close, R.string.assistant_cancel) { dismissOverlay() }
        val restart = ControlSpec(R.drawable.ic_restart, R.string.assistant_restart_desc) { startRecording() }
        val dictate = ControlSpec(R.drawable.ic_mic, R.string.assistant_voice_mode_desc) { enterVoiceMode() }
        val followUpVoice = ControlSpec(R.drawable.ic_mic, R.string.assistant_follow_up_voice_desc) {
            startVoiceFollowUp()
        }
        val followUpKeyboard = ControlSpec(R.drawable.ic_keyboard, R.string.assistant_follow_up_keyboard_desc) {
            startKeyboardFollowUp()
        }
        return when (state) {
            UiState.RECORDING -> ControlSet(
                left = close,
                center = ControlSpec(R.drawable.ic_stop, R.string.assistant_stop_desc) { stopAndTranscribe() },
                right = restart
            )
            UiState.REVIEW, UiState.SENDING -> ControlSet(
                left = if (isKeyboardMode) dictate else restart,
                center = ControlSpec(R.drawable.ic_send, R.string.assistant_send_to_pc) { sendTranscript() },
                right = close
            )
            UiState.AGENT_RESULT -> ControlSet(
                left = followUpKeyboard,
                center = followUpVoice,
                right = close
            )
            UiState.TRANSCRIBING, UiState.AGENT_THINKING, UiState.SETUP_REQUIRED -> ControlSet(null, close, null)
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
        private const val LOCAL_TARGET_ID = "local-phone"
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
