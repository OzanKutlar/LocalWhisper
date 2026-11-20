package me.Integer.localwhisper

import android.accessibilityservice.AccessibilityService
import android.content.Intent
import android.graphics.Rect
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import android.util.Log
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo
import android.view.accessibility.AccessibilityWindowInfo
import android.widget.Toast
import androidx.annotation.StringRes
import org.json.JSONArray
import org.json.JSONObject

/**
 * Types text sent to the "This phone" target into the text field that had input focus
 * before the assistant overlay opened. Text is inserted literally at the cursor.
 *
 * Insertion is retried on a fixed, bounded schedule while the overlay is closing. When no
 * editable field can be found, or typing fails, the text is copied to the clipboard instead.
 */
class LocalTypingService : AccessibilityService() {

    private enum class Attempt { INSERTED, RETRY, FAILED }

    private val handler = Handler(Looper.getMainLooper())
    private val attemptRunnable = Runnable { runAttempt() }
    private var pendingText: String? = null
    private var attemptsLeft = 0
    private var lastExternalCaptureUptimeMs = 0L

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
            // The overlay may still be closing; wait until another app is in front.
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

    private fun insertAtCursor(field: AccessibilityNodeInfo, text: String): Boolean {
        val current = currentRealText(field)
        val (start, end) = selectionBounds(field, current.length)
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

    /**
     * The field's real contents. Many apps report an empty field's placeholder (e.g. "Message")
     * as its text without setting isShowingHintText, so that case is detected and treated as empty.
     */
    private fun currentRealText(field: AccessibilityNodeInfo): String {
        val reported = field.text?.toString().orEmpty()
        val hintLength = field.hintText?.length ?: 0
        val hintFlag = field.isShowingHintText
        val matchesHint = reported.isNotEmpty() && reported == field.hintText?.toString()
        val noSelectionSupport = reported.isNotEmpty() && reportsNoRealText(field)
        val matchesChildLabel = reported.isNotEmpty() && !hintFlag && !matchesHint &&
            !noSelectionSupport && childShowsText(field, reported)
        val treatedEmpty = hintFlag || matchesHint || noSelectionSupport || matchesChildLabel
        // Lengths and flags only: the user's text is never written to the log.
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

    /**
     * Android only gives a text field a cursor and the set-selection action when its real text is
     * non-empty, so an editable field lacking both is showing a placeholder, whatever its text says.
     */
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

    /** True when a non-editable child or grandchild label shows exactly this text (a drawn placeholder). */
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
        // The node is a snapshot from before ACTION_SET_TEXT; refresh it so set-selection is offered.
        if (!field.refresh()) {
            Log.d(TAG, "Field is gone, skipping the cursor move")
            return
        }
        val args = Bundle().apply {
            putInt(AccessibilityNodeInfo.ACTION_ARGUMENT_SELECTION_START_INT, position)
            putInt(AccessibilityNodeInfo.ACTION_ARGUMENT_SELECTION_END_INT, position)
        }
        if (!field.performAction(AccessibilityNodeInfo.ACTION_SET_SELECTION, args)) {
            Log.d(TAG, "Text inserted but the cursor could not be moved")
        }
    }

    /** Current selection clamped to the text; an unknown cursor appends at the end. */
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
                if (pkg.isNotEmpty() && pkg != packageName) {
                    return root
                }
            }
        }
        return null
    }

    private fun buildWindowSnapshot(root: AccessibilityNodeInfo): JSONObject {
        val pkgName = root.packageName?.toString().orEmpty()
        val appLabel = resolveAppLabel(pkgName)
        val visibleTexts = JSONArray()
        val uiElements = JSONArray()
        val seenTexts = HashSet<String>()

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

            val isInteresting = rawText.isNotEmpty() || desc.isNotEmpty() ||
                node.isEditable || node.isClickable
            if (isInteresting && uiElements.length() < MAX_UI_ELEMENTS_RETURNED) {
                node.getBoundsInScreen(boundsRect)
                uiElements.put(
                    JSONObject().apply {
                        put("role", role)
                        if (rawText.isNotEmpty()) put("text", rawText)
                        if (desc.isNotEmpty()) put("content_desc", desc)
                        if (hint.isNotEmpty()) put("hint", hint)
                        if (viewId.isNotEmpty()) put("view_id", viewId)
                        put("clickable", node.isClickable)
                        put("editable", node.isEditable)
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

            val childCount = minOf(node.childCount, MAX_CHILDREN_PER_NODE)
            for (i in 0 until childCount) {
                node.getChild(i)?.let { queue.add(it) }
            }
        }

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

    companion object {
        private const val TAG = "LW-LocalTyping"
        private const val FIRST_ATTEMPT_DELAY_MS = 300L
        private const val RETRY_INTERVAL_MS = 150L
        private const val MAX_ATTEMPTS = 30 // ~4.5 s, covers the overlay close animation
        private const val MAX_CHILDREN_SCANNED = 8 // per level, when looking for a placeholder label
        private const val SNAPSHOT_THROTTLE_MS = 400L
        private const val MAX_SCREEN_NODES_SCANNED = 140
        private const val MAX_UI_ELEMENTS_RETURNED = 65
        private const val MAX_CHILDREN_PER_NODE = 20

        @Volatile
        private var instance: LocalTypingService? = null

        @Volatile
        private var cachedExternalSnapshot: JSONObject? = null

        /** Queues text to type into the focused field. Returns false when the service is off. Main thread only. */
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
    }
}
