package me.Integer.localwhisper

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.GestureDescription
import android.content.Intent
import android.graphics.Bitmap
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import android.graphics.Path
import android.graphics.Rect
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import android.util.Base64
import android.util.Log
import android.view.Display
import android.view.WindowManager
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo
import android.view.accessibility.AccessibilityWindowInfo
import android.widget.Toast
import androidx.annotation.StringRes
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.suspendCancellableCoroutine
import org.json.JSONArray
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.util.concurrent.Executor
import java.util.concurrent.atomic.AtomicInteger
import kotlin.coroutines.resume

/**
 * Types text sent to the "This phone" target into the text field that had input focus
 * before the assistant overlay opened, and provides live screen snapshots (p1..pN) plus
 * phone UI actions (click, type, scroll) for the LocalWhisper Agent.
 */
class LocalTypingService : AccessibilityService() {

    private enum class Attempt { INSERTED, RETRY, FAILED }

    private val handler = Handler(Looper.getMainLooper())
    private val mainExecutor = Executor { command -> handler.post(command) }
    private val attemptRunnable = Runnable { runAttempt() }
    private var pendingText: String? = null
    private var attemptsLeft = 0
    private var lastExternalCaptureUptimeMs = 0L
    private var lastScreenshotUptimeMs = 0L
    private var serviceScope: CoroutineScope? = null
    private var overlayObserverJob: Job? = null
    private var overlayController: AgentOverlayController? = null

    override fun onServiceConnected() {
        super.onServiceConnected()
        instance = this
        AgentRuntimeManager.init(applicationContext)
        attachAgentOverlay()
    }

