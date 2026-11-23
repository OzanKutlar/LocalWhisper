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
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo
import android.view.accessibility.AccessibilityWindowInfo
import android.widget.Toast
import androidx.annotation.StringRes
import kotlinx.coroutines.delay
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

    override fun onServiceConnected() {
        super.onServiceConnected()
        instance = this
    }

    override fun onAccessibilityEvent(event: AccessibilityEvent?) {
        val pkg = event?.packageName?.toString() ?: return
        if (pkg == packageName || pkg == "com.android.systemui") return
        val now = SystemClock.uptimeMillis()
        if (now - lastExternalCaptureUptimeMs < SNAPSHOT_THROTTLE_MS) return
        val root = rootInActiveWindow ?: return
        if (root.packageName?.toString() == packageName) return
        lastExternalCaptureUptimeMs = now
        cachedExternalSnapshot = buildWindowSnapshot(root)
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
        handler.removeCallbacks(attemptRunnable)
        pendingText = null
        cachedNodesById = emptyMap()
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
                val fresh = buildWindowSnapshot(externalRoot)
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

    private fun buildWindowSnapshot(root: AccessibilityNodeInfo): JSONObject {
        val snapshotId = "ps${snapshotCounter.incrementAndGet()}"
        val pkgName = root.packageName?.toString().orEmpty()
        val appLabel = resolveAppLabel(pkgName)
        val visibleTexts = JSONArray()
        val uiElements = JSONArray()
        val seenTexts = HashSet<String>()
        val nodeMap = HashMap<String, AccessibilityNodeInfo>()

        val queue = ArrayDeque<AccessibilityNodeInfo>()
        queue.add(root)
        var scanned = 0
        val boundsRect = Rect()

        while (queue.isNotEmpty() && scanned < MAX_SCREEN_NODES_SCANNED) {
            val node = queue.removeFirst()
            scanned++
            if (!node.isVisibleToUser) continue

            val rawText = if (node.isPassword) "" else node.text?.toString()?.trim().orEmpty()
            val desc = node.contentDescription?.toString()?.trim().orEmpty()
            val hint = node.hintText?.toString()?.trim().orEmpty()
            val viewId = node.viewIdResourceName?.substringAfterLast('/')?.trim().orEmpty()
            val role = node.className?.toString()?.substringAfterLast('.')?.trim().orEmpty()

            if (rawText.isNotEmpty() && seenTexts.add(rawText)) {
                visibleTexts.put(rawText)
            } else if (desc.isNotEmpty() && seenTexts.add(desc)) {
                visibleTexts.put(desc)
            }

            val isInteresting = rawText.isNotEmpty() || desc.isNotEmpty() || hint.isNotEmpty() ||
                node.isEditable || node.isClickable || node.isScrollable
            if (isInteresting && uiElements.length() < MAX_UI_ELEMENTS_RETURNED) {
                node.getBoundsInScreen(boundsRect)
                if (boundsRect.width() > 0 && boundsRect.height() > 0) {
                    val elementId = "p${uiElements.length() + 1}"
                    nodeMap[elementId] = node
                    uiElements.put(
                        JSONObject().apply {
                            put("element_id", elementId)
                            put("role", role)
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
                                    put("left", boundsRect.left)
                                    put("top", boundsRect.top)
                                    put("right", boundsRect.right)
                                    put("bottom", boundsRect.bottom)
                                }
                            )
                        }
                    )
                }
            }

            val childCount = minOf(node.childCount, MAX_CHILDREN_PER_NODE)
            for (i in 0 until childCount) {
                node.getChild(i)?.let { queue.add(it) }
            }
        }

        cachedNodesById = nodeMap

        val focusedField = root.findFocus(AccessibilityNodeInfo.FOCUS_INPUT)
        val focusedJson = if (focusedField != null) {
            JSONObject().apply {
                put("editable", focusedField.isEditable)
                put("text", if (focusedField.isPassword) "" else currentRealText(focusedField))
                put("hint", focusedField.hintText?.toString().orEmpty())
            }
        } else null

        return JSONObject().apply {
            put("accessibility_enabled", true)
            put("phone_snapshot_id", snapshotId)
            put("package_name", pkgName)
            put("app_name", appLabel)
            put("captured_at_ms", System.currentTimeMillis())
            if (focusedJson != null) {
                put("focused_input", focusedJson)
            }
            put("visible_texts", visibleTexts)
            put("ui_elements", uiElements)
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

    private suspend fun captureScreenshotSuspend(): Bitmap? = suspendCancellableCoroutine { cont ->
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
                        if (cont.isActive) cont.resume(bitmap)
                    }

                    override fun onFailure(errorCode: Int) {
                        if (cont.isActive) cont.resume(null)
                    }
                }
            )
        } catch (_: Exception) {
            if (cont.isActive) cont.resume(null)
        }
    }

    private suspend fun performPhoneActionInternal(actionJson: JSONObject): JSONObject {
        val action = actionJson.optString("action", "click")
        val elementId = actionJson.optString("element_id", "").lowercase()
        val x = actionJson.optInt("x", 0)
        val y = actionJson.optInt("y", 0)
        val longPress = actionJson.optBoolean("long_press", false)
        val text = actionJson.optString("text", "")
        val clearFirst = actionJson.optBoolean("clear_first", false)
        val direction = actionJson.optString("direction", "down").lowercase()

        val node = if (elementId.isNotEmpty()) cachedNodesById[elementId] else null
        val ok = when (action) {
            "click" -> performClickOnPhone(node, x, y, longPress)
            "type" -> performTypeOnPhone(node, x, y, text, clearFirst)
            "scroll" -> performScrollOnPhone(node, direction)
            else -> false
        }

        delay(PHONE_ACTION_SETTLE_MS)
        captureScreenshotSuspend()
        captureBestSnapshot()
        return JSONObject().apply {
            put("ok", ok)
            put("action", action)
            put("element_id", elementId)
        }
    }

    private suspend fun performClickOnPhone(
        node: AccessibilityNodeInfo?,
        x: Int,
        y: Int,
        longPress: Boolean
    ): Boolean {
        val nodeAction = if (longPress) {
            AccessibilityNodeInfo.ACTION_LONG_CLICK
        } else {
            AccessibilityNodeInfo.ACTION_CLICK
        }
        if (node != null && performNodeOrParentAction(node, nodeAction)) {
            return true
        }
        if (x > 0 && y > 0) {
            val durationMs = if (longPress) 550L else 60L
            return dispatchTapGesture(x.toFloat(), y.toFloat(), durationMs)
        }
        return false
    }

    private suspend fun performTypeOnPhone(
        node: AccessibilityNodeInfo?,
        x: Int,
        y: Int,
        text: String,
        clearFirst: Boolean
    ): Boolean {
        if (node != null) {
            node.performAction(AccessibilityNodeInfo.ACTION_FOCUS)
            node.performAction(AccessibilityNodeInfo.ACTION_CLICK)
            if (node.isEditable && insertAtCursor(node, text, clearFirst)) {
                return true
            }
        }
        if (x > 0 && y > 0) {
            dispatchTapGesture(x.toFloat(), y.toFloat(), 60L)
            delay(200L)
        }
        val root = findUnderlyingAppRoot()
        val focused = root?.findFocus(AccessibilityNodeInfo.FOCUS_INPUT)
        if (focused != null && focused.isEditable) {
            return insertAtCursor(focused, text, clearFirst)
        }
        return false
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
        if (node != null && performNodeOrParentAction(node, scrollAction)) {
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
        return dispatchSwipeGesture(centerX, startY, centerX, endY, 260L)
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
        private const val PHONE_ACTION_SETTLE_MS = 350L
        private const val MAX_SCREEN_NODES_SCANNED = 140
        private const val MAX_UI_ELEMENTS_RETURNED = 85
        private const val MAX_CHILDREN_PER_NODE = 20
        private const val PREVIEW_MAX_WIDTH = 540

        private val snapshotCounter = AtomicInteger(0)

        @Volatile
        private var instance: LocalTypingService? = null

        @Volatile
        private var cachedExternalSnapshot: JSONObject? = null

        @Volatile
        private var cachedNodesById: Map<String, AccessibilityNodeInfo> = emptyMap()

        @Volatile
        private var cachedUnderlyingScreenshot: Bitmap? = null

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

        /** Draws the red highlight box and click point on the cached underlying phone screenshot. */
        fun renderPhoneTargetPreviewBase64(actionJson: JSONObject): String {
            val source = cachedUnderlyingScreenshot ?: return ""
            return try {
                val copy = source.copy(Bitmap.Config.ARGB_8888, true)
                val canvas = Canvas(copy)
                val paint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
                    color = Color.rgb(242, 61, 92)
                    style = Paint.Style.STROKE
                    strokeWidth = 6f
                }
                val box = actionJson.optJSONObject("box")
                if (box != null) {
                    val bx = box.optInt("x", 0).toFloat()
                    val by = box.optInt("y", 0).toFloat()
                    val bw = box.optInt("w", 0).toFloat()
                    val bh = box.optInt("h", 0).toFloat()
                    if (bw > 0f && bh > 0f) {
                        canvas.drawRect(bx - 5f, by - 5f, bx + bw + 5f, by + bh + 5f, paint)
                    }
                }
                val px = actionJson.optInt("x", -1)
                val py = actionJson.optInt("y", -1)
                if (px > 0 && py > 0) {
                    canvas.drawCircle(px.toFloat(), py.toFloat(), 14f, paint)
                }
                val scaled = if (copy.width > PREVIEW_MAX_WIDTH) {
                    val ratio = PREVIEW_MAX_WIDTH.toFloat() / copy.width.toFloat()
                    val targetH = (copy.height * ratio).toInt().coerceAtLeast(1)
                    Bitmap.createScaledBitmap(copy, PREVIEW_MAX_WIDTH, targetH, true).also {
                        if (it !== copy) copy.recycle()
                    }
                } else {
                    copy
                }
                val out = ByteArrayOutputStream()
                scaled.compress(Bitmap.CompressFormat.JPEG, 72, out)
                scaled.recycle()
                Base64.encodeToString(out.toByteArray(), Base64.NO_WRAP)
            } catch (exc: Exception) {
                Log.w(TAG, "Failed to render phone target preview", exc)
                ""
            }
        }
    }
}
