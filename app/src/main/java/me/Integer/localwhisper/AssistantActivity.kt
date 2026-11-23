package me.Integer.localwhisper

import android.Manifest
import android.annotation.SuppressLint
import android.content.Intent
import android.content.pm.PackageManager
import android.content.res.ColorStateList
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.Typeface
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.util.Base64
import android.util.Log
import android.view.HapticFeedbackConstants
import android.view.View
import android.view.ViewGroup
import android.view.animation.DecelerateInterpolator
import android.view.animation.OvershootInterpolator
import android.widget.HorizontalScrollView
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.TextView
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
import com.google.android.material.chip.ChipGroup
import com.google.android.material.dialog.MaterialAlertDialogBuilder
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
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

    private data class LiveStreamingTurnViews(
        val thinkingCard: LinearLayout,
        val thinkingHeader: TextView,
        val thinkingBody: TextView,
        val toolScroll: HorizontalScrollView,
        val toolGroup: ChipGroup,
        val replyBox: TextView,
        var userToggledThinking: Boolean = false,
        var isThinkingExpanded: Boolean = true
    )

    private lateinit var binding: ActivityAssistantBinding
    private lateinit var prefs: ServerPreferences
    private lateinit var sessionStore: AgentSessionStore
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
    private var agentSessionId: String? = null
    private var lastSubmittedPrompt: String = ""
    private var activeConfirmation: PendingConfirmationInfo? = null
    private var liveTurnViews: LiveStreamingTurnViews? = null

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
        sessionStore = AgentSessionStore(this)
        agentSessionId = sessionStore.resolveInitialSessionId(prefs.agentResumeTimeoutSeconds)
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

    override fun onStop() {
        super.onStop()
        sessionStore.recordOverlayClosed()
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
        binding.sessionSelectorText.setOnClickListener {
            performTapHaptic(it)
            showSessionPickerDialog()
        }
        binding.viewChatHistoryButton.setOnClickListener {
            performTapHaptic(it)
            openActiveSessionChatView()
        }
        binding.newSessionButton.setOnClickListener {
            performTapHaptic(it)
            startFreshAgentSession()
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
        renderSessionBar(currentState)
        renderSubtitle()
        renderBottomSections(currentState)
        updateTranscriptHint()
        if (currentState == UiState.AGENT_RESULT && !isAgentMode) {
            setUiState(UiState.REVIEW)
        }
    }

    private fun updateTranscriptHint() {
        binding.transcriptInput.setHint(
            if (isAgentMode) R.string.assistant_agent_prompt_hint else R.string.assistant_transcript_hint
        )
    }

    private fun showSessionPickerDialog() {
        val sessions = sessionStore.listSessions()
        val items = ArrayList<String>(sessions.size + 1)
        items.add(getString(R.string.assistant_session_picker_new))
        for (session in sessions) {
            items.add(
                getString(
                    R.string.assistant_session_picker_item_format,
                    session.title,
                    session.turns.size
                )
            )
        }
        val activeIndex = sessions.indexOfFirst { it.sessionId == agentSessionId }
        val checkedItem = if (activeIndex >= 0) activeIndex + 1 else 0

        val builder = MaterialAlertDialogBuilder(this)
            .setTitle(R.string.assistant_session_picker_title)
            .setSingleChoiceItems(items.toTypedArray(), checkedItem) { dialog, which ->
                dialog.dismiss()
                if (which == 0) {
                    startFreshAgentSession()
                } else {
                    val chosen = sessions.getOrNull(which - 1)
                    if (chosen != null) {
                        selectExistingAgentSession(chosen)
                    }
                }
            }
            .setNegativeButton(R.string.assistant_cancel, null)

        if (sessions.isNotEmpty()) {
            builder.setNeutralButton(R.string.assistant_session_clear_all) { _, _ ->
                sessionStore.clearAll()
                startFreshAgentSession()
            }
        }
        builder.show()
    }

    private fun startFreshAgentSession() {
        agentSessionId = sessionStore.startNewSession()
        activeConfirmation = null
        binding.chatHistoryContainer.removeAllViews()
        binding.confirmationGateCard.visibility = View.GONE
        if (currentState == UiState.AGENT_RESULT) {
            if (isKeyboardMode) {
                enterKeyboardMode()
            } else {
                enterVoiceMode()
            }
        } else {
            renderSessionBar(currentState)
        }
    }

    private fun selectExistingAgentSession(session: SavedAgentSession) {
        stopTimer()
        binding.waveformView.setActive(false)
        activeJob?.cancel()
        recorder?.cancel()
        recorder = null
        hideKeyboard()
        clearError()

        isAgentMode = true
        agentSessionId = session.sessionId
        sessionStore.activeSessionId = session.sessionId
        activeConfirmation = null
        binding.confirmationGateCard.visibility = View.GONE
        renderChatHistoryThread(session)
        setUiState(UiState.AGENT_RESULT)
    }

    private fun openActiveSessionChatView() {
        val session = sessionStore.getSession(agentSessionId) ?: return
        selectExistingAgentSession(session)
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
        lastSubmittedPrompt = prompt
        val existingSession = sessionStore.getSession(agentSessionId)
        val existingTurns = existingSession?.turns.orEmpty()
        prepareLiveStreamingTurn(existingTurns, userPrompt = prompt, isConfirmationResume = false)
        setUiState(UiState.AGENT_THINKING)

        val targetClientId = if (isLocalTargetSelected()) null else selectedClient?.id
        val phoneScreenSnapshot = LocalTypingService.capturePhoneScreenSnapshot()
        activeJob?.cancel()
        activeJob = lifecycleScope.launch {
            val result = api.runAgentStream(
                baseUrl = baseUrl,
                prompt = prompt,
                clientId = targetClientId,
                sessionId = agentSessionId,
                priorTurns = existingTurns,
                phoneScreen = phoneScreenSnapshot,
                onProgress = { progress ->
                    withContext(Dispatchers.Main) {
                        updateLiveStreamingTurn(progress)
                    }
                }
            )
            result.onSuccess { response ->
                liveTurnViews = null
                handleAgentResponse(response, isConfirmationResume = false)
            }.onFailure { exc ->
                liveTurnViews = null
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
        binding.confirmationGateCard.visibility = View.GONE
        val existingTurns = sessionStore.getSession(sessionId)?.turns.orEmpty()
        val lastPrompt = existingTurns.lastOrNull()?.userPrompt.orEmpty()
        lastSubmittedPrompt = lastPrompt
        prepareLiveStreamingTurn(
            existingTurns = if (existingTurns.isNotEmpty()) existingTurns.dropLast(1) else emptyList(),
            userPrompt = lastPrompt,
            isConfirmationResume = true,
            initialThinking = existingTurns.lastOrNull()?.thinking.orEmpty(),
            initialTools = existingTurns.lastOrNull()?.toolCalls.orEmpty()
        )
        setUiState(UiState.AGENT_THINKING)

        val targetClientId = if (isLocalTargetSelected()) null else selectedClient?.id
        activeJob?.cancel()
        activeJob = lifecycleScope.launch {
            val result = api.confirmAgentActionStream(
                baseUrl = baseUrl,
                sessionId = sessionId,
                confirmationId = pending.confirmationId,
                approved = approved,
                clientId = targetClientId,
                onProgress = { progress ->
                    withContext(Dispatchers.Main) {
                        updateLiveStreamingTurn(progress)
                    }
                }
            )
            result.onSuccess { response ->
                liveTurnViews = null
                handleAgentResponse(response, isConfirmationResume = true)
            }.onFailure { exc ->
                liveTurnViews = null
                showError(exc.message ?: getString(R.string.assistant_send_failed))
                setUiState(UiState.AGENT_RESULT)
            }
        }
    }

    private fun prepareLiveStreamingTurn(
        existingTurns: List<AgentChatTurn>,
        userPrompt: String,
        isConfirmationResume: Boolean,
        initialThinking: String = "",
        initialTools: List<AgentToolCallInfo> = emptyList()
    ) {
        val container = binding.chatHistoryContainer
        container.removeAllViews()
        for ((index, turn) in existingTurns.withIndex()) {
            container.addView(buildTurnView(turn, isFirst = index == 0))
        }

        val isFirst = existingTurns.isEmpty()
        val turnWrapper = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            layoutParams = LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT
            ).apply {
                if (!isFirst) topMargin = dpToPx(12)
            }
        }

        if (userPrompt.isNotBlank()) {
            val userBox = LinearLayout(this).apply {
                orientation = LinearLayout.VERTICAL
                setBackgroundResource(R.drawable.bg_status_pill)
                setPadding(dpToPx(14), dpToPx(8), dpToPx(14), dpToPx(8))
                layoutParams = LinearLayout.LayoutParams(
                    ViewGroup.LayoutParams.MATCH_PARENT,
                    ViewGroup.LayoutParams.WRAP_CONTENT
                ).apply {
                    bottomMargin = dpToPx(6)
                }
            }
            val userLabel = TextView(this).apply {
                text = getString(R.string.assistant_you_label)
                textSize = 11f
                setTypeface(typeface, Typeface.BOLD)
                setTextColor(ContextCompat.getColor(context, R.color.lw_accent))
            }
            val userText = TextView(this).apply {
                text = userPrompt
                textSize = 14f
                setTextColor(ContextCompat.getColor(context, R.color.lw_text_primary))
            }
            userBox.addView(userLabel)
            userBox.addView(userText)
            turnWrapper.addView(userBox)
        }

        val thinkingCard = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setBackgroundResource(R.drawable.bg_info_panel)
            setPadding(dpToPx(12), dpToPx(9), dpToPx(12), dpToPx(9))
            isClickable = true
            isFocusable = true
            visibility = visibleIf(initialThinking.isNotBlank())
            layoutParams = LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT
            ).apply {
                bottomMargin = dpToPx(6)
            }
        }
        val thinkingHeader = TextView(this).apply {
            text = getString(R.string.assistant_thinking_header_expanded)
            textSize = 12f
            setTypeface(typeface, Typeface.BOLD)
            setTextColor(ContextCompat.getColor(context, R.color.lw_accent))
        }
        val thinkingBody = TextView(this).apply {
            text = initialThinking
            textSize = 12f
            typeface = Typeface.MONOSPACE
            setTextColor(ContextCompat.getColor(context, R.color.lw_text_secondary))
            visibility = View.VISIBLE
            setPadding(0, dpToPx(6), 0, 0)
        }
        thinkingCard.addView(thinkingHeader)
        thinkingCard.addView(thinkingBody)
        turnWrapper.addView(thinkingCard)

        val toolScroll = HorizontalScrollView(this).apply {
            isHorizontalScrollBarEnabled = false
            visibility = visibleIf(initialTools.isNotEmpty())
            layoutParams = LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT
            ).apply {
                bottomMargin = dpToPx(6)
            }
        }
        val toolGroup = ChipGroup(this).apply {
            isSingleLine = true
            chipSpacingHorizontal = dpToPx(6)
        }
        toolScroll.addView(toolGroup)
        turnWrapper.addView(toolScroll)

        val replyBox = TextView(this).apply {
            text = getString(
                if (isConfirmationResume) R.string.assistant_agent_thinking_hint else R.string.assistant_state_thinking
            )
            textSize = 15f
            setTextIsSelectable(true)
            setLineSpacing(0f, 1.2f)
            setBackgroundResource(R.drawable.bg_info_panel)
            setPadding(dpToPx(14), dpToPx(11), dpToPx(14), dpToPx(11))
            setTextColor(ContextCompat.getColor(context, R.color.lw_text_secondary))
            layoutParams = LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT
            )
        }
        turnWrapper.addView(replyBox)
        container.addView(turnWrapper)

        val views = LiveStreamingTurnViews(
            thinkingCard = thinkingCard,
            thinkingHeader = thinkingHeader,
            thinkingBody = thinkingBody,
            toolScroll = toolScroll,
            toolGroup = toolGroup,
            replyBox = replyBox
        )
        thinkingCard.setOnClickListener {
            performTapHaptic(it)
            views.userToggledThinking = true
            views.isThinkingExpanded = !views.isThinkingExpanded
            thinkingBody.visibility = visibleIf(views.isThinkingExpanded)
            thinkingHeader.setText(
                if (views.isThinkingExpanded) {
                    R.string.assistant_thinking_header_expanded
                } else {
                    R.string.assistant_thinking_header_collapsed
                }
            )
            adjustAgentScrollHeightAndScrollToBottom(scrollToBottom = false)
        }
        if (initialTools.isNotEmpty()) {
            bindChipsToGroup(toolGroup, initialTools)
        }
        liveTurnViews = views
        adjustAgentScrollHeightAndScrollToBottom(scrollToBottom = true)
    }

    private fun updateLiveStreamingTurn(progress: AgentStreamProgress) {
        if (progress.sessionId.isNotBlank()) {
            agentSessionId = progress.sessionId
        }
        val views = liveTurnViews ?: return

        if (progress.thinking.isNotBlank()) {
            views.thinkingCard.visibility = View.VISIBLE
            views.thinkingBody.text = progress.thinking
            if (!views.userToggledThinking) {
                views.isThinkingExpanded = true
                views.thinkingBody.visibility = View.VISIBLE
                views.thinkingHeader.setText(R.string.assistant_thinking_header_expanded)
            }
        }

        if (progress.toolCalls.isNotEmpty()) {
            views.toolScroll.visibility = View.VISIBLE
            bindChipsToGroup(views.toolGroup, progress.toolCalls)
        }

        if (progress.reply.isNotBlank()) {
            views.replyBox.setTextColor(ContextCompat.getColor(this, R.color.lw_text_primary))
            views.replyBox.text = progress.reply
        }

        adjustAgentScrollHeightAndScrollToBottom(scrollToBottom = true)
    }

    private fun handleAgentResponse(response: AgentResponse, isConfirmationResume: Boolean) {
        val resolvedSessionId = response.sessionId.ifEmpty {
            agentSessionId ?: sessionStore.startNewSession()
        }
        agentSessionId = resolvedSessionId
        activeConfirmation = response.pendingConfirmation

        // Execute any native Android system intents (Idea #7)
        val androidOutcomes = AndroidActionExecutor.executeAll(this, response.androidActions)

        val allPills = ArrayList<AgentToolCallInfo>(response.toolCalls.size + androidOutcomes.size)
        allPills.addAll(response.toolCalls)
        for (outcome in androidOutcomes) {
            allPills.add(AgentToolCallInfo(tool = "android", status = "ok", summary = outcome))
        }

        val turn = AgentChatTurn(
            userPrompt = lastSubmittedPrompt,
            thinking = response.thinking,
            toolCalls = allPills,
            reply = response.reply.ifBlank { getString(R.string.assistant_state_sent) }
        )
        val savedSession = if (isConfirmationResume) {
            sessionStore.updateLastTurn(resolvedSessionId, turn)
        } else {
            sessionStore.recordTurn(resolvedSessionId, turn)
        }

        renderChatHistoryThread(savedSession)

        // Bind Confirmation Gate card if a high-impact PC tool is waiting approval (Idea #10)
        val pending = response.pendingConfirmation
        if (pending != null) {
            binding.confirmationDescriptionText.text = pending.description
            bindConfirmationPreview(pending)
            binding.confirmationGateCard.visibility = View.VISIBLE
        } else {
            binding.confirmationPreviewImage.setImageDrawable(null)
            binding.confirmationGateCard.visibility = View.GONE
        }

        setUiState(UiState.AGENT_RESULT)
    }

    /** Shows the PC screenshot with the click target outlined, when the server sent one. */
    private fun bindConfirmationPreview(pending: PendingConfirmationInfo) {
        val imageView = binding.confirmationPreviewImage
        val bitmap = decodePreviewImage(pending.previewImageB64)
        imageView.setImageBitmap(bitmap)
        imageView.visibility = visibleIf(bitmap != null)
        if (bitmap == null) {
            imageView.setOnClickListener(null)
        } else {
            imageView.setOnClickListener {
                performTapHaptic(it)
                showPreviewFullScreen(bitmap)
            }
        }
    }

    private fun decodePreviewImage(base64: String): Bitmap? {
        if (base64.isBlank() || base64.length > MAX_PREVIEW_BASE64_CHARS) return null
        return try {
            val bytes = Base64.decode(base64, Base64.DEFAULT)
            BitmapFactory.decodeByteArray(bytes, 0, bytes.size)
        } catch (exc: IllegalArgumentException) {
            Log.w(TAG, "Confirmation preview image is not valid base64", exc)
            null
        }
    }

    private fun showPreviewFullScreen(bitmap: Bitmap) {
        val imageView = ImageView(this).apply {
            adjustViewBounds = true
            scaleType = ImageView.ScaleType.FIT_CENTER
            setImageBitmap(bitmap)
            contentDescription = getString(R.string.assistant_confirm_preview_desc)
        }
        MaterialAlertDialogBuilder(this)
            .setTitle(R.string.assistant_confirm_preview_title)
            .setView(imageView)
            .setPositiveButton(R.string.assistant_close_desc, null)
            .show()
    }

    private fun renderChatHistoryThread(session: SavedAgentSession) {
        val container = binding.chatHistoryContainer
        container.removeAllViews()
        val turns = session.turns
        for ((index, turn) in turns.withIndex()) {
            container.addView(buildTurnView(turn, isFirst = index == 0))
        }
        adjustAgentScrollHeightAndScrollToBottom()
    }

    private fun buildTurnView(turn: AgentChatTurn, isFirst: Boolean): View {
        val turnWrapper = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            layoutParams = LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT
            ).apply {
                if (!isFirst) topMargin = dpToPx(12)
            }
        }

        // 1. User message bubble
        if (turn.userPrompt.isNotBlank()) {
            val userBox = LinearLayout(this).apply {
                orientation = LinearLayout.VERTICAL
                setBackgroundResource(R.drawable.bg_status_pill)
                setPadding(dpToPx(14), dpToPx(8), dpToPx(14), dpToPx(8))
                layoutParams = LinearLayout.LayoutParams(
                    ViewGroup.LayoutParams.MATCH_PARENT,
                    ViewGroup.LayoutParams.WRAP_CONTENT
                ).apply {
                    bottomMargin = dpToPx(6)
                }
            }
            val userLabel = TextView(this).apply {
                text = getString(R.string.assistant_you_label)
                textSize = 11f
                setTypeface(typeface, Typeface.BOLD)
                setTextColor(ContextCompat.getColor(context, R.color.lw_accent))
            }
            val userText = TextView(this).apply {
                text = turn.userPrompt
                textSize = 14f
                setTextColor(ContextCompat.getColor(context, R.color.lw_text_primary))
            }
            userBox.addView(userLabel)
            userBox.addView(userText)
            turnWrapper.addView(userBox)
        }

        // 2. Collapsible Thinking drawer for this turn
        if (turn.thinking.isNotBlank()) {
            val thinkingCard = LinearLayout(this).apply {
                orientation = LinearLayout.VERTICAL
                setBackgroundResource(R.drawable.bg_info_panel)
                setPadding(dpToPx(12), dpToPx(9), dpToPx(12), dpToPx(9))
                isClickable = true
                isFocusable = true
                layoutParams = LinearLayout.LayoutParams(
                    ViewGroup.LayoutParams.MATCH_PARENT,
                    ViewGroup.LayoutParams.WRAP_CONTENT
                ).apply {
                    bottomMargin = dpToPx(6)
                }
            }
            val headerText = TextView(this).apply {
                text = getString(R.string.assistant_thinking_header_collapsed)
                textSize = 12f
                setTypeface(typeface, Typeface.BOLD)
                setTextColor(ContextCompat.getColor(context, R.color.lw_accent))
            }
            val bodyText = TextView(this).apply {
                text = turn.thinking
                textSize = 12f
                typeface = Typeface.MONOSPACE
                setTextColor(ContextCompat.getColor(context, R.color.lw_text_secondary))
                visibility = View.GONE
                setPadding(0, dpToPx(6), 0, 0)
            }
            thinkingCard.setOnClickListener {
                performTapHaptic(it)
                val expanded = bodyText.visibility != View.VISIBLE
                bodyText.visibility = visibleIf(expanded)
                headerText.setText(
                    if (expanded) {
                        R.string.assistant_thinking_header_expanded
                    } else {
                        R.string.assistant_thinking_header_collapsed
                    }
                )
                adjustAgentScrollHeightAndScrollToBottom(scrollToBottom = false)
            }
            thinkingCard.addView(headerText)
            thinkingCard.addView(bodyText)
            turnWrapper.addView(thinkingCard)
        }

        // 3. Tool Execution Status Pills for this turn
        if (turn.toolCalls.isNotEmpty()) {
            val chipScroll = HorizontalScrollView(this).apply {
                isHorizontalScrollBarEnabled = false
                layoutParams = LinearLayout.LayoutParams(
                    ViewGroup.LayoutParams.MATCH_PARENT,
                    ViewGroup.LayoutParams.WRAP_CONTENT
                ).apply {
                    bottomMargin = dpToPx(6)
                }
            }
            val chipGroup = ChipGroup(this).apply {
                isSingleLine = true
                chipSpacingHorizontal = dpToPx(6)
            }
            bindChipsToGroup(chipGroup, turn.toolCalls)
            chipScroll.addView(chipGroup)
            turnWrapper.addView(chipScroll)
        }

        // 4. Agent reply bubble
        val replyBox = TextView(this).apply {
            text = turn.reply
            textSize = 15f
            setTextIsSelectable(true)
            setLineSpacing(0f, 1.2f)
            setBackgroundResource(R.drawable.bg_info_panel)
            setPadding(dpToPx(14), dpToPx(11), dpToPx(14), dpToPx(11))
            setTextColor(ContextCompat.getColor(context, R.color.lw_text_primary))
            layoutParams = LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT
            )
        }
        turnWrapper.addView(replyBox)
        return turnWrapper
    }

    private fun bindChipsToGroup(chipGroup: ChipGroup, pills: List<AgentToolCallInfo>) {
        chipGroup.removeAllViews()
        val chipBg = ColorStateList.valueOf(ContextCompat.getColor(this, R.color.lw_pill_bg))
        for (pill in pills) {
            val prefix = when (pill.status) {
                "ok", "queued_on_phone" -> "✓ "
                "running" -> "⟳ "
                "pending_confirmation" -> "⏳ "
                "rejected", "error" -> "✕ "
                else -> "• "
            }
            val textColorRes = when (pill.status) {
                "ok", "queued_on_phone" -> R.color.lw_success
                "running", "pending_confirmation" -> R.color.lw_accent
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
            chipGroup.addView(chip)
        }
    }

    private fun adjustAgentScrollHeightAndScrollToBottom(scrollToBottom: Boolean = true) {
        binding.agentResultScroll.post {
            val maxHeightPx = dpToPx(MAX_AGENT_SCROLL_HEIGHT_DP)
            val contentHeight = binding.agentResultContent.height
            val params = binding.agentResultScroll.layoutParams
            val desiredHeight = if (contentHeight > maxHeightPx) {
                maxHeightPx
            } else {
                ViewGroup.LayoutParams.WRAP_CONTENT
            }
            if (params.height != desiredHeight) {
                params.height = desiredHeight
                binding.agentResultScroll.layoutParams = params
            }
            if (scrollToBottom) {
                binding.agentResultScroll.fullScroll(View.FOCUS_DOWN)
            }
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
        renderSessionBar(state)
        updateTranscriptHint()
        if (state == UiState.AGENT_RESULT) {
            adjustAgentScrollHeightAndScrollToBottom()
        }
        binding.transcriptInput.isEnabled = state != UiState.SENDING && state != UiState.AGENT_THINKING
    }

    private fun renderSessionBar(state: UiState) {
        val showBar = isAgentMode && state != UiState.SETUP_REQUIRED && state != UiState.SENT
        binding.agentSessionBar.visibility = visibleIf(showBar)
        if (!showBar) return

        val activeSession = sessionStore.getSession(agentSessionId)
        if (activeSession != null && activeSession.turns.isNotEmpty()) {
            binding.sessionSelectorText.text = getString(
                R.string.assistant_session_continuing_format,
                activeSession.title,
                activeSession.turns.size
            )
            binding.newSessionButton.visibility = View.VISIBLE
            binding.viewChatHistoryButton.text = getString(
                R.string.assistant_session_view_chat_format,
                activeSession.turns.size
            )
            binding.viewChatHistoryButton.visibility =
                visibleIf(state == UiState.RECORDING || state == UiState.REVIEW)
        } else {
            binding.sessionSelectorText.setText(R.string.assistant_session_new)
            binding.newSessionButton.visibility = View.GONE
            binding.viewChatHistoryButton.visibility = View.GONE
        }
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
        binding.transcribingSpinner.visibility = visibleIf(state == UiState.TRANSCRIBING)
        binding.transcriptInput.visibility =
            visibleIf(state == UiState.REVIEW || state == UiState.SENDING)
        binding.agentResultScroll.visibility =
            visibleIf(state == UiState.AGENT_RESULT || state == UiState.AGENT_THINKING)
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
        sessionStore.recordOverlayClosed()
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
        private const val MAX_AGENT_SCROLL_HEIGHT_DP = 360
        private const val MAX_PREVIEW_BASE64_CHARS = 600_000
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
