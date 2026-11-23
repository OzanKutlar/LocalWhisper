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
    val activityVisible: Boolean = false
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
 */
object AgentRuntimeManager {

    private const val TAG = "LW-AgentRuntime"
    private const val LOCAL_TARGET_ID = "local-phone"
    private const val MAX_AUTO_EXECUTE_STEPS = 12
    private const val MINIMIZE_WAIT_ATTEMPTS = 18
    private const val MINIMIZE_WAIT_STEP_MS = 25L
    private const val FOREGROUND_SETTLE_MS = 180L
    private const val MAX_STEP_SUMMARY_CHARS = 28

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)
    private val api = LocalWhisperApi()
    private val pauseGate = PauseGate()

    private val _state = MutableStateFlow(AgentRuntimeState())
    val state: StateFlow<AgentRuntimeState> = _state.asStateFlow()

    private val _minimizeRequests = MutableSharedFlow<Unit>(extraBufferCapacity = 2)
    val minimizeRequests: SharedFlow<Unit> = _minimizeRequests.asSharedFlow()

    @Volatile
    private var appContext: Context? = null

    private var activeJob: Job? = null

    fun init(context: Context) {
        appContext = context.applicationContext
    }

    fun setActivityVisible(visible: Boolean) {
        _state.update { it.copy(activityVisible = visible) }
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
        activeJob?.cancel()
        activeJob = null
        _state.update { current ->
            AgentRuntimeState(
                phase = AgentRuntimePhase.IDLE,
                sessionId = current.sessionId,
                activityVisible = current.activityVisible
            )
        }
    }

    /** Clears a COMPLETED or ERROR state once the user has seen or dismissed it. */
    fun dismissFinishedState() {
        val phase = _state.value.phase
        if (phase == AgentRuntimePhase.COMPLETED || phase == AgentRuntimePhase.ERROR) {
            _state.update { current ->
                AgentRuntimeState(
                    phase = AgentRuntimePhase.IDLE,
                    sessionId = current.sessionId,
                    activityVisible = current.activityVisible
                )
            }
        }
    }

    fun startTurn(
        context: Context,
        prompt: String,
        clientId: String?,
        sessionId: String?,
        isPhoneTarget: Boolean,
        waitForActivityHide: Boolean
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
                stepSummary = if (isPhoneTarget) "Reading screen…" else "Thinking…",
                isConfirmationResume = false,
                isPhoneFocused = isPhoneTarget
            )
        }

        activeJob = scope.launch {
            try {
                if (waitForActivityHide && LocalTypingService.isRunning()) {
                    waitUntilActivityHidden()
                }
                pauseGate.awaitUnpaused()
                val phoneScreenSnapshot = LocalTypingService.capturePhoneScreenSnapshot()
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

    fun submitConfirmation(approved: Boolean) {
        val ctx = appContext ?: return
        val current = _state.value
        val sessionId = current.sessionId ?: return
        val pending = current.pendingConfirmation ?: return
        val prefs = ServerPreferences(ctx)
        val baseUrl = prefs.buildBaseUrl() ?: return
        val targetClientId = prefs.selectedClientId.takeIf {
            it.isNotBlank() && it != LOCAL_TARGET_ID
        }

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
                    ensureActivityMinimizedForPhoneWork()
                    pauseGate.awaitUnpaused()
                    phoneActionResult = LocalTypingService.executePhoneUiAction(pending.phoneAction)
                    pauseGate.awaitUnpaused()
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

    private suspend fun runAgentLoopFromResponse(
        baseUrl: String,
        initialResponse: AgentResponse,
        initialIsResume: Boolean,
        targetClientId: String?,
        ctx: Context
    ) {
        val sessionStore = AgentSessionStore(ctx)
        var response = initialResponse
        var isResume = initialIsResume

        for (autoStep in 0 until MAX_AUTO_EXECUTE_STEPS) {
            pauseGate.awaitUnpaused()
            val resolvedSessionId = response.sessionId.ifEmpty {
                _state.value.sessionId ?: sessionStore.startNewSession()
            }
            if (response.androidActions.isNotEmpty()) {
                ensureActivityMinimizedForPhoneWork()
                pauseGate.awaitUnpaused()
            }
            val androidOutcomes = AndroidActionExecutor.executeAll(ctx, response.androidActions)
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

            val pending = response.pendingConfirmation
            if (pending != null && pending.autoExecute && pending.phoneAction != null) {
                _state.update {
                    it.copy(
                        sessionId = resolvedSessionId,
                        timeline = timeline,
                        reply = replyText,
                        pendingConfirmation = pending,
                        isPhoneFocused = true,
                        stepSummary = shorten(pending.description.ifBlank { "Working on phone…" })
                    )
                }
                pauseGate.awaitUnpaused()
                ensureActivityMinimizedForPhoneWork()
                pauseGate.awaitUnpaused()
                val phoneActionResult = LocalTypingService.executePhoneUiAction(pending.phoneAction)
                pauseGate.awaitUnpaused()
                val updatedPhoneScreen = LocalTypingService.capturePhoneScreenSnapshot()
                _state.update {
                    it.copy(
                        phase = if (pauseGate.isPaused) AgentRuntimePhase.PAUSED else AgentRuntimePhase.THINKING,
                        pendingConfirmation = null,
                        isConfirmationResume = true,
                        stepSummary = if (pauseGate.isPaused) "Paused" else "Thinking…"
                    )
                }
                val nextResult = api.confirmAgentActionStream(
                    baseUrl = baseUrl,
                    sessionId = resolvedSessionId,
                    confirmationId = pending.confirmationId,
                    approved = true,
                    clientId = targetClientId,
                    initialTimeline = timeline,
                    phoneScreen = updatedPhoneScreen,
                    phoneActionResult = phoneActionResult,
                    onProgress = { progress -> applyStreamProgress(progress) }
                )
                response = nextResult.getOrElse { exc ->
                    handleFailure(ctx, exc, fallbackToResult = true)
                    return
                }
                isResume = true
                continue
            }

            if (pending != null) {
                _state.update {
                    it.copy(
                        phase = AgentRuntimePhase.WAITING_CONFIRMATION,
                        sessionId = resolvedSessionId,
                        timeline = timeline,
                        reply = replyText,
                        pendingConfirmation = pending,
                        isPhoneFocused = it.isPhoneFocused || pending.phoneAction != null,
                        errorMessage = null,
                        stepSummary = ctx.getString(R.string.overlay_pill_confirm)
                    )
                }
                return
            }

            _state.update {
                it.copy(
                    phase = AgentRuntimePhase.COMPLETED,
                    sessionId = resolvedSessionId,
                    timeline = timeline,
                    reply = replyText,
                    pendingConfirmation = null,
                    errorMessage = null,
                    stepSummary = ctx.getString(R.string.overlay_pill_done)
                )
            }
            return
        }

        _state.update {
            it.copy(
                phase = AgentRuntimePhase.COMPLETED,
                pendingConfirmation = null,
                stepSummary = ctx.getString(R.string.overlay_pill_done)
            )
        }
    }

    private suspend fun ensureActivityMinimizedForPhoneWork() {
        if (!_state.value.activityVisible || !LocalTypingService.isRunning()) return
        _minimizeRequests.tryEmit(Unit)
        waitUntilActivityHidden()
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
