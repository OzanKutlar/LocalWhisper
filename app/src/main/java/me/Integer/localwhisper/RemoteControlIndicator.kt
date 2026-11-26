package me.Integer.localwhisper

import android.accessibilityservice.AccessibilityService
import android.content.Context
import android.graphics.PixelFormat
import android.graphics.Typeface
import android.os.Handler
import android.os.Looper
import android.util.Log
import android.view.ContextThemeWrapper
import android.view.Gravity
import android.view.HapticFeedbackConstants
import android.view.View
import android.view.WindowInsets
import android.view.WindowManager
import android.widget.LinearLayout
import android.widget.TextView
import android.widget.Toast
import androidx.core.content.ContextCompat

/**
 * Shows a compact "Controlled from PC · Stop" pill on the phone screen while the PC popup
 * inspects or operates the phone UI.
 */
class RemoteControlIndicator(private val service: AccessibilityService) {

    private val themedContext: Context = ContextThemeWrapper(service, R.style.Theme_LocalWhisper)
    private val windowManager = service.getSystemService(WindowManager::class.java)
    private val handler = Handler(Looper.getMainLooper())
    private val density = service.resources.displayMetrics.density
    private val autoHideRunnable = Runnable { hide() }

    private var pillView: LinearLayout? = null

    fun noteActivity() {
        handler.post {
            ensureShown()
            handler.removeCallbacks(autoHideRunnable)
            handler.postDelayed(autoHideRunnable, AUTO_HIDE_MS)
        }
    }

    fun setTemporaryHidden(hidden: Boolean) {
        handler.post {
            pillView?.visibility = if (hidden) View.INVISIBLE else View.VISIBLE
        }
    }

    fun hide() {
        handler.post {
            handler.removeCallbacks(autoHideRunnable)
            val view = pillView
            pillView = null
            if (view != null && windowManager != null) {
                try {
                    windowManager.removeViewImmediate(view)
                } catch (_: Exception) {
                }
            }
        }
    }

    private fun ensureShown() {
        if (pillView != null || windowManager == null) return
        val row = LinearLayout(themedContext).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            setBackgroundResource(R.drawable.bg_floating_pill)
            setPadding(dp(12), dp(7), dp(12), dp(7))
            elevation = dp(8).toFloat()
        }

        val dot = TextView(themedContext).apply {
            text = "● "
            textSize = 13f
            setTextColor(ContextCompat.getColor(themedContext, R.color.lw_record_coral))
        }

        val label = TextView(themedContext).apply {
            setText(R.string.remote_control_pill_label)
            textSize = 13f
            setTypeface(typeface, Typeface.BOLD)
            setTextColor(ContextCompat.getColor(themedContext, R.color.lw_text_primary))
        }

        val stopBtn = TextView(themedContext).apply {
            setText(R.string.overlay_action_stop)
            textSize = 13f
            setTypeface(typeface, Typeface.BOLD)
            setTextColor(ContextCompat.getColor(themedContext, R.color.lw_error))
            setPadding(dp(12), dp(2), dp(4), dp(2))
            setOnClickListener { v ->
                v.performHapticFeedback(HapticFeedbackConstants.KEYBOARD_TAP)
                PhoneLink.sendEvent("remote_stop")
                hide()
                Toast.makeText(themedContext, R.string.remote_control_stopped_toast, Toast.LENGTH_SHORT).show()
            }
        }

        row.addView(dot)
        row.addView(label)
        row.addView(stopBtn)

        val topInset = windowManager.currentWindowMetrics.windowInsets
            .getInsets(WindowInsets.Type.systemBars()).top
        val params = WindowManager.LayoutParams(
            WindowManager.LayoutParams.WRAP_CONTENT,
            WindowManager.LayoutParams.WRAP_CONTENT,
            WindowManager.LayoutParams.TYPE_ACCESSIBILITY_OVERLAY,
            WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE or
                WindowManager.LayoutParams.FLAG_NOT_TOUCH_MODAL or
                WindowManager.LayoutParams.FLAG_LAYOUT_IN_SCREEN,
            PixelFormat.TRANSLUCENT
        ).apply {
            gravity = Gravity.TOP or Gravity.CENTER_HORIZONTAL
            y = topInset + dp(10)
        }

        try {
            windowManager.addView(row, params)
            pillView = row
        } catch (exc: Exception) {
            Log.w(TAG, "Could not show Controlled from PC pill", exc)
        }
    }

    private fun dp(value: Int): Int = (value * density).toInt()

    companion object {
        private const val TAG = "LW-RemotePill"
        private const val AUTO_HIDE_MS = 8_000L
    }
}