    private fun attachAgentOverlay() {
        overlayObserverJob?.cancel()
        overlayController?.destroy()
        serviceScope?.cancel()
        val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)
        val controller = AgentOverlayController(this)
        serviceScope = scope
        overlayController = controller
        overlayObserverJob = scope.launch {
            AgentRuntimeManager.state.collect { state ->
                controller.render(state)
            }
        }
    }

    override fun onAccessibilityEvent(event: AccessibilityEvent?) {
        val pkg = event?.packageName?.toString() ?: return
        if (pkg == packageName || pkg == "com.android.systemui") return
        val now = SystemClock.uptimeMillis()
        if (now - lastExternalCaptureUptimeMs < SNAPSHOT_THROTTLE_MS) return
        val root = rootInActiveWindow ?: return
        if (root.packageName?.toString() == packageName) return
        lastExternalCaptureUptimeMs = now
        cachedExternalSnapshot = buildWindowSnapshot(root, rememberNodes = false)
        if (now - lastScreenshotUptimeMs >= SCREENSHOT_THROTTLE_MS) {
            lastScreenshotUptimeMs = now
            captureUnderlyingScreenshotAsync()
        }
    }

    override fun onInterrupt() {
        // Nothing to interrupt: this service gives no spoken or haptic feedback.
    }

    override fun onUnbind(intent: Intent?): Boolean {
        release()
        return super.onUnbind(intent)
    }

    override fun onDestroy() {
        release()
        super.onDestroy()
    }

    private fun release() {
        if (instance === this) {
            instance = null
        }
        overlayObserverJob?.cancel()
        overlayObserverJob = null
        overlayController?.destroy()
        overlayController = null
        serviceScope?.cancel()
        serviceScope = null
        handler.removeCallbacks(attemptRunnable)
        pendingText = null
        clearNodeMaps()
    }

    // ------------------------------------------------------------ Scheduling

    private fun begin(text: String) {
        pendingText = text
        attemptsLeft = MAX_ATTEMPTS
        handler.removeCallbacks(attemptRunnable)
        handler.postDelayed(attemptRunnable, FIRST_ATTEMPT_DELAY_MS)
    }

    private fun runAttempt() {
        val text = pendingText ?: return
        attemptsLeft--
        when (tryInsert(text)) {
            Attempt.INSERTED -> pendingText = null
            Attempt.FAILED -> fallBackToClipboard(text, R.string.local_insert_failed_copied)
            Attempt.RETRY -> scheduleRetryOrGiveUp(text)
        }
    }

    private fun scheduleRetryOrGiveUp(text: String) {
        if (attemptsLeft > 0) {
            handler.postDelayed(attemptRunnable, RETRY_INTERVAL_MS)
        } else {
            fallBackToClipboard(text, R.string.local_no_field_copied)
        }
    }

    // ------------------------------------------------------------ Insertion

    private fun tryInsert(text: String): Attempt {
        return try {
            val root = rootInActiveWindow ?: return Attempt.RETRY
            if (root.packageName?.toString() == packageName) return Attempt.RETRY
            val field = root.findFocus(AccessibilityNodeInfo.FOCUS_INPUT) ?: return Attempt.RETRY
            when {
                !field.isEditable -> Attempt.RETRY
                field.isPassword -> Attempt.FAILED
                insertAtCursor(field, text) -> Attempt.INSERTED
                else -> Attempt.FAILED
            }
        } catch (exc: Exception) {
            Log.w(TAG, "Inserting text into the focused field failed", exc)
            Attempt.FAILED
        }
    }

    private fun insertAtCursor(field: AccessibilityNodeInfo, text: String, clearFirst: Boolean = false): Boolean {
        val current = if (clearFirst) "" else currentRealText(field)
        val (start, end) = if (clearFirst) 0 to 0 else selectionBounds(field, current.length)
        val updated = current.substring(0, start) + text + current.substring(end)
        val textArgs = Bundle().apply {
            putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, updated)
        }
        if (!field.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, textArgs)) {
            return false
        }
        moveCursor(field, start + text.length)
        return true
    }

    private fun currentRealText(field: AccessibilityNodeInfo): String {
        val reported = field.text?.toString().orEmpty()
        val hintLength = field.hintText?.length ?: 0
        val hintFlag = field.isShowingHintText
        val matchesHint = reported.isNotEmpty() && reported == field.hintText?.toString()
        val noSelectionSupport = reported.isNotEmpty() && reportsNoRealText(field)
        val matchesChildLabel = reported.isNotEmpty() && !hintFlag && !matchesHint &&
            !noSelectionSupport && childShowsText(field, reported)
        val treatedEmpty = hintFlag || matchesHint || noSelectionSupport || matchesChildLabel
        Log.d(
            TAG,
            "field=${field.className} len=${reported.length} hintLen=$hintLength " +
                "hintFlag=$hintFlag matchesHint=$matchesHint " +
                "noSelectionSupport=$noSelectionSupport matchesChildLabel=$matchesChildLabel " +
                "treatedEmpty=$treatedEmpty " +
                "sel=${field.textSelectionStart}..${field.textSelectionEnd}"
        )
        return if (treatedEmpty) "" else reported
    }

    private fun reportsNoRealText(field: AccessibilityNodeInfo): Boolean {
        if (!field.isEditable) return false
        val noCursor = field.textSelectionStart < 0 && field.textSelectionEnd < 0
        if (!noCursor) return false
        return !supportsAction(field, AccessibilityNodeInfo.AccessibilityAction.ACTION_SET_SELECTION)
    }

    private fun supportsAction(
        node: AccessibilityNodeInfo,
        action: AccessibilityNodeInfo.AccessibilityAction
    ): Boolean {
        return node.actionList.any { it.id == action.id }
    }

    private fun childShowsText(field: AccessibilityNodeInfo, text: String): Boolean {
        return try {
            val children = boundedChildren(field)
            children.any { labelText(it) == text } ||
                children.any { child -> boundedChildren(child).any { labelText(it) == text } }
        } catch (exc: Exception) {
            Log.w(TAG, "Could not inspect field children for a placeholder", exc)
            false
        }
    }

    private fun boundedChildren(node: AccessibilityNodeInfo): List<AccessibilityNodeInfo> {
        val count = minOf(node.childCount, MAX_CHILDREN_SCANNED)
        val result = ArrayList<AccessibilityNodeInfo>(count)
        for (index in 0 until count) {
            node.getChild(index)?.let { result.add(it) }
        }
        return result
    }

    private fun labelText(node: AccessibilityNodeInfo): String? {
        if (node.isEditable) return null
        return node.text?.toString()
    }

    private fun moveCursor(field: AccessibilityNodeInfo, position: Int) {
        if (!field.refresh()) {
            return
        }
        val args = Bundle().apply {
            putInt(AccessibilityNodeInfo.ACTION_ARGUMENT_SELECTION_START_INT, position)
            putInt(AccessibilityNodeInfo.ACTION_ARGUMENT_SELECTION_END_INT, position)
        }
        field.performAction(AccessibilityNodeInfo.ACTION_SET_SELECTION, args)
    }

    private fun selectionBounds(field: AccessibilityNodeInfo, length: Int): Pair<Int, Int> {
        val rawStart = field.textSelectionStart
        val rawEnd = field.textSelectionEnd
        if (rawStart < 0 || rawEnd < 0) {
            return length to length
        }
        val start = minOf(rawStart, rawEnd).coerceIn(0, length)
        val end = maxOf(rawStart, rawEnd).coerceIn(0, length)
        return start to end
    }

    private fun fallBackToClipboard(text: String, @StringRes messageRes: Int) {
        pendingText = null
        val copied = LocalClipboard.copy(this, text)
        val shownRes = if (copied) messageRes else R.string.local_clipboard_failed
        Toast.makeText(this, shownRes, Toast.LENGTH_LONG).show()
    }

    // ------------------------------------------------------------ Screen Snapshot

    private fun captureBestSnapshot(): JSONObject? {
        return try {
            val externalRoot = findUnderlyingAppRoot()
            if (externalRoot != null) {
                val fresh = buildWindowSnapshot(externalRoot, rememberNodes = true)
                cachedExternalSnapshot = fresh
                fresh
            } else {
                cachedExternalSnapshot
            }
        } catch (exc: Exception) {
            Log.w(TAG, "Could not capture live phone screen snapshot", exc)
            cachedExternalSnapshot
        }
    }

    private fun findUnderlyingAppRoot(): AccessibilityNodeInfo? {
        val activeRoot = rootInActiveWindow
        if (activeRoot != null) {
            val pkg = activeRoot.packageName?.toString().orEmpty()
            if (pkg.isNotEmpty() && pkg != packageName && pkg != "com.android.systemui") {
                return activeRoot
            }
        }
        val interactiveWindows = try {
            windows
        } catch (_: Exception) {
            null
        }
        if (interactiveWindows != null) {
            for (window in interactiveWindows) {
                if (window.type != AccessibilityWindowInfo.TYPE_APPLICATION) continue
                val root = window.root ?: continue
                val pkg = root.packageName?.toString().orEmpty()
                if (pkg.isNotEmpty() && pkg != packageName && pkg != "com.android.systemui") {
                    return root
                }
            }
        }
        return null
    }

    private class ElementCandidate(
        val node: AccessibilityNodeInfo,
        val json: JSONObject,
        val priority: Int,
        val left: Int,
        val top: Int,
        val order: Int
    )

    /**
     * Builds a p1..pN snapshot. Only snapshots sent to the server ([rememberNodes]) scan deeply
     * and keep their node map, so background event snapshots never change what an id points at.
     */
    private fun buildWindowSnapshot(root: AccessibilityNodeInfo, rememberNodes: Boolean): JSONObject {
        val snapshotId = "ps${snapshotCounter.incrementAndGet()}"
        val pkgName = root.packageName?.toString().orEmpty()
        val visibleTexts = JSONArray()
        val scanLimit = if (rememberNodes) MAX_SCREEN_NODES_SCANNED else BACKGROUND_NODES_SCANNED
        val chosen = selectElements(collectElementCandidates(root, visibleTexts, scanLimit))
        val uiElements = JSONArray()
        val nodeMap = HashMap<String, AccessibilityNodeInfo>()
        for ((index, candidate) in chosen.withIndex()) {
            val elementId = "p${index + 1}"
            candidate.json.put("element_id", elementId)
            uiElements.put(candidate.json)
            nodeMap[elementId] = candidate.node
        }
        if (rememberNodes) {
            rememberNodeMap(snapshotId, nodeMap)
        }
        val (screenWidth, screenHeight) = screenSize()
        val focusedJson = focusedInputJson(root)
        return JSONObject().apply {
            put("accessibility_enabled", true)
            put("phone_snapshot_id", snapshotId)
            put("package_name", pkgName)
            put("app_name", resolveAppLabel(pkgName))
            put("captured_at_ms", System.currentTimeMillis())
            put("screen_width", screenWidth)
            put("screen_height", screenHeight)
            if (focusedJson != null) {
                put("focused_input", focusedJson)
            }
            put("visible_texts", visibleTexts)
            put("ui_elements", uiElements)
        }
    }

    /** Breadth-first scan of visible nodes, bounded by [scanLimit] nodes and [MAX_ELEMENT_CANDIDATES]. */
    private fun collectElementCandidates(
        root: AccessibilityNodeInfo,
        visibleTexts: JSONArray,
        scanLimit: Int
    ): List<ElementCandidate> {
        val candidates = ArrayList<ElementCandidate>()
        val seenTexts = HashSet<String>()
        val queue = ArrayDeque<AccessibilityNodeInfo>()
        queue.add(root)
        var scanned = 0
        while (queue.isNotEmpty() && scanned < scanLimit) {
            val node = queue.removeFirst()
            scanned++
            if (!node.isVisibleToUser) continue
            if (candidates.size < MAX_ELEMENT_CANDIDATES) {
                describeNode(node, candidates.size, visibleTexts, seenTexts)?.let { candidates.add(it) }
            }
            enqueueChildren(node, queue, scanLimit - scanned)
        }
        return candidates
    }

    /** Queues children only while they can still be scanned, so no child is fetched for nothing. */
    private fun enqueueChildren(
        node: AccessibilityNodeInfo,
        queue: ArrayDeque<AccessibilityNodeInfo>,
        remainingBudget: Int
    ) {
        val childCount = minOf(node.childCount, MAX_CHILDREN_PER_NODE)
        for (i in 0 until childCount) {
            if (queue.size >= remainingBudget) return
            node.getChild(i)?.let { queue.add(it) }
        }
    }

    private fun describeNode(
        node: AccessibilityNodeInfo,
        order: Int,
        visibleTexts: JSONArray,
        seenTexts: MutableSet<String>
    ): ElementCandidate? {
        val rawText = if (node.isPassword) "" else node.text?.toString()?.trim().orEmpty()
        val desc = node.contentDescription?.toString()?.trim().orEmpty()
        val hint = node.hintText?.toString()?.trim().orEmpty()
        recordVisibleText(rawText.ifEmpty { desc }, visibleTexts, seenTexts)
        val interactive = node.isEditable || node.isClickable || node.isScrollable
        val labeled = rawText.isNotEmpty() || desc.isNotEmpty() || hint.isNotEmpty()
        if (!interactive && !labeled) return null
        val bounds = Rect()
        node.getBoundsInScreen(bounds)
        if (bounds.width() <= 0 || bounds.height() <= 0) return null
        val viewId = node.viewIdResourceName?.substringAfterLast('/')?.trim().orEmpty()
        val json = JSONObject().apply {
            put("role", node.className?.toString()?.substringAfterLast('.')?.trim().orEmpty())
            if (rawText.isNotEmpty()) put("text", rawText)
            if (desc.isNotEmpty()) put("content_desc", desc)
            if (hint.isNotEmpty()) put("hint", hint)
            if (viewId.isNotEmpty()) put("view_id", viewId)
            put("clickable", node.isClickable)
            put("editable", node.isEditable)
            put("scrollable", node.isScrollable)
            put("focused", node.isFocused || node.isAccessibilityFocused)
            put(
                "bounds",
                JSONObject().apply {
                    put("left", bounds.left)
                    put("top", bounds.top)
                    put("right", bounds.right)
                    put("bottom", bounds.bottom)
                }
            )
        }
        val priority = when {
            interactive && labeled -> 3
            interactive -> 2
            else -> 1
        }
        return ElementCandidate(node, json, priority, bounds.left, bounds.top, order)
    }

    private fun recordVisibleText(text: String, visibleTexts: JSONArray, seenTexts: MutableSet<String>) {
        if (text.isEmpty() || visibleTexts.length() >= MAX_VISIBLE_TEXTS) return
        if (seenTexts.add(text)) {
            visibleTexts.put(text)
        }
    }

    /** Keeps the most useful elements (labelled and interactive first), then orders them top to bottom. */
    private fun selectElements(candidates: List<ElementCandidate>): List<ElementCandidate> {
        return candidates
            .sortedWith(compareByDescending<ElementCandidate> { it.priority }.thenBy { it.order })
            .take(MAX_UI_ELEMENTS_RETURNED)
            .sortedWith(compareBy<ElementCandidate> { it.top }.thenBy { it.left })
    }

    private fun focusedInputJson(root: AccessibilityNodeInfo): JSONObject? {
        val focusedField = root.findFocus(AccessibilityNodeInfo.FOCUS_INPUT) ?: return null
        return JSONObject().apply {
            put("editable", focusedField.isEditable)
            put("text", if (focusedField.isPassword) "" else currentRealText(focusedField))
            put("hint", focusedField.hintText?.toString().orEmpty())
        }
    }

    /** Full display size in pixels; accessibility bounds and screenshots share this space. */
    private fun screenSize(): Pair<Int, Int> {
        val metrics = resources.displayMetrics
        val fallback = metrics.widthPixels to metrics.heightPixels
        return try {
            val bounds = getSystemService(WindowManager::class.java)?.currentWindowMetrics?.bounds
            if (bounds != null && bounds.width() > 0 && bounds.height() > 0) {
                bounds.width() to bounds.height()
            } else {
                fallback
            }
        } catch (exc: Exception) {
            Log.w(TAG, "Could not read window metrics; using display metrics", exc)
            fallback
        }
    }

    private fun resolveAppLabel(pkgName: String): String {
        if (pkgName.isEmpty()) return "Unknown"
        return try {
            val info = packageManager.getApplicationInfo(pkgName, 0)
            packageManager.getApplicationLabel(info)?.toString().orEmpty().ifEmpty { pkgName }
        } catch (_: Exception) {
            pkgName
        }
    }

    // ------------------------------------------------------------ Phone UI Actions & Previews

    private fun captureUnderlyingScreenshotAsync() {
        try {
            takeScreenshot(
                Display.DEFAULT_DISPLAY,
                mainExecutor,
                object : TakeScreenshotCallback {
                    override fun onSuccess(screenshot: ScreenshotResult) {
                        try {
                            val hwBuffer = screenshot.hardwareBuffer
                            val hwBitmap = Bitmap.wrapHardwareBuffer(hwBuffer, screenshot.colorSpace)
                            hwBuffer.close()
                            if (hwBitmap != null) {
                                val swBitmap = hwBitmap.copy(Bitmap.Config.ARGB_8888, false)
                                hwBitmap.recycle()
                                cachedUnderlyingScreenshot = swBitmap
                            }
                        } catch (exc: Exception) {
                            Log.w(TAG, "Could not convert phone screenshot", exc)
                        }
                    }

                    override fun onFailure(errorCode: Int) {
                        Log.d(TAG, "takeScreenshot failed with code $errorCode")
                    }
                }
            )
        } catch (exc: Exception) {
            Log.d(TAG, "takeScreenshot unavailable", exc)
        }
    }

    private data class ScreenshotAttempt(val bitmap: Bitmap?, val errorCode: Int)

    private data class EncodedJpeg(val base64: String, val width: Int, val height: Int)

    private suspend fun captureScreenshotAttempt(): ScreenshotAttempt = suspendCancellableCoroutine { cont ->
        try {
            takeScreenshot(
                Display.DEFAULT_DISPLAY,
                mainExecutor,
                object : TakeScreenshotCallback {
                    override fun onSuccess(screenshot: ScreenshotResult) {
                        val bitmap = try {
                            val hwBuffer = screenshot.hardwareBuffer
                            val hw = Bitmap.wrapHardwareBuffer(hwBuffer, screenshot.colorSpace)
                            hwBuffer.close()
                            val sw = hw?.copy(Bitmap.Config.ARGB_8888, false)
                            hw?.recycle()
                            if (sw != null) {
                                cachedUnderlyingScreenshot = sw
                            }
                            sw
                        } catch (_: Exception) {
                            null
                        }
                        if (cont.isActive) cont.resume(ScreenshotAttempt(bitmap, 0))
                    }

                    override fun onFailure(errorCode: Int) {
                        if (cont.isActive) cont.resume(ScreenshotAttempt(null, errorCode))
                    }
                }
            )
        } catch (_: Exception) {
            if (cont.isActive) cont.resume(ScreenshotAttempt(null, -1))
        }
    }

    private suspend fun captureScreenshotSuspend(): Bitmap? = captureScreenshotAttempt().bitmap

    private suspend fun captureScreenshotWithRetry(): Bitmap? {
        val first = captureScreenshotAttempt()
        if (first.bitmap != null) return first.bitmap
        if (first.errorCode == ERROR_TAKE_SCREENSHOT_INTERVAL_TIME_SHORT) {
            delay(SCREENSHOT_RETRY_DELAY_MS)
            val second = captureScreenshotAttempt()
            if (second.bitmap != null) return second.bitmap
        }
        return cachedUnderlyingScreenshot
    }

    private fun encodeBitmapToJpeg(source: Bitmap, maxWidth: Int): EncodedJpeg? {
        val attempts = listOf(
            maxWidth to CAPTURE_PRIMARY_QUALITY,
            minOf(maxWidth, CAPTURE_FALLBACK_WIDTH) to CAPTURE_FALLBACK_QUALITY
        )
        for ((targetMaxWidth, quality) in attempts) {
            val scaled = if (source.width > targetMaxWidth) {
                val ratio = targetMaxWidth.toFloat() / source.width.toFloat()
                val targetH = (source.height * ratio).toInt().coerceAtLeast(1)
                Bitmap.createScaledBitmap(source, targetMaxWidth, targetH, true)
            } else {
                source
            }
            val out = ByteArrayOutputStream()
            val ok = try {
                scaled.compress(Bitmap.CompressFormat.JPEG, quality, out)
            } finally {
                if (scaled !== source) scaled.recycle()
            }
            if (!ok) continue
            val bytes = out.toByteArray()
            if (bytes.isNotEmpty() && bytes.size <= CAPTURE_MAX_BYTES) {
                val finalWidth = minOf(source.width, targetMaxWidth)
                val finalHeight = if (source.width > targetMaxWidth) {
                    (source.height * (targetMaxWidth.toFloat() / source.width.toFloat())).toInt().coerceAtLeast(1)
                } else {
                    source.height
                }
                return EncodedJpeg(
                    base64 = Base64.encodeToString(bytes, Base64.NO_WRAP),
                    width = finalWidth,
                    height = finalHeight
                )
            }
        }
        return null
    }

    /** Paints for the pN boxes drawn on screenshots sent to the model, scaled to the image width. */
    private class MarkPaints(widthScale: Float) {
        val box = Paint(Paint.ANTI_ALIAS_FLAG).apply {
            color = MARK_COLOR
            style = Paint.Style.STROKE
            strokeWidth = (MARK_STROKE_PX * widthScale).coerceAtLeast(1f)
        }
        val labelBackground = Paint(Paint.ANTI_ALIAS_FLAG).apply {
            color = MARK_COLOR
            style = Paint.Style.FILL
        }
        val labelText = Paint(Paint.ANTI_ALIAS_FLAG).apply {
            color = Color.WHITE
            textSize = (MARK_TEXT_PX * widthScale).coerceAtLeast(10f)
            isFakeBoldText = true
        }
    }

    /**
     * Copy of [source] with a labelled box around every snapshot element, so the model can tell
     * which visible items have pN ids. Never mutates [source] (it may be the shared cached shot).
     */
    private fun drawElementMarks(source: Bitmap, snapshot: JSONObject): Bitmap? {
        val elements = snapshot.optJSONArray("ui_elements") ?: return null
        val screenWidth = snapshot.optInt("screen_width", 0)
        if (elements.length() == 0 || screenWidth <= 0 || source.width <= 0) return null
        val marked = try {
            source.copy(Bitmap.Config.ARGB_8888, true)
        } catch (exc: Exception) {
            Log.w(TAG, "Could not copy screenshot for element marks", exc)
            null
        } ?: return null
        val scale = marked.width.toFloat() / screenWidth.toFloat()
        val paints = MarkPaints(marked.width / MARK_REFERENCE_WIDTH)
        val canvas = Canvas(marked)
        val count = minOf(elements.length(), MAX_UI_ELEMENTS_RETURNED)
        for (index in 0 until count) {
            elements.optJSONObject(index)?.let { drawElementMark(canvas, it, scale, paints) }
        }
        return marked
    }

    private fun drawElementMark(canvas: Canvas, element: JSONObject, scale: Float, paints: MarkPaints) {
        val bounds = element.optJSONObject("bounds") ?: return
        val left = bounds.optInt("left", 0) * scale
        val top = bounds.optInt("top", 0) * scale
        val right = bounds.optInt("right", 0) * scale
        val bottom = bounds.optInt("bottom", 0) * scale
        if (right <= left || bottom <= top) return
        canvas.drawRect(left, top, right, bottom, paints.box)
        val label = element.optString("element_id", "")
        if (label.isEmpty()) return
        val text = paints.labelText
        val pad = text.textSize * 0.15f
        val labelRight = left + text.measureText(label) + pad * 2
        val labelBottom = top + text.textSize + pad * 2
        canvas.drawRect(left, top, labelRight, labelBottom, paints.labelBackground)
        canvas.drawText(label, left + pad, top + pad - text.ascent(), text)
    }

    private suspend fun capturePhoneScreenshotAction(actionJson: JSONObject): JSONObject {
        val actionName = actionJson.optString("action", "capture_screen")
        val maxWidth = actionJson.optInt("max_width", CAPTURE_DEFAULT_MAX_WIDTH)
            .coerceIn(CAPTURE_MIN_WIDTH, CAPTURE_MAX_WIDTH)
        val snapshot = captureBestSnapshot()
        val appName = snapshot?.optString("app_name", "").orEmpty()
        val bitmap = runWithOverlayHidden(alwaysHide = true) { captureScreenshotWithRetry() }
        val marked = if (bitmap != null && snapshot != null) drawElementMarks(bitmap, snapshot) else null
        val marksDrawn = marked != null
        val encoded = try {
            when {
                marked != null -> encodeBitmapToJpeg(marked, maxWidth)
                bitmap != null -> encodeBitmapToJpeg(bitmap, maxWidth)
                else -> null
            }
        } finally {
            marked?.recycle()
        }
        if (encoded == null) {
            return JSONObject().apply {
                put("ok", snapshot != null)
                put("action", actionName)
                put("image_unavailable", true)
                if (appName.isNotBlank()) put("app_name", appName)
                if (snapshot == null) put("error", "Could not capture phone screenshot or UI tree")
            }
        }
        return JSONObject().apply {
            put("ok", true)
            put("action", actionName)
            put("image_jpeg_b64", encoded.base64)
            put("mime_type", "image/jpeg")
            put("width", encoded.width)
            put("height", encoded.height)
            put("element_marks_drawn", marksDrawn)
            if (appName.isNotBlank()) put("app_name", appName)
        }
    }

    private suspend fun performPhoneActionInternal(actionJson: JSONObject): JSONObject {
        val action = actionJson.optString("action", "click")
        if (action == "capture_screenshot" || action == "capture_screen") {
            return capturePhoneScreenshotAction(actionJson)
        }
        val elementId = actionJson.optString("element_id", "").lowercase()
        val x = actionJson.optInt("x", 0)
        val y = actionJson.optInt("y", 0)
        val longPress = actionJson.optBoolean("long_press", false)
        val text = actionJson.optString("text", "")
        val clearFirst = actionJson.optBoolean("clear_first", false)
        val direction = actionJson.optString("direction", "down").lowercase()

        val snapshotId = actionJson.optString("phone_snapshot_id", "").trim()
        val node = findCachedNode(snapshotId, elementId)
        if (elementId.isNotEmpty() && node == null) {
            Log.d(TAG, "No cached node for $elementId in snapshot '$snapshotId'; using tap coordinates")
        }
        val before = screenFingerprint()
        val outcome = when (action) {
            "click" -> performClickOnPhone(node, x, y, longPress)
            "type" -> performTypeOnPhone(node, x, y, text, clearFirst)
            "scroll" -> TapOutcome(performScrollOnPhone(node, direction), "scroll", false)
            else -> TapOutcome(false, "none", false)
        }

        delay(PHONE_ACTION_SETTLE_MS)
        val after = screenFingerprint()
        val screenChanged: Boolean? = if (before != null && after != null) before != after else null
        Log.i(
            TAG,
            "Phone $action on ${elementId.ifEmpty { "point" }} snapshot='$snapshotId' at ($x,$y): " +
                "method=${outcome.method} gesture_completed=${outcome.gestureCompleted} " +
                "ok=${outcome.ok} screen_changed=$screenChanged"
        )
        runWithOverlayHidden(alwaysHide = true) { captureScreenshotSuspend() }
        captureBestSnapshot()
        return JSONObject().apply {
            put("ok", outcome.ok)
            put("action", action)
            put("element_id", elementId)
            put("method", outcome.method)
            put("gesture_completed", outcome.gestureCompleted)
            if (screenChanged != null) put("screen_changed", screenChanged)
        }
    }

    /** Cheap hash of what is on screen (package, element labels and bounds); null when unreadable. */
    private fun screenFingerprint(): Int? {
        return try {
            val root = findUnderlyingAppRoot() ?: return null
            fingerprintOf(buildWindowSnapshot(root, rememberNodes = false))
        } catch (exc: Exception) {
            Log.w(TAG, "Could not fingerprint the phone screen", exc)
            null
        }
    }

    private fun fingerprintOf(snapshot: JSONObject): Int {
        val parts = StringBuilder(snapshot.optString("package_name", ""))
        snapshot.optJSONObject("focused_input")?.let { parts.append('|').append(it.optString("text", "")) }
        val elements = snapshot.optJSONArray("ui_elements") ?: return parts.toString().hashCode()
        for (index in 0 until elements.length()) {
            val element = elements.optJSONObject(index) ?: continue
            parts.append('|').append(element.optString("text", ""))
                .append('/').append(element.optString("content_desc", ""))
                .append('@').append(element.optJSONObject("bounds")?.toString().orEmpty())
        }
        return parts.toString().hashCode()
    }

    private suspend fun <T> runWithOverlayHidden(
        tapX: Int = -1,
        tapY: Int = -1,
        alwaysHide: Boolean = false,
        block: suspend () -> T
    ): T {
        val controller = overlayController ?: return block()
        return controller.withOverlayHidden(
            tapX = tapX,
            tapY = tapY,
            alwaysHide = alwaysHide,
            block = block
        )
    }

    /** How a phone action was carried out; [method] is reported to the agent and logged. */
    private data class TapOutcome(val ok: Boolean, val method: String, val gestureCompleted: Boolean)

    /**
     * Taps with a real touch gesture at the approved point. The accessibility click is only a
     * fallback, because WebView and custom-drawn apps often accept ACTION_CLICK on a parent
     * container without anything happening.
     */
    private suspend fun performClickOnPhone(
        node: AccessibilityNodeInfo?,
        x: Int,
        y: Int,
        longPress: Boolean
    ): TapOutcome {
        val durationMs = if (longPress) LONG_PRESS_GESTURE_MS else TAP_GESTURE_MS
        if (isOnScreen(x, y) && tapAt(x, y, durationMs)) {
            return TapOutcome(true, "gesture", true)
        }
        val nodeAction = if (longPress) {
            AccessibilityNodeInfo.ACTION_LONG_CLICK
        } else {
            AccessibilityNodeInfo.ACTION_CLICK
        }
        val target = node?.takeIf { it.refresh() }?.let { findClickTarget(it, x, y, longPress) }
        if (target != null && target.performAction(nodeAction)) {
            return TapOutcome(true, "node_click", false)
        }
        return TapOutcome(false, "none", false)
    }

    /**
     * The node itself when it is clickable, else the nearest clickable ancestor that contains the
     * tap point and is not much bigger than the node. Never a whole list or WebView.
     */
    private fun findClickTarget(
        node: AccessibilityNodeInfo,
        x: Int,
        y: Int,
        longPress: Boolean
    ): AccessibilityNodeInfo? {
        val nodeBounds = Rect()
        node.getBoundsInScreen(nodeBounds)
        val nodeArea = (nodeBounds.width().toLong() * nodeBounds.height().toLong()).coerceAtLeast(1L)
        var current: AccessibilityNodeInfo? = node
        for (depth in 0 until MAX_CLICK_ANCESTOR_DEPTH) {
            val candidate = current ?: return null
            val clickable = if (longPress) candidate.isLongClickable else candidate.isClickable
            if (clickable) {
                val bounds = Rect()
                candidate.getBoundsInScreen(bounds)
                val area = bounds.width().toLong() * bounds.height().toLong()
                val containsPoint = x <= 0 || y <= 0 || bounds.contains(x, y)
                return if (containsPoint && area <= nodeArea * MAX_ANCESTOR_AREA_RATIO) candidate else null
            }
            current = candidate.parent
        }
        return null
    }

    private suspend fun performTypeOnPhone(
        node: AccessibilityNodeInfo?,
        x: Int,
        y: Int,
        text: String,
        clearFirst: Boolean
    ): TapOutcome {
        val editable = node?.takeIf { it.refresh() && it.isEditable }
        if (editable != null) {
            editable.performAction(AccessibilityNodeInfo.ACTION_FOCUS)
            if (insertAtCursor(editable, text, clearFirst)) {
                return TapOutcome(true, "set_text", false)
            }
        }
        val tapped = isOnScreen(x, y) && tapAt(x, y, TAP_GESTURE_MS)
        if (tapped) {
            delay(TYPE_AFTER_TAP_MS)
        }
        val root = findUnderlyingAppRoot()
        val focused = root?.findFocus(AccessibilityNodeInfo.FOCUS_INPUT)
        val inserted = focused != null && focused.isEditable && insertAtCursor(focused, text, clearFirst)
        val method = if (tapped) "gesture" else if (inserted) "focused_field" else "none"
        return TapOutcome(inserted, method, tapped)
    }

    /** Real touch at (x, y); our overlay windows are hidden and untouchable so it reaches the app. */
    private suspend fun tapAt(x: Int, y: Int, durationMs: Long): Boolean {
        return runWithOverlayHidden(alwaysHide = true) {
            dispatchTapGesture(x.toFloat(), y.toFloat(), durationMs)
        }
    }

    private fun isOnScreen(x: Int, y: Int): Boolean {
        val (width, height) = screenSize()
        return x in 1 until width && y in 1 until height
    }

    private suspend fun performScrollOnPhone(
        node: AccessibilityNodeInfo?,
        direction: String
    ): Boolean {
        val scrollAction = if (direction == "up") {
            AccessibilityNodeInfo.ACTION_SCROLL_BACKWARD
        } else {
            AccessibilityNodeInfo.ACTION_SCROLL_FORWARD
        }
        if (node != null && node.refresh() && performNodeOrParentAction(node, scrollAction)) {
            return true
        }
        val root = findUnderlyingAppRoot()
        val scrollable = if (root != null) findFirstScrollable(root) else null
        if (scrollable != null && scrollable.performAction(scrollAction)) {
            return true
        }
        val metrics = resources.displayMetrics
        val centerX = metrics.widthPixels / 2f
        val startY = if (direction == "up") metrics.heightPixels * 0.30f else metrics.heightPixels * 0.72f
        val endY = if (direction == "up") metrics.heightPixels * 0.72f else metrics.heightPixels * 0.28f
        return runWithOverlayHidden(tapX = centerX.toInt(), tapY = startY.toInt()) {
            dispatchSwipeGesture(centerX, startY, centerX, endY, 260L)
        }
    }

    private fun performNodeOrParentAction(node: AccessibilityNodeInfo, action: Int): Boolean {
        var current: AccessibilityNodeInfo? = node
        var depth = 0
        while (current != null && depth < 6) {
            if (current.performAction(action)) {
                return true
            }
            current = current.parent
            depth++
        }
        return false
    }

    private fun findFirstScrollable(root: AccessibilityNodeInfo): AccessibilityNodeInfo? {
        val queue = ArrayDeque<AccessibilityNodeInfo>()
        queue.add(root)
        var scanned = 0
        while (queue.isNotEmpty() && scanned < 60) {
            val node = queue.removeFirst()
            scanned++
            if (node.isVisibleToUser && node.isScrollable) {
                return node
            }
            val count = minOf(node.childCount, MAX_CHILDREN_PER_NODE)
            for (i in 0 until count) {
                node.getChild(i)?.let { queue.add(it) }
            }
        }
        return null
    }

    private suspend fun dispatchTapGesture(x: Float, y: Float, durationMs: Long): Boolean {
        val path = Path().apply { moveTo(x, y) }
        val stroke = GestureDescription.StrokeDescription(path, 0L, durationMs)
        return dispatchGestureSuspend(GestureDescription.Builder().addStroke(stroke).build())
    }

    private suspend fun dispatchSwipeGesture(
        startX: Float,
        startY: Float,
        endX: Float,
        endY: Float,
        durationMs: Long
    ): Boolean {
        val path = Path().apply {
            moveTo(startX, startY)
            lineTo(endX, endY)
        }
        val stroke = GestureDescription.StrokeDescription(path, 0L, durationMs)
        return dispatchGestureSuspend(GestureDescription.Builder().addStroke(stroke).build())
    }

    private suspend fun dispatchGestureSuspend(gesture: GestureDescription): Boolean =
        suspendCancellableCoroutine { cont ->
            val dispatched = try {
                dispatchGesture(
                    gesture,
                    object : GestureResultCallback() {
                        override fun onCompleted(gestureDescription: GestureDescription?) {
                            if (cont.isActive) cont.resume(true)
                        }

                        override fun onCancelled(gestureDescription: GestureDescription?) {
                            if (cont.isActive) cont.resume(false)
                        }
                    },
                    handler
                )
            } catch (exc: Exception) {
                Log.w(TAG, "dispatchGesture failed", exc)
                false
            }
            if (!dispatched && cont.isActive) {
                cont.resume(false)
            }
        }

    companion object {
        private const val TAG = "LW-LocalTyping"
        private const val FIRST_ATTEMPT_DELAY_MS = 300L
        private const val RETRY_INTERVAL_MS = 150L
        private const val MAX_ATTEMPTS = 30
        private const val MAX_CHILDREN_SCANNED = 8
        private const val SNAPSHOT_THROTTLE_MS = 400L
        private const val SCREENSHOT_THROTTLE_MS = 1800L
        private const val PHONE_ACTION_SETTLE_MS = 450L
        private const val TAP_GESTURE_MS = 60L
        private const val LONG_PRESS_GESTURE_MS = 550L
        private const val TYPE_AFTER_TAP_MS = 200L
        private const val MAX_CLICK_ANCESTOR_DEPTH = 6
        private const val MAX_ANCESTOR_AREA_RATIO = 6L
        private const val MAX_SCREEN_NODES_SCANNED = 600
        private const val BACKGROUND_NODES_SCANNED = 140
        private const val MAX_ELEMENT_CANDIDATES = 400
        private const val MAX_UI_ELEMENTS_RETURNED = 100
        private const val MAX_VISIBLE_TEXTS = 200
        private const val MAX_CHILDREN_PER_NODE = 60
        private const val MAX_NODE_MAPS = 6
        private const val MARK_COLOR = 0xFFE91E8C.toInt()
        private const val MARK_STROKE_PX = 3f
        private const val MARK_TEXT_PX = 26f
        private const val MARK_REFERENCE_WIDTH = 1080f
        private const val PREVIEW_MAX_WIDTH = 720
        private const val SCREENSHOT_RETRY_DELAY_MS = 1050L
        private const val CAPTURE_DEFAULT_MAX_WIDTH = 1080
        private const val CAPTURE_MIN_WIDTH = 320
        private const val CAPTURE_MAX_WIDTH = 1440
        private const val CAPTURE_FALLBACK_WIDTH = 720
        private const val CAPTURE_PRIMARY_QUALITY = 80
        private const val CAPTURE_FALLBACK_QUALITY = 62
        private const val CAPTURE_MAX_BYTES = 600_000

        private val snapshotCounter = AtomicInteger(0)

        @Volatile
        private var instance: LocalTypingService? = null

        @Volatile
        private var cachedExternalSnapshot: JSONObject? = null

        private val nodeMapsLock = Any()

        /** Node maps of recent snapshots sent to the server, oldest first, keyed by phone_snapshot_id. */
        private val nodeMapsBySnapshot = LinkedHashMap<String, Map<String, AccessibilityNodeInfo>>()

        private fun rememberNodeMap(snapshotId: String, nodes: Map<String, AccessibilityNodeInfo>) {
            synchronized(nodeMapsLock) {
                nodeMapsBySnapshot.remove(snapshotId)
                nodeMapsBySnapshot[snapshotId] = nodes
                val excess = (nodeMapsBySnapshot.size - MAX_NODE_MAPS).coerceAtLeast(0)
                val oldest = nodeMapsBySnapshot.keys.take(excess)
                for (key in oldest) {
                    nodeMapsBySnapshot.remove(key)
                }
            }
        }

        /** Node for [elementId] in the snapshot it was resolved against; newest snapshot when no id was sent. */
        private fun findCachedNode(snapshotId: String, elementId: String): AccessibilityNodeInfo? {
            if (elementId.isEmpty()) return null
            synchronized(nodeMapsLock) {
                val nodes = if (snapshotId.isNotEmpty()) {
                    nodeMapsBySnapshot[snapshotId]
                } else {
                    nodeMapsBySnapshot.values.lastOrNull()
                }
                return nodes?.get(elementId)
            }
        }

        private fun clearNodeMaps() {
            synchronized(nodeMapsLock) {
                nodeMapsBySnapshot.clear()
            }
        }

        @Volatile
        private var cachedUnderlyingScreenshot: Bitmap? = null

        /** True when LocalTypingService is connected and able to host the floating overlay. */
        fun isRunning(): Boolean = instance != null

        /** Queues text to type into the focused field. Returns false when the service is off. */
        fun enqueue(text: String): Boolean {
            if (text.isEmpty()) return false
            val service = instance ?: return false
            service.begin(text)
            return true
        }

        /** Captures a structured JSON snapshot of the active Android phone screen for the Agent. */
        fun capturePhoneScreenSnapshot(): JSONObject {
            val service = instance
            if (service == null) {
                return JSONObject().apply {
                    put("accessibility_enabled", false)
                    put(
                        "note",
                        "LocalWhisper typing Accessibility service is disabled on the phone. " +
                            "Enable 'Type on this phone' in LocalWhisper settings to let the agent view the phone screen."
                    )
                }
            }
            return service.captureBestSnapshot() ?: JSONObject().apply {
                put("accessibility_enabled", true)
                put("note", "No external phone app screen snapshot is available yet.")
            }
        }

        /** Executes a click, type, or scroll action on the underlying phone app and refreshes the snapshot. */
        suspend fun executePhoneUiAction(actionJson: JSONObject): JSONObject {
            val service = instance ?: return JSONObject().apply {
                put("ok", false)
                put("error", "Accessibility service is not running")
            }
            return service.performPhoneActionInternal(actionJson)
        }

        /** Draws a zoomed-in crop around the target element with red highlight box and click point. */
        fun renderPhoneTargetPreviewBase64(actionJson: JSONObject): String {
            val source = cachedUnderlyingScreenshot ?: return ""
            if (source.width <= 0 || source.height <= 0) return ""
            return try {
                val box = actionJson.optJSONObject("box")
                val bx = box?.optInt("x", 0) ?: 0
                val by = box?.optInt("y", 0) ?: 0
                val bw = box?.optInt("w", 0) ?: 0
                val bh = box?.optInt("h", 0) ?: 0
                val px = actionJson.optInt("x", -1)
                val py = actionJson.optInt("y", -1)

                val centerX = when {
                    px > 0 && bw > 0 -> (px + bx + bw / 2) / 2
                    bw > 0 -> bx + bw / 2
                    px > 0 -> px
                    else -> source.width / 2
                }
                val centerY = when {
                    py > 0 && bh > 0 -> (py + by + bh / 2) / 2
                    bh > 0 -> by + bh / 2
                    py > 0 -> py
                    else -> source.height / 2
                }

                val desiredW = maxOf((bw * 2.6f).toInt(), (source.width * 0.65f).toInt())
                val desiredH = maxOf((bh * 4.2f).toInt(), (source.height * 0.24f).toInt())
                val cropW = desiredW.coerceIn(minOf(source.width, 420), source.width)
                val cropH = desiredH.coerceIn(minOf(source.height, 300), minOf(source.height, 760))
                val cropX = (centerX - cropW / 2).coerceIn(0, (source.width - cropW).coerceAtLeast(0))
                val cropY = (centerY - cropH / 2).coerceIn(0, (source.height - cropH).coerceAtLeast(0))

                val rawCrop = Bitmap.createBitmap(source, cropX, cropY, cropW, cropH)
                val mutableCrop = rawCrop.copy(Bitmap.Config.ARGB_8888, true)
                if (mutableCrop !== rawCrop) rawCrop.recycle()

                val canvas = Canvas(mutableCrop)
                val paint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
                    color = Color.rgb(242, 61, 92)
                    style = Paint.Style.STROKE
                    strokeWidth = 5f
                }
                if (bw > 0 && bh > 0) {
                    val lx = (bx - cropX).toFloat()
                    val ly = (by - cropY).toFloat()
                    canvas.drawRect(lx - 4f, ly - 4f, lx + bw + 4f, ly + bh + 4f, paint)
                }
                if (px > 0 && py > 0) {
                    canvas.drawCircle((px - cropX).toFloat(), (py - cropY).toFloat(), 12f, paint)
                }
                val scaled = if (mutableCrop.width > PREVIEW_MAX_WIDTH) {
                    val ratio = PREVIEW_MAX_WIDTH.toFloat() / mutableCrop.width.toFloat()
                    val targetH = (mutableCrop.height * ratio).toInt().coerceAtLeast(1)
                    Bitmap.createScaledBitmap(mutableCrop, PREVIEW_MAX_WIDTH, targetH, true).also {
                        if (it !== mutableCrop) mutableCrop.recycle()
                    }
                } else {
                    mutableCrop
                }
                val out = ByteArrayOutputStream()
                scaled.compress(Bitmap.CompressFormat.JPEG, 84, out)
                scaled.recycle()
                Base64.encodeToString(out.toByteArray(), Base64.NO_WRAP)
            } catch (exc: Exception) {
                Log.w(TAG, "Failed to render phone target preview", exc)
                ""
            }
        }
    }
}
