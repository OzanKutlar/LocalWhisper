package me.Integer.localwhisper

import android.content.Context
import android.util.Log
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asSharedFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import org.json.JSONObject

enum class AgentRuntimePhase {
    IDLE,
    THINKING,
    PAUSED,
    WAITING_CONFIRMATION,
    COMPLETED,
    ERROR
}

data class AgentRuntimeState(
    val phase: AgentRuntimePhase = AgentRuntimePhase.IDLE,
    val sessionId: String? = null,
    val userPrompt: String = "",
    val timeline: List<TimelineEntry> = emptyList(),
    val reply: String = "",
    val pendingConfirmation: PendingConfirmationInfo? = null,
    val errorMessage: String? = null,
    val stepSummary: String = "",
    val isConfirmationResume: Boolean = false,
    val isPhoneFocused: Boolean = false,
    val activityVisible: Boolean = false,
    /** Client id the current turn ran with; "local-phone" for phone turns, null before any turn. */
    val targetClientId: String? = null
) {
    val isRunningOrWaiting: Boolean
        get() = phase == AgentRuntimePhase.THINKING ||
            phase == AgentRuntimePhase.PAUSED ||
            phase == AgentRuntimePhase.WAITING_CONFIRMATION

    val hasActiveOverlay: Boolean
        get() = phase != AgentRuntimePhase.IDLE && !activityVisible
}

/**
 * Process-scoped manager that owns the active Agent streaming coroutine so runs survive
 * closing AssistantActivity and can execute phone UI actions while the target app is active.
 *
 * Pausing holds anything that acts on the phone (automatic phone steps and Android intents),
 * but never an approval: a pending confirmation is shown as soon as the server sends it.
 */
object AgentRuntimeManager {

