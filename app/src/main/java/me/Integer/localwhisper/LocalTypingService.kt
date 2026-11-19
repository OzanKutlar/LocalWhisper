package me.Integer.localwhisper

import android.accessibilityservice.AccessibilityService
import android.content.Intent
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.util.Log
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo
import android.widget.Toast
import androidx.annotation.StringRes

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

    override fun onServiceConnected() {
        super.onServiceConnected()
        instance = this
    }

    override fun onAccessibilityEvent(event: AccessibilityEvent?) {
        // Not event driven: insertion runs on the retry schedule started by enqueue().
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
        val current = if (field.isShowingHintText) "" else field.text?.toString().orEmpty()
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

    private fun moveCursor(field: AccessibilityNodeInfo, position: Int) {
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

    companion object {
        private const val TAG = "LW-LocalTyping"
        private const val FIRST_ATTEMPT_DELAY_MS = 300L
        private const val RETRY_INTERVAL_MS = 150L
        private const val MAX_ATTEMPTS = 30 // ~4.5 s, covers the overlay close animation

        @Volatile
        private var instance: LocalTypingService? = null

        /** Queues text to type into the focused field. Returns false when the service is off. Main thread only. */
        fun enqueue(text: String): Boolean {
            if (text.isEmpty()) return false
            val service = instance ?: return false
            service.begin(text)
            return true
        }
    }
}
