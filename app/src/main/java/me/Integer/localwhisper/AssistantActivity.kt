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
import android.view.WindowManager
import android.view.animation.DecelerateInterpolator
import android.view.animation.OvershootInterpolator
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
import com.google.android.material.dialog.MaterialAlertDialogBuilder
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import me.Integer.localwhisper.databinding.ActivityAssistantBinding
import org.json.JSONObject
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

    /** Collapsible card holding one turn's thinking blocks and tool steps in the order they happened. */
    private class TimelineViews(
        val card: LinearLayout,
        val header: TextView,
        val container: LinearLayout
    ) {
        val rendered: MutableList<View> = ArrayList()
        var entries: List<TimelineEntry> = emptyList()
        var expanded: Boolean = true
    }

    private class LiveStreamingTurnViews(
        val timeline: TimelineViews,
        val replyBox: TextView
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
        // The resumed stream continues the waiting turn's timeline instead of starting a new one.
        val resumeTimeline = existingTurns.lastOrNull()?.let { turn ->
            turn.timeline.ifEmpty { AgentTimeline.fromLegacy(turn.thinking, turn.toolCalls) }
        }.orEmpty()
        prepareLiveStreamingTurn(
            existingTurns = if (existingTurns.isNotEmpty()) existingTurns.dropLast(1) else emptyList(),
            userPrompt = lastPrompt,
            isConfirmationResume = true,
            initialTimeline = resumeTimeline
        )
        setUiState(UiState.AGENT_THINKING)

        val targetClientId = if (isLocalTargetSelected()) null else selectedClient?.id
        activeJob?.cancel()
        activeJob = lifecycleScope.launch {
            var phoneActionResult: JSONObject? = null
            var updatedPhoneScreen: JSONObject? = null
            if (approved && pending.phoneAction != null) {
                phoneActionResult = runPhoneUiActionWithOverlayPeek(pending.phoneAction)
                updatedPhoneScreen = LocalTypingService.capturePhoneScreenSnapshot()
            }
            val result = api.confirmAgentActionStream(
                baseUrl = baseUrl,
                sessionId = sessionId,
                confirmationId = pending.confirmationId,
                approved = approved,
                clientId = targetClientId,
                initialTimeline = resumeTimeline,
                phoneScreen = updatedPhoneScreen,
                phoneActionResult = phoneActionResult,
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
        initialTimeline: List<TimelineEntry> = emptyList()
    ) {
        val container = binding.chatHistoryContainer
        container.removeAllViews()
        for ((index, turn) in existingTurns.withIndex()) {
            container.addView(buildTurnView(turn, isFirst = index == 0))
        }

        val turnWrapper = newTurnWrapper(isFirst = existingTurns.isEmpty())
        if (userPrompt.isNotBlank()) {
            turnWrapper.addView(buildUserBubble(userPrompt))
        }
        val timelineViews = buildTimelineCard(startExpanded = true)
        timelineViews.card.setOnClickListener {
            performTapHaptic(it)
            toggleTimeline(timelineViews)
            adjustAgentScrollHeightAndScrollToBottom(scrollToBottom = false)
        }
        turnWrapper.addView(timelineViews.card)

        val placeholderRes =
            if (isConfirmationResume) R.string.assistant_agent_thinking_hint else R.string.assistant_state_thinking
        val replyBox = buildReplyBox(getString(placeholderRes), R.color.lw_text_secondary)
        turnWrapper.addView(replyBox)
        container.addView(turnWrapper)

        syncTimelineViews(timelineViews, initialTimeline)
        liveTurnViews = LiveStreamingTurnViews(timelineViews, replyBox)
        adjustAgentScrollHeightAndScrollToBottom(scrollToBottom = true)
    }

    private fun updateLiveStreamingTurn(progress: AgentStreamProgress) {
        if (progress.sessionId.isNotBlank()) {
            agentSessionId = progress.sessionId
        }
        val views = liveTurnViews ?: return
        // Follow new output only while the user is at the bottom; scrolling up to read stops it.
        val follow = isAgentScrollNearBottom()
        syncTimelineViews(views.timeline, progress.timeline)
        if (progress.reply.isNotBlank()) {
            views.replyBox.setTextColor(ContextCompat.getColor(this, R.color.lw_text_primary))
            views.replyBox.text = progress.reply
        }
        adjustAgentScrollHeightAndScrollToBottom(scrollToBottom = follow)
    }

    private fun isAgentScrollNearBottom(): Boolean {
        val scroll = binding.agentResultScroll
        val content = scroll.getChildAt(0) ?: return true
        val remaining = content.bottom - (scroll.scrollY + scroll.height)
        return remaining <= dpToPx(FOLLOW_SCROLL_THRESHOLD_DP)
    }

    private fun handleAgentResponse(response: AgentResponse, isConfirmationResume: Boolean) {
        val resolvedSessionId = response.sessionId.ifEmpty {
            agentSessionId ?: sessionStore.startNewSession()
        }
        agentSessionId = resolvedSessionId
        activeConfirmation = response.pendingConfirmation

        // Execute any native Android system intents (Idea #7)
        val androidOutcomes = AndroidActionExecutor.executeAll(this, response.androidActions)

        val timeline = response.timeline
            .ifEmpty { AgentTimeline.fromLegacy(response.thinking, response.toolCalls) }
            .toMutableList()
        for (outcome in androidOutcomes) {
            timeline.add(
                TimelineEntry.Tool(
                    tool = AgentTimeline.LOCAL_TOOL_NAME,
                    status = "ok",
                    summary = outcome,
                    local = true
                )
            )
        }

        val turn = AgentChatTurn(
            userPrompt = lastSubmittedPrompt,
            thinking = AgentTimeline.thinkingText(timeline),
            toolCalls = AgentTimeline.toolCalls(timeline),
            reply = response.reply.ifBlank { getString(R.string.assistant_state_sent) },
            timeline = timeline
        )
        val savedSession = if (isConfirmationResume) {
            sessionStore.updateLastTurn(resolvedSessionId, turn)
        } else {
            sessionStore.recordTurn(resolvedSessionId, turn)
        }

        renderChatHistoryThread(savedSession, expandLastTimeline = true)

        val pending = response.pendingConfirmation
        if (pending != null && pending.autoExecute && pending.phoneAction != null) {
            // Automatic phone actions (such as scroll_phone) execute immediately and resume.
            submitAgentConfirmation(approved = true)
            return
        }

        // Bind Confirmation Gate card if a PC or phone action is waiting approval
        if (pending != null) {
            binding.confirmationTitleText.setText(
                if (pending.phoneAction != null) {
                    R.string.assistant_confirm_phone_title
                } else {
                    R.string.assistant_confirm_title
                }
            )
            binding.confirmationDescriptionText.text = pending.description
            bindConfirmationPreview(pending)
            binding.confirmationGateCard.visibility = View.VISIBLE
        } else {
            binding.confirmationPreviewImage.setImageDrawable(null)
            binding.confirmationGateCard.visibility = View.GONE
        }

        setUiState(UiState.AGENT_RESULT)
    }

    /** Briefly hides the overlay sheet while executing a phone tap/type/scroll/screenshot on the underlying app. */
    private suspend fun runPhoneUiActionWithOverlayPeek(phoneAction: JSONObject): JSONObject {
        val isScreenshot = phoneAction.optString("action", "") == "capture_screenshot"
        window.addFlags(WindowManager.LayoutParams.FLAG_NOT_TOUCHABLE)
        binding.overlayScrim.alpha = 0f
        binding.sheetCard.alpha = 0f
        if (isScreenshot) {
            binding.overlayScrim.visibility = View.INVISIBLE
        }
        return try {
            delay(if (isScreenshot) 180L else 120L)
            LocalTypingService.executePhoneUiAction(phoneAction)
        } finally {
            binding.overlayScrim.visibility = View.VISIBLE
            binding.overlayScrim.alpha = 1f
            binding.sheetCard.alpha = 1f
            window.clearFlags(WindowManager.LayoutParams.FLAG_NOT_TOUCHABLE)
        }
    }

    /** Shows the PC or phone screenshot with the click target outlined, when available. */
    private fun bindConfirmationPreview(pending: PendingConfirmationInfo) {
        val imageView = binding.confirmationPreviewImage
        val phoneActionType = pending.phoneAction?.optString("action", "").orEmpty()
        val b64 = if (pending.previewImageB64.isNotBlank()) {
            pending.previewImageB64
        } else if (pending.phoneAction != null && phoneActionType in setOf("click", "type")) {
            LocalTypingService.renderPhoneTargetPreviewBase64(pending.phoneAction)
        } else {
            ""
        }
        val bitmap = decodePreviewImage(b64)
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

    private fun renderChatHistoryThread(session: SavedAgentSession, expandLastTimeline: Boolean = false) {
        val container = binding.chatHistoryContainer
        container.removeAllViews()
        val turns = session.turns
        for ((index, turn) in turns.withIndex()) {
            val expand = expandLastTimeline && index == turns.lastIndex
            container.addView(buildTurnView(turn, isFirst = index == 0, expandTimeline = expand))
        }
        adjustAgentScrollHeightAndScrollToBottom()
    }

    private fun buildTurnView(turn: AgentChatTurn, isFirst: Boolean, expandTimeline: Boolean = false): View {
        val turnWrapper = newTurnWrapper(isFirst)
        if (turn.userPrompt.isNotBlank()) {
            turnWrapper.addView(buildUserBubble(turn.userPrompt))
        }
        val entries = turn.timeline.ifEmpty { AgentTimeline.fromLegacy(turn.thinking, turn.toolCalls) }
        if (entries.isNotEmpty()) {
            // Collapsed cards only build their rows when the user expands them.
            val timelineViews = buildTimelineCard(startExpanded = expandTimeline)
            timelineViews.card.setOnClickListener {
                performTapHaptic(it)
                toggleTimeline(timelineViews)
                adjustAgentScrollHeightAndScrollToBottom(scrollToBottom = false)
            }
            syncTimelineViews(timelineViews, entries)
            turnWrapper.addView(timelineViews.card)
        }
        turnWrapper.addView(buildReplyBox(turn.reply, R.color.lw_text_primary))
        return turnWrapper
    }

    private fun matchWidthParams(): LinearLayout.LayoutParams = LinearLayout.LayoutParams(
        ViewGroup.LayoutParams.MATCH_PARENT,
        ViewGroup.LayoutParams.WRAP_CONTENT
    )

    private fun newTurnWrapper(isFirst: Boolean): LinearLayout = LinearLayout(this).apply {
        orientation = LinearLayout.VERTICAL
        layoutParams = matchWidthParams().apply {
            if (!isFirst) topMargin = dpToPx(12)
        }
    }

    private fun buildUserBubble(prompt: String): View {
        val userBox = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setBackgroundResource(R.drawable.bg_status_pill)
            setPadding(dpToPx(14), dpToPx(8), dpToPx(14), dpToPx(8))
            layoutParams = matchWidthParams().apply { bottomMargin = dpToPx(6) }
        }
        val userLabel = TextView(this).apply {
            text = getString(R.string.assistant_you_label)
            textSize = 11f
            setTypeface(typeface, Typeface.BOLD)
            setTextColor(ContextCompat.getColor(context, R.color.lw_accent))
        }
        val userText = TextView(this).apply {
            text = prompt
            textSize = 14f
            setTextColor(ContextCompat.getColor(context, R.color.lw_text_primary))
        }
        userBox.addView(userLabel)
        userBox.addView(userText)
        return userBox
    }

    private fun buildReplyBox(content: String, colorRes: Int): TextView = TextView(this).apply {
        text = content
        textSize = 15f
        setTextIsSelectable(true)
        setLineSpacing(0f, 1.2f)
        setBackgroundResource(R.drawable.bg_info_panel)
        setPadding(dpToPx(14), dpToPx(11), dpToPx(14), dpToPx(11))
        setTextColor(ContextCompat.getColor(context, colorRes))
        layoutParams = matchWidthParams()
    }

    private fun buildTimelineCard(startExpanded: Boolean): TimelineViews {
        val card = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setBackgroundResource(R.drawable.bg_info_panel)
            setPadding(dpToPx(12), dpToPx(9), dpToPx(12), dpToPx(9))
            isClickable = true
            isFocusable = true
            visibility = View.GONE
            layoutParams = matchWidthParams().apply { bottomMargin = dpToPx(6) }
        }
        val header = TextView(this).apply {
            textSize = 12f
            setTypeface(typeface, Typeface.BOLD)
            setTextColor(ContextCompat.getColor(context, R.color.lw_accent))
        }
        val container = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(0, dpToPx(4), 0, 0)
            visibility = visibleIf(startExpanded)
        }
        card.addView(header)
        card.addView(container)
        return TimelineViews(card, header, container).apply { expanded = startExpanded }
    }

    private fun toggleTimeline(views: TimelineViews) {
        views.expanded = !views.expanded
        views.container.visibility = visibleIf(views.expanded)
        syncTimelineViews(views, views.entries)
    }

    private fun updateTimelineHeader(views: TimelineViews) {
        val headerRes = if (views.expanded) {
            R.string.assistant_timeline_header_expanded
        } else {
            R.string.assistant_timeline_header_collapsed
        }
        views.header.text = getString(headerRes, AgentTimeline.toolCount(views.entries))
    }

    /** Brings the card's rows in line with [entries], touching only rows that changed. */
    private fun syncTimelineViews(views: TimelineViews, entries: List<TimelineEntry>) {
        views.entries = entries
        views.card.visibility = visibleIf(entries.isNotEmpty())
        updateTimelineHeader(views)
        if (!views.expanded) return
        for ((index, entry) in entries.withIndex()) {
            renderTimelineEntryAt(views, index, entry)
        }
        val extraRows = (views.rendered.size - entries.size).coerceAtLeast(0)
        repeat(extraRows) {
            views.rendered.removeAt(views.rendered.lastIndex)
            views.container.removeViewAt(views.container.childCount - 1)
        }
    }

    private fun renderTimelineEntryAt(views: TimelineViews, index: Int, entry: TimelineEntry) {
        val existing = views.rendered.getOrNull(index)
        if (existing != null && bindTimelineEntry(existing, entry)) return
        val fresh = createTimelineEntryView(entry)
        if (existing != null) {
            views.container.removeViewAt(index)
            views.container.addView(fresh, index)
            views.rendered[index] = fresh
        } else {
            views.container.addView(fresh)
            views.rendered.add(fresh)
        }
    }

    private fun createTimelineEntryView(entry: TimelineEntry): TextView {
        val view = when (entry) {
            is TimelineEntry.Thinking -> TextView(this).apply {
                tag = TIMELINE_TAG_THINKING
                textSize = 12f
                typeface = Typeface.MONOSPACE
                setTextColor(ContextCompat.getColor(context, R.color.lw_text_secondary))
                setPadding(0, dpToPx(4), 0, dpToPx(4))
            }
            is TimelineEntry.Tool -> TextView(this).apply {
                tag = TIMELINE_TAG_TOOL
                textSize = 12f
                setBackgroundResource(R.drawable.bg_status_pill)
                setPadding(dpToPx(10), dpToPx(4), dpToPx(10), dpToPx(4))
                layoutParams = LinearLayout.LayoutParams(
                    ViewGroup.LayoutParams.WRAP_CONTENT,
                    ViewGroup.LayoutParams.WRAP_CONTENT
                ).apply {
                    topMargin = dpToPx(3)
                    bottomMargin = dpToPx(3)
                }
            }
        }
        bindTimelineEntry(view, entry)
        return view
    }

    /** Updates a rendered row in place; false when the row is the wrong kind and must be replaced. */
    private fun bindTimelineEntry(view: View, entry: TimelineEntry): Boolean {
        val textView = view as? TextView ?: return false
        return when (entry) {
            is TimelineEntry.Thinking -> {
                if (view.tag != TIMELINE_TAG_THINKING) return false
                val text = entry.text.trim()
                if (textView.text.toString() != text) {
                    textView.text = text
                }
                true
            }
            is TimelineEntry.Tool -> {
                if (view.tag != TIMELINE_TAG_TOOL) return false
                textView.text = toolStatusPrefix(entry.status) + entry.summary
                textView.setTextColor(ContextCompat.getColor(this, toolStatusColor(entry.status)))
                true
            }
        }
    }

    private fun toolStatusPrefix(status: String): String = when (status) {
        "ok", "queued_on_phone" -> "✓ "
        "running" -> "⟳ "
        "pending_confirmation" -> "⏳ "
        "rejected", "error" -> "✕ "
        else -> "• "
    }

    private fun toolStatusColor(status: String): Int = when (status) {
        "ok", "queued_on_phone" -> R.color.lw_success
        "running", "pending_confirmation" -> R.color.lw_accent
        "rejected", "error" -> R.color.lw_error
        else -> R.color.lw_text_primary
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
        private const val FOLLOW_SCROLL_THRESHOLD_DP = 48
        private const val TIMELINE_TAG_THINKING = "timeline_thinking"
        private const val TIMELINE_TAG_TOOL = "timeline_tool"
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