    private const val TAG = "LW-AgentRuntime"
    private const val LOCAL_TARGET_ID = "local-phone"
    private const val MAX_AUTO_EXECUTE_STEPS = 12
    private const val MINIMIZE_WAIT_ATTEMPTS = 18
    private const val MINIMIZE_WAIT_STEP_MS = 25L
    private const val FOREGROUND_SETTLE_MS = 180L
    private const val MAX_STEP_SUMMARY_CHARS = 28
    /** Delay before the overlay returns after phone work, so back-to-back steps do not flicker. */
    private const val RESTORE_GRACE_MS = 600L

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)
    private val api = LocalWhisperApi()
    private val pauseGate = PauseGate()

    private val _state = MutableStateFlow(AgentRuntimeState())
    val state: StateFlow<AgentRuntimeState> = _state.asStateFlow()

    private val _hideRequests = MutableSharedFlow<Unit>(extraBufferCapacity = 2)

    /** Emitted when the overlay must move out of the way so the agent can act on the phone. */
    val hideRequests: SharedFlow<Unit> = _hideRequests.asSharedFlow()

    /** True while the overlay is hidden because the agent hid it; a user minimize clears it. */
    private var hiddenByAgent = false
    private var phoneWorkDepth = 0
    private var restoreJob: Job? = null

    @Volatile
    private var appContext: Context? = null

    private var activeJob: Job? = null

    /** One server response after its phone intents ran and its turn was saved. */
    private data class AppliedTurn(
        val sessionId: String,
        val timeline: List<TimelineEntry>,
        val reply: String
    )

    fun init(context: Context) {
        appContext = context.applicationContext
    }

    fun setActivityVisible(visible: Boolean) {
        if (visible) {
            // Shown again (restored, or reopened by the user): nothing is pending any more.
            hiddenByAgent = false
            restoreJob?.cancel()
            restoreJob = null
        }
        _state.update { it.copy(activityVisible = visible) }
    }

    /** The user minimized or closed the overlay; the agent must not bring it back by itself. */
    fun onUserHidOverlay() {
        cancelOverlayRestore()
    }

    private fun cancelOverlayRestore() {
        hiddenByAgent = false
        restoreJob?.cancel()
        restoreJob = null
    }

    fun pause() {
        val current = _state.value
        if (current.phase != AgentRuntimePhase.THINKING) return
        pauseGate.pause()
        _state.update { it.copy(phase = AgentRuntimePhase.PAUSED, stepSummary = "Paused") }
    }

    fun resume() {
        val current = _state.value
        if (current.phase != AgentRuntimePhase.PAUSED) return
        pauseGate.resume()
        _state.update {
            it.copy(
                phase = AgentRuntimePhase.THINKING,
                stepSummary = deriveStepSummary(it.timeline, it.reply)
            )
        }
    }

    fun togglePause() {
        when (_state.value.phase) {
            AgentRuntimePhase.THINKING -> pause()
            AgentRuntimePhase.PAUSED -> resume()
            else -> Unit
        }
    }

    /** Cancels any active agent stream and clears the runtime state back to IDLE. */
    fun stop() {
        pauseGate.resume()
        cancelOverlayRestore()
        activeJob?.cancel()
        activeJob = null
        _state.update { current ->
            AgentRuntimeState(
                phase = AgentRuntimePhase.IDLE,
                sessionId = current.sessionId,
                activityVisible = current.activityVisible,
                targetClientId = current.targetClientId
            )
        }
    }

    /** Clears a COMPLETED or ERROR state once the user has seen or dismissed it. */
    fun dismissFinishedState() {
        val phase = _state.value.phase
        if (phase == AgentRuntimePhase.COMPLETED || phase == AgentRuntimePhase.ERROR) {
            cancelOverlayRestore()
            _state.update { current ->
                AgentRuntimeState(
                    phase = AgentRuntimePhase.IDLE,
                    sessionId = current.sessionId,
                    activityVisible = current.activityVisible,
                    targetClientId = current.targetClientId
                )
            }
        }
    }

    /**
     * PC client id for follow-up requests (approvals, interjections). Prefers the id the turn
     * actually ran with, because PC client ids change on every reconnect and the saved
     * preference can point at a connection that no longer exists.
     */
    private fun resolveTargetClientId(ctx: Context, current: AgentRuntimeState): String? {
        val remembered = current.targetClientId
            ?: ServerPreferences(ctx).selectedClientId.takeIf { it.isNotBlank() }
        return remembered?.takeIf { it != LOCAL_TARGET_ID }
    }

    /** Interrupts the current in-flight agent work and steers the session with a user correction. */
    fun interject(context: Context, instruction: String) {
        val clean = instruction.trim()
        if (clean.isEmpty()) return
        init(context)
        val ctx = context.applicationContext
        val current = _state.value
        val targetClientId = resolveTargetClientId(ctx, current)
        startTurn(
            context = ctx,
            prompt = clean,
            clientId = targetClientId,
            sessionId = current.sessionId,
            isPhoneTarget = current.isPhoneFocused || targetClientId == null,
            interjection = true
        )
    }

    fun startTurn(
        context: Context,
        prompt: String,
        clientId: String?,
        sessionId: String?,
        isPhoneTarget: Boolean,
        interjection: Boolean = false
    ) {
        init(context)
        val ctx = context.applicationContext
        val prefs = ServerPreferences(ctx)
        val baseUrl = prefs.buildBaseUrl()
        if (baseUrl == null) {
            _state.update {
                it.copy(
                    phase = AgentRuntimePhase.ERROR,
                    userPrompt = prompt,
                    errorMessage = ctx.getString(R.string.invalid_server_config),
                    stepSummary = "Error"
                )
            }
            return
        }

        val sessionStore = AgentSessionStore(ctx)
        val existingTurns = sessionStore.getSession(sessionId)?.turns.orEmpty()
        pauseGate.resume()
        activeJob?.cancel()

        _state.update { current ->
            current.copy(
                phase = AgentRuntimePhase.THINKING,
                sessionId = sessionId,
                userPrompt = prompt,
                timeline = emptyList(),
                reply = "",
                pendingConfirmation = null,
                errorMessage = null,
                stepSummary = when {
                    interjection -> "Redirecting…"
                    isPhoneTarget -> "Reading screen…"
                    else -> "Thinking…"
                },
                isConfirmationResume = false,
                isPhoneFocused = isPhoneTarget,
                targetClientId = clientId ?: LOCAL_TARGET_ID
            )
        }

        activeJob = scope.launch {
            try {
                pauseGate.awaitUnpaused()
                // Reading the phone screen needs the app underneath on top, so the overlay steps aside briefly.
                val phoneScreenSnapshot = if (isPhoneTarget) {
                    withPhoneWork { LocalTypingService.capturePhoneScreenSnapshot() }
                } else {
                    LocalTypingService.capturePhoneScreenSnapshot()
                }
                _state.update {
                    if (it.phase == AgentRuntimePhase.THINKING) {
                        it.copy(stepSummary = "Thinking…")
                    } else {
                        it
                    }
                }
                val result = api.runAgentStream(
                    baseUrl = baseUrl,
                    prompt = prompt,
                    clientId = clientId,
                    sessionId = sessionId,
                    priorTurns = existingTurns,
                    phoneScreen = phoneScreenSnapshot,
                    interjection = interjection,
                    onProgress = { progress -> applyStreamProgress(progress) }
                )
                result.onSuccess { response ->
                    runAgentLoopFromResponse(
                        baseUrl = baseUrl,
                        initialResponse = response,
                        initialIsResume = false,
                        targetClientId = clientId,
                        ctx = ctx
                    )
                }.onFailure { exc ->
                    handleFailure(ctx, exc, fallbackToResult = false)
                }
            } catch (exc: CancellationException) {
                throw exc
            } catch (exc: Exception) {
                handleFailure(ctx, exc, fallbackToResult = false)
            }
        }
    }

    fun submitConfirmation(approved: Boolean, rejectionReason: String? = null) {
        val ctx = appContext
        if (ctx == null) {
            Log.w(TAG, "submitConfirmation ignored: runtime is not initialised")
            return
        }
        val current = _state.value
        val sessionId = current.sessionId
        val pending = current.pendingConfirmation
        if (sessionId == null || pending == null) {
            Log.w(TAG, "submitConfirmation ignored: no pending approval (phase=${current.phase})")
            return
        }
        val prefs = ServerPreferences(ctx)
        val baseUrl = prefs.buildBaseUrl()
        if (baseUrl == null) {
            handleFailure(
                ctx,
                IllegalStateException(ctx.getString(R.string.invalid_server_config)),
                fallbackToResult = true
            )
            return
        }
        val targetClientId = resolveTargetClientId(ctx, current)

        val sessionStore = AgentSessionStore(ctx)
        val resumeTimeline = current.timeline.ifEmpty {
            sessionStore.getSession(sessionId)?.turns?.lastOrNull()?.let { turn ->
                turn.timeline.ifEmpty { AgentTimeline.fromLegacy(turn.thinking, turn.toolCalls) }
            }.orEmpty()
        }
        val isPhoneAction = pending.phoneAction != null

        pauseGate.resume()
        activeJob?.cancel()
        _state.update {
            it.copy(
                phase = AgentRuntimePhase.THINKING,
                pendingConfirmation = null,
                errorMessage = null,
                isConfirmationResume = true,
                isPhoneFocused = it.isPhoneFocused || isPhoneAction,
                stepSummary = if (approved && isPhoneAction) "Acting on phone…" else "Thinking…"
            )
        }

        activeJob = scope.launch {
            try {
                var phoneActionResult: JSONObject? = null
                var updatedPhoneScreen: JSONObject? = null
                if (approved && pending.phoneAction != null) {
                    pauseGate.awaitUnpaused()
                    val outcome = withPhoneWork {
                        pauseGate.awaitUnpaused()
                        LocalTypingService.executePhoneUiAction(pending.phoneAction) to
                            LocalTypingService.capturePhoneScreenSnapshot()
                    }
                    phoneActionResult = outcome.first
                    updatedPhoneScreen = outcome.second
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
                    rejectionReason = if (!approved) rejectionReason else null,
                    onProgress = { progress -> applyStreamProgress(progress) }
                )
                result.onSuccess { response ->
                    runAgentLoopFromResponse(
                        baseUrl = baseUrl,
                        initialResponse = response,
                        initialIsResume = true,
                        targetClientId = targetClientId,
                        ctx = ctx
                    )
                }.onFailure { exc ->
                    handleFailure(ctx, exc, fallbackToResult = true)
                }
            } catch (exc: CancellationException) {
                throw exc
            } catch (exc: Exception) {
                handleFailure(ctx, exc, fallbackToResult = true)
            }
        }
    }

    /**
     * Applies each server response and runs automatic phone steps until the turn completes
     * or needs the user. Bounded: every pass returns or runs exactly one automatic step.
     */
    private suspend fun runAgentLoopFromResponse(
        baseUrl: String,
        initialResponse: AgentResponse,
        initialIsResume: Boolean,
        targetClientId: String?,
        ctx: Context
    ) {
        var response = initialResponse
        var isResume = initialIsResume

        for (autoStepsUsed in 0..MAX_AUTO_EXECUTE_STEPS) {
            val applied = applyAgentResponse(ctx, response, isResume)
            val pending = response.pendingConfirmation
            if (pending == null) {
                markCompleted(ctx, applied)
                return
            }
            val autoAction = pending.phoneAction?.takeIf { pending.autoExecute }
            if (autoAction == null) {
                surfaceConfirmation(ctx, applied, pending)
                return
            }
            if (AutoStepPolicy.shouldAskToContinue(autoStepsUsed, MAX_AUTO_EXECUTE_STEPS)) {
                surfaceConfirmation(ctx, applied, askToContinue(ctx, pending))
                return
            }
            val next = runAutoPhoneStep(baseUrl, applied, pending, autoAction, targetClientId)
            response = next.getOrElse { exc ->
                handleFailure(ctx, exc, fallbackToResult = true)
                return
            }
            isResume = true
        }

        Log.w(TAG, "Automatic step loop ended without a final state; marking the turn done")
        _state.update {
            it.copy(
                phase = AgentRuntimePhase.COMPLETED,
                pendingConfirmation = null,
                stepSummary = ctx.getString(R.string.overlay_pill_done)
            )
        }
    }

    /** Runs the response's phone intents, then saves the turn to the session store. */
    private suspend fun applyAgentResponse(
        ctx: Context,
        response: AgentResponse,
        isResume: Boolean
    ): AppliedTurn {
        val sessionStore = AgentSessionStore(ctx)
        val resolvedSessionId = response.sessionId.ifEmpty {
            _state.value.sessionId ?: sessionStore.startNewSession()
        }
        val androidOutcomes = runAndroidActions(ctx, response.androidActions)
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

        val replyText = response.reply.ifBlank { ctx.getString(R.string.assistant_state_sent) }
        val turn = AgentChatTurn(
            userPrompt = _state.value.userPrompt,
            thinking = AgentTimeline.thinkingText(timeline),
            toolCalls = AgentTimeline.toolCalls(timeline),
            reply = replyText,
            timeline = timeline
        )
        if (isResume) {
            sessionStore.updateLastTurn(resolvedSessionId, turn)
        } else {
            sessionStore.recordTurn(resolvedSessionId, turn)
        }
        return AppliedTurn(sessionId = resolvedSessionId, timeline = timeline.toList(), reply = replyText)
    }

    /** Android intents act on the phone, so they wait while the user has paused the agent. */
    private suspend fun runAndroidActions(
        ctx: Context,
        actions: List<AndroidClientAction>
    ): List<String> {
        if (actions.isEmpty()) return emptyList()
        pauseGate.awaitUnpaused()
        return withPhoneWork {
            pauseGate.awaitUnpaused()
            AndroidActionExecutor.executeAll(ctx, actions)
        }
    }

    /** Runs one automatic phone step (screen capture or scroll) and resumes the server turn. */
    private suspend fun runAutoPhoneStep(
        baseUrl: String,
        applied: AppliedTurn,
        pending: PendingConfirmationInfo,
        phoneAction: JSONObject,
        targetClientId: String?
    ): Result<AgentResponse> {
        _state.update {
            it.copy(
                sessionId = applied.sessionId,
                timeline = applied.timeline,
                reply = applied.reply,
                pendingConfirmation = null,
                isPhoneFocused = true,
                stepSummary = if (pauseGate.isPaused) {
                    "Paused"
                } else {
                    shorten(pending.description.ifBlank { "Working on phone…" })
                }
            )
        }
        pauseGate.awaitUnpaused()
        val (phoneActionResult, updatedPhoneScreen) = withPhoneWork {
            pauseGate.awaitUnpaused()
            LocalTypingService.executePhoneUiAction(phoneAction) to
                LocalTypingService.capturePhoneScreenSnapshot()
        }
        _state.update {
            it.copy(
                phase = if (pauseGate.isPaused) AgentRuntimePhase.PAUSED else AgentRuntimePhase.THINKING,
                isConfirmationResume = true,
                stepSummary = if (pauseGate.isPaused) "Paused" else "Thinking…"
            )
        }
        return api.confirmAgentActionStream(
            baseUrl = baseUrl,
            sessionId = applied.sessionId,
            confirmationId = pending.confirmationId,
            approved = true,
            clientId = targetClientId,
            initialTimeline = applied.timeline,
            phoneScreen = updatedPhoneScreen,
            phoneActionResult = phoneActionResult,
            onProgress = { progress -> applyStreamProgress(progress) }
        )
    }

    /** Shows an approval immediately; an approval is itself a stopping point, so any pause is cleared. */
    private fun surfaceConfirmation(
        ctx: Context,
        applied: AppliedTurn,
        pending: PendingConfirmationInfo
    ) {
        pauseGate.resume()
        _state.update {
            it.copy(
                phase = AgentRuntimePhase.WAITING_CONFIRMATION,
                sessionId = applied.sessionId,
                timeline = applied.timeline,
                reply = applied.reply,
                pendingConfirmation = pending,
                isPhoneFocused = it.isPhoneFocused || pending.phoneAction != null,
                errorMessage = null,
                stepSummary = ctx.getString(R.string.overlay_pill_confirm)
            )
        }
    }

    /** Turns an automatic step into a normal approval once the automatic step limit is reached. */
    private fun askToContinue(ctx: Context, pending: PendingConfirmationInfo): PendingConfirmationInfo {
        val next = pending.description.ifBlank { "phone action" }
        return pending.copy(
            autoExecute = false,
            description = ctx.getString(
                R.string.agent_auto_step_limit_confirm,
                MAX_AUTO_EXECUTE_STEPS,
                next
            )
        )
    }

    private fun markCompleted(ctx: Context, applied: AppliedTurn) {
        pauseGate.resume()
        _state.update {
            it.copy(
                phase = AgentRuntimePhase.COMPLETED,
                sessionId = applied.sessionId,
                timeline = applied.timeline,
                reply = applied.reply,
                pendingConfirmation = null,
                errorMessage = null,
                stepSummary = ctx.getString(R.string.overlay_pill_done)
            )
        }
    }

    /**
     * Runs [block] (an action on the phone) with the overlay out of the way. The overlay is hidden
     * only if visible, and comes back [RESTORE_GRACE_MS] after the last phone work ends.
     */
    private suspend fun <T> withPhoneWork(block: suspend () -> T): T {
        restoreJob?.cancel()
        restoreJob = null
        phoneWorkDepth++
        try {
            hideOverlayForPhoneWork()
            return block()
        } finally {
            phoneWorkDepth = (phoneWorkDepth - 1).coerceAtLeast(0)
            scheduleOverlayRestore()
        }
    }

    private suspend fun hideOverlayForPhoneWork() {
        if (!_state.value.activityVisible || !LocalTypingService.isRunning()) return
        hiddenByAgent = true
        _hideRequests.tryEmit(Unit)
        waitUntilActivityHidden()
    }

    private fun scheduleOverlayRestore() {
        if (!hiddenByAgent || phoneWorkDepth > 0) return
        restoreJob?.cancel()
        restoreJob = scope.launch {
            delay(RESTORE_GRACE_MS)
            restoreOverlayIfDue()
        }
    }

    private fun restoreOverlayIfDue() {
        val current = _state.value
        val due = OverlayVisibilityPolicy.shouldRestore(
            phase = current.phase,
            phoneWorkActive = phoneWorkDepth > 0,
            hiddenByAgent = hiddenByAgent,
            activityVisible = current.activityVisible
        )
        if (!due) return
        hiddenByAgent = false
        if (!LocalTypingService.bringAssistantToFront()) {
            Log.w(TAG, "Could not bring the agent overlay back after phone work")
        }
    }

    private suspend fun waitUntilActivityHidden() {
        for (attempt in 0 until MINIMIZE_WAIT_ATTEMPTS) {
            if (!_state.value.activityVisible) break
            delay(MINIMIZE_WAIT_STEP_MS)
        }
        delay(FOREGROUND_SETTLE_MS)
    }

    private fun applyStreamProgress(progress: AgentStreamProgress) {
        _state.update { current ->
            val resolvedSession = progress.sessionId.ifBlank { current.sessionId.orEmpty() }
                .takeIf { it.isNotBlank() }
            val paused = pauseGate.isPaused
            current.copy(
                phase = if (paused) AgentRuntimePhase.PAUSED else AgentRuntimePhase.THINKING,
                sessionId = resolvedSession,
                timeline = progress.timeline,
                reply = progress.reply,
                stepSummary = if (paused) "Paused" else deriveStepSummary(progress.timeline, progress.reply)
            )
        }
    }

    private fun handleFailure(ctx: Context, exc: Throwable, fallbackToResult: Boolean) {
        Log.w(TAG, "Agent runtime failed", exc)
        pauseGate.resume()
        val message = exc.message ?: ctx.getString(R.string.assistant_send_failed)
        _state.update { current ->
            current.copy(
                phase = AgentRuntimePhase.ERROR,
                pendingConfirmation = null,
                errorMessage = message,
                isConfirmationResume = fallbackToResult,
                stepSummary = ctx.getString(R.string.overlay_pill_error)
            )
        }
    }

    private fun deriveStepSummary(timeline: List<TimelineEntry>, reply: String): String {
        val runningTool = timeline.lastOrNull {
            it is TimelineEntry.Tool && it.status == "running"
        } as? TimelineEntry.Tool
        if (runningTool != null && runningTool.summary.isNotBlank()) {
            return shorten(runningTool.summary.removePrefix("Running: ").removePrefix("Locating: "))
        }
        if (reply.isNotBlank()) return "Responding…"
        return "Thinking…"
    }

    private fun shorten(text: String): String {
        val clean = text.trim()
        return if (clean.length <= MAX_STEP_SUMMARY_CHARS) {
            clean
        } else {
            clean.take(MAX_STEP_SUMMARY_CHARS - 1).trimEnd() + "…"
        }
    }
}
