package me.Integer.localwhisper

import android.accessibilityservice.AccessibilityService
import android.annotation.SuppressLint
import android.content.Context
import android.content.Intent
import android.content.res.ColorStateList
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.PixelFormat
import android.graphics.Typeface
import android.text.TextUtils
import android.util.Base64
import android.util.Log
import android.view.ContextThemeWrapper
import android.view.Gravity
import android.view.HapticFeedbackConstants
import android.view.MotionEvent
import android.view.View
import android.view.ViewConfiguration
import android.view.ViewGroup
import android.view.WindowInsets
import android.view.WindowManager
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.ProgressBar
import android.widget.TextView
import androidx.annotation.ColorRes
import androidx.annotation.DrawableRes
import androidx.core.content.ContextCompat
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.withContext
import org.json.JSONObject
import kotlin.math.abs

/**
 * Manages the non-blocking floating Accessibility overlay for the phone Agent:
 * 1. Draggable corner pill with tap-to-pause and quick controls (Resume / Open / Stop).
 * 2. Compact floating confirmation card placed away from the target element.
 * 3. Non-touchable live red highlight box over phone elements awaiting click/type approval.
 */
class AgentOverlayController(private val service: AccessibilityService) {

    private val themedContext: Context = ContextThemeWrapper(service, R.style.Theme_LocalWhisper)
    private val windowManager = service.getSystemService(WindowManager::class.java)
    private val density = service.resources.displayMetrics.density
    private val touchSlop = ViewConfiguration.get(service).scaledTouchSlop

    private var pillRoot: LinearLayout? = null
    private var pillParams: WindowManager.LayoutParams? = null
    private var pillSpinner: ProgressBar? = null
    private var pillIcon: ImageView? = null
    private var pillLabel: TextView? = null
    private var pillDismissIcon: ImageView? = null
    private var pillActionsRow: LinearLayout? = null
    private var pillResumeButton: TextView? = null

    private var confirmRoot: LinearLayout? = null
    private var confirmParams: WindowManager.LayoutParams? = null
    private var confirmTitleView: TextView? = null
    private var confirmDescView: TextView? = null
    private var confirmPreviewImage: ImageView? = null
    private var confirmCardHiddenForId: String? = null

    private var highlightView: View? = null
    private var highlightParams: WindowManager.LayoutParams? = null

    private var lastState: AgentRuntimeState = AgentRuntimeState()
    private var quickControlsExpanded = false
    private var temporarilyHidden = false

    fun render(state: AgentRuntimeState) {
        val previousPendingId = lastState.pendingConfirmation?.confirmationId
        val newPendingId = state.pendingConfirmation?.confirmationId
        if (previousPendingId != newPendingId) {
            confirmCardHiddenForId = null
        }
        if (state.phase == AgentRuntimePhase.PAUSED) {
            quickControlsExpanded = true
        } else if (state.phase != AgentRuntimePhase.THINKING) {
            quickControlsExpanded = false
        }
        lastState = state

        if (!state.hasActiveOverlay) {
            hideAllWindows()
            return
        }

        renderPill(state)
        renderConfirmationAndHighlight(state)
    }

    /**
     * Temporarily hides overlay windows when taking a screenshot or when a tap point
     * overlaps the floating pill, then restores visibility.
     */
    suspend fun <T> withOverlayHidden(
        tapX: Int = -1,
        tapY: Int = -1,
        alwaysHide: Boolean = false,
        block: suspend () -> T
    ): T {
        val needsHide = alwaysHide || isPointOverlappingPill(tapX, tapY)
        if (!needsHide) return block()
        withContext(Dispatchers.Main) { setTemporaryHidden(true) }
        return try {
            delay(OVERLAY_HIDE_SETTLE_MS)
            block()
        } finally {
            withContext(Dispatchers.Main) { setTemporaryHidden(false) }
        }
    }

    fun destroy() {
        hideAllWindows()
    }

    // ------------------------------------------------------------ Corner Pill

    private fun renderPill(state: AgentRuntimeState) {
        ensurePillCreated()
        val root = pillRoot ?: return
        val spinner = pillSpinner ?: return
        val icon = pillIcon ?: return
        val label = pillLabel ?: return
        val dismissIcon = pillDismissIcon ?: return
        val actionsRow = pillActionsRow ?: return
        val resumeButton = pillResumeButton ?: return

        when (state.phase) {
            AgentRuntimePhase.THINKING -> {
                spinner.visibility = View.VISIBLE
                icon.visibility = View.GONE
                dismissIcon.visibility = View.GONE
                label.text = state.stepSummary.ifBlank {
                    themedContext.getString(R.string.overlay_pill_thinking)
                }
                resumeButton.setText(R.string.assistant_pause_desc)
            }
            AgentRuntimePhase.PAUSED -> {
                spinner.visibility = View.GONE
                icon.visibility = View.VISIBLE
                icon.setImageResource(R.drawable.ic_pause)
                icon.imageTintList = colorStateList(R.color.lw_accent)
                dismissIcon.visibility = View.GONE
                label.setText(R.string.overlay_pill_paused)
                resumeButton.setText(R.string.overlay_action_resume)
            }
            AgentRuntimePhase.WAITING_CONFIRMATION -> {
                spinner.visibility = View.GONE
                icon.visibility = View.VISIBLE
                icon.setImageResource(R.drawable.ic_sparkle)
                icon.imageTintList = colorStateList(R.color.lw_record_coral)
                dismissIcon.visibility = View.GONE
                label.setText(R.string.overlay_pill_confirm)
            }
            AgentRuntimePhase.COMPLETED -> {
                spinner.visibility = View.GONE
                icon.visibility = View.VISIBLE
                icon.setImageResource(R.drawable.ic_check_circle)
                icon.imageTintList = colorStateList(R.color.lw_success)
                dismissIcon.visibility = View.VISIBLE
                label.setText(R.string.overlay_pill_done)
            }
            AgentRuntimePhase.ERROR -> {
                spinner.visibility = View.GONE
                icon.visibility = View.VISIBLE
                icon.setImageResource(R.drawable.ic_close)
                icon.imageTintList = colorStateList(R.color.lw_error)
                dismissIcon.visibility = View.VISIBLE
                label.setText(R.string.overlay_pill_error)
            }
            AgentRuntimePhase.IDLE -> return
        }

        val showQuickActions = quickControlsExpanded &&
            (state.phase == AgentRuntimePhase.PAUSED || state.phase == AgentRuntimePhase.THINKING)
        actionsRow.visibility = if (showQuickActions) View.VISIBLE else View.GONE
        root.visibility = if (temporarilyHidden) View.INVISIBLE else View.VISIBLE
    }

    @SuppressLint("ClickableViewAccessibility")
    private fun ensurePillCreated() {
        if (pillRoot != null || windowManager == null) return

        val root = LinearLayout(themedContext).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.END
        }

        val pillRow = LinearLayout(themedContext).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            setBackgroundResource(R.drawable.bg_floating_pill)
            setPadding(dp(12), dp(8), dp(12), dp(8))
            elevation = dp(8).toFloat()
        }

        val spinner = ProgressBar(themedContext, null, android.R.attr.progressBarStyleSmall).apply {
            isIndeterminate = true
            indeterminateTintList = colorStateList(R.color.lw_accent)
            layoutParams = LinearLayout.LayoutParams(dp(18), dp(18)).apply {
                marginEnd = dp(8)
            }
        }

        val icon = ImageView(themedContext).apply {
            layoutParams = LinearLayout.LayoutParams(dp(18), dp(18)).apply {
                marginEnd = dp(8)
            }
            visibility = View.GONE
        }

        val label = TextView(themedContext).apply {
            textSize = 13f
            setTypeface(typeface, Typeface.BOLD)
            setTextColor(color(R.color.lw_text_primary))
            isSingleLine = true
            ellipsize = TextUtils.TruncateAt.END
            maxWidth = dp(156)
        }

        val dismissIcon = ImageView(themedContext).apply {
            setImageResource(R.drawable.ic_close)
            imageTintList = colorStateList(R.color.lw_text_secondary)
            contentDescription = themedContext.getString(R.string.overlay_action_dismiss)
            setPadding(dp(6), dp(2), 0, dp(2))
            layoutParams = LinearLayout.LayoutParams(dp(24), dp(24))
            visibility = View.GONE
            setOnClickListener { v ->
                v.performHapticFeedback(HapticFeedbackConstants.KEYBOARD_TAP)
                AgentRuntimeManager.dismissFinishedState()
            }
        }

        pillRow.addView(spinner)
        pillRow.addView(icon)
        pillRow.addView(label)
        pillRow.addView(dismissIcon)

        val actionsRow = LinearLayout(themedContext).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            setBackgroundResource(R.drawable.bg_floating_pill)
            setPadding(dp(8), dp(6), dp(8), dp(6))
            elevation = dp(8).toFloat()
            visibility = View.GONE
            layoutParams = LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT,
                ViewGroup.LayoutParams.WRAP_CONTENT
            ).apply {
                topMargin = dp(6)
            }
        }

        val resumeButton = buildQuickActionChip(
            R.string.overlay_action_resume,
            R.color.lw_accent
        ) {
            AgentRuntimeManager.togglePause()
        }
        val interjectButton = buildQuickActionChip(
            R.string.overlay_action_interject,
            R.color.lw_accent
        ) {
            AgentRuntimeManager.pause()
            openAssistantActivity(focusInterject = true)
        }
        val openButton = buildQuickActionChip(
            R.string.overlay_action_open,
            R.color.lw_text_primary
        ) {
            openAssistantActivity()
        }
        val stopButton = buildQuickActionChip(
            R.string.overlay_action_stop,
            R.color.lw_error
        ) {
            AgentRuntimeManager.stop()
        }

        actionsRow.addView(resumeButton)
        actionsRow.addView(interjectButton)
        actionsRow.addView(openButton)
        actionsRow.addView(stopButton)

        root.addView(pillRow)
        root.addView(actionsRow)

        val screenBounds = screenMetricsBounds()
        val topInset = systemTopInset()
        val params = WindowManager.LayoutParams(
            WindowManager.LayoutParams.WRAP_CONTENT,
            WindowManager.LayoutParams.WRAP_CONTENT,
            WindowManager.LayoutParams.TYPE_ACCESSIBILITY_OVERLAY,
            WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE or
                WindowManager.LayoutParams.FLAG_NOT_TOUCH_MODAL or
                WindowManager.LayoutParams.FLAG_LAYOUT_IN_SCREEN,
            PixelFormat.TRANSLUCENT
        ).apply {
            gravity = Gravity.TOP or Gravity.START
            x = (screenBounds.width - dp(152) - dp(EDGE_MARGIN_DP)).coerceAtLeast(dp(EDGE_MARGIN_DP))
            y = topInset + dp(DEFAULT_TOP_OFFSET_DP)
        }

        attachPillDragAndTapListener(pillRow, root, params)

        try {
            windowManager.addView(root, params)
            pillRoot = root
            pillParams = params
            pillSpinner = spinner
            pillIcon = icon
            pillLabel = label
            pillDismissIcon = dismissIcon
            pillActionsRow = actionsRow
            pillResumeButton = resumeButton
        } catch (exc: Exception) {
            Log.w(TAG, "Could not attach floating corner pill", exc)
        }
    }

    private fun buildQuickActionChip(
        textRes: Int,
        @ColorRes colorRes: Int,
        onClick: () -> Unit
    ): TextView {
        return TextView(themedContext).apply {
            setText(textRes)
            textSize = 12f
            setTypeface(typeface, Typeface.BOLD)
            setTextColor(color(colorRes))
            setPadding(dp(10), dp(6), dp(10), dp(6))
            setOnClickListener { v ->
                v.performHapticFeedback(HapticFeedbackConstants.KEYBOARD_TAP)
                onClick()
            }
        }
    }

    @SuppressLint("ClickableViewAccessibility")
    private fun attachPillDragAndTapListener(
        touchTarget: View,
        windowRoot: View,
        params: WindowManager.LayoutParams
    ) {
        var downRawX = 0f
        var downRawY = 0f
        var startX = 0
        var startY = 0
        var dragging = false

        touchTarget.setOnTouchListener { view, event ->
            when (event.actionMasked) {
                MotionEvent.ACTION_DOWN -> {
                    downRawX = event.rawX
                    downRawY = event.rawY
                    startX = params.x
                    startY = params.y
                    dragging = false
                    true
                }
                MotionEvent.ACTION_MOVE -> {
                    val dx = (event.rawX - downRawX).toInt()
                    val dy = (event.rawY - downRawY).toInt()
                    if (!dragging && (abs(dx) > touchSlop || abs(dy) > touchSlop)) {
                        dragging = true
                    }
                    if (dragging) {
                        val bounds = screenMetricsBounds()
                        val margin = dp(EDGE_MARGIN_DP)
                        val maxX = (bounds.width - windowRoot.width - margin).coerceAtLeast(margin)
                        val minY = systemTopInset() + dp(8)
                        val maxY = (bounds.height - windowRoot.height - systemBottomInset() - dp(8))
                            .coerceAtLeast(minY)
                        params.x = OverlayGeometry.clampCoordinate(startX + dx, margin, maxX)
                        params.y = OverlayGeometry.clampCoordinate(startY + dy, minY, maxY)
                        updateLayoutSafely(windowRoot, params)
                    }
                    true
                }
                MotionEvent.ACTION_UP -> {
                    if (dragging) {
                        val bounds = screenMetricsBounds()
                        params.x = OverlayGeometry.snapXToNearestEdge(
                            currentX = params.x,
                            viewWidth = windowRoot.width.coerceAtLeast(dp(120)),
                            screenWidth = bounds.width,
                            edgeMarginPx = dp(EDGE_MARGIN_DP)
                        )
                        val isLeftSide = params.x < bounds.width / 2
                        (windowRoot as? LinearLayout)?.gravity =
                            if (isLeftSide) Gravity.START else Gravity.END
                        updateLayoutSafely(windowRoot, params)
                    } else {
                        view.performHapticFeedback(HapticFeedbackConstants.KEYBOARD_TAP)
                        onPillTapped()
                    }
                    true
                }
                else -> false
            }
        }
    }

    private fun onPillTapped() {
        when (lastState.phase) {
            AgentRuntimePhase.THINKING -> {
                quickControlsExpanded = true
                AgentRuntimeManager.pause()
            }
            AgentRuntimePhase.PAUSED -> {
                quickControlsExpanded = !quickControlsExpanded
                renderPill(lastState)
            }
            AgentRuntimePhase.WAITING_CONFIRMATION -> {
                val confId = lastState.pendingConfirmation?.confirmationId
                confirmCardHiddenForId = if (confirmCardHiddenForId == confId) null else confId
                renderConfirmationAndHighlight(lastState)
            }
            AgentRuntimePhase.COMPLETED, AgentRuntimePhase.ERROR -> {
                openAssistantActivity()
            }
            AgentRuntimePhase.IDLE -> Unit
        }
    }

    // ------------------------------------------------------------ Confirmation & Highlight

    private fun renderConfirmationAndHighlight(state: AgentRuntimeState) {
        val pending = state.pendingConfirmation
        val shouldShow = state.phase == AgentRuntimePhase.WAITING_CONFIRMATION &&
            pending != null &&
            confirmCardHiddenForId != pending.confirmationId

        if (!shouldShow || pending == null) {
            removeConfirmAndHighlight()
            return
        }

        val targetBox = extractTargetBox(pending.phoneAction)
        renderTargetHighlight(targetBox)
        renderConfirmCard(pending, targetBox)
    }

    private fun renderConfirmCard(pending: PendingConfirmationInfo, targetBox: TargetBox?) {
        ensureConfirmCardCreated()
        val root = confirmRoot ?: return
        val params = confirmParams ?: return
        val titleView = confirmTitleView ?: return
        val descView = confirmDescView ?: return
        val previewView = confirmPreviewImage ?: return

        titleView.setText(
            if (pending.phoneAction != null) {
                R.string.assistant_confirm_phone_title
            } else {
                R.string.assistant_confirm_title
            }
        )
        descView.text = pending.description

        // Phone targets already show a live red highlight on the real screen; PC clicks show the JPEG preview.
        val bitmap = if (pending.phoneAction == null && pending.previewImageB64.isNotBlank()) {
            decodePreviewBitmap(pending.previewImageB64)
        } else {
            null
        }
        previewView.setImageBitmap(bitmap)
        previewView.visibility = if (bitmap != null) View.VISIBLE else View.GONE

        val bounds = screenMetricsBounds()
        val placeAtTop = OverlayGeometry.shouldPlaceConfirmCardAtTop(
            targetCenterY = targetBox?.centerY,
            screenHeight = bounds.height
        )
        params.gravity = (if (placeAtTop) Gravity.TOP else Gravity.BOTTOM) or Gravity.CENTER_HORIZONTAL
        params.y = if (placeAtTop) {
            systemTopInset() + dp(12)
        } else {
            systemBottomInset() + dp(16)
        }
        root.visibility = if (temporarilyHidden) View.INVISIBLE else View.VISIBLE
        updateLayoutSafely(root, params)
    }

    private fun ensureConfirmCardCreated() {
        if (confirmRoot != null || windowManager == null) return

        val card = LinearLayout(themedContext).apply {
            orientation = LinearLayout.VERTICAL
            setBackgroundResource(R.drawable.bg_floating_card)
            setPadding(dp(16), dp(14), dp(16), dp(14))
            elevation = dp(10).toFloat()
        }

        val headerRow = LinearLayout(themedContext).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
        }

        val titleView = TextView(themedContext).apply {
            textSize = 14f
            setTypeface(typeface, Typeface.BOLD)
            setTextColor(color(R.color.lw_error))
            layoutParams = LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f)
        }

        val openText = TextView(themedContext).apply {
            setText(R.string.overlay_action_open)
            textSize = 13f
            setTypeface(typeface, Typeface.BOLD)
            setTextColor(color(R.color.lw_accent))
            setPadding(dp(10), dp(4), dp(4), dp(4))
            setOnClickListener { v ->
                v.performHapticFeedback(HapticFeedbackConstants.KEYBOARD_TAP)
                openAssistantActivity()
            }
        }

        headerRow.addView(titleView)
        headerRow.addView(openText)

        val descView = TextView(themedContext).apply {
            textSize = 14f
            setTextColor(color(R.color.lw_text_primary))
            layoutParams = LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT
            ).apply {
                topMargin = dp(4)
            }
        }

        val previewImage = ImageView(themedContext).apply {
            adjustViewBounds = true
            scaleType = ImageView.ScaleType.FIT_CENTER
            maxHeight = dp(220)
            visibility = View.GONE
            layoutParams = LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT
            ).apply {
                topMargin = dp(8)
            }
        }

        val buttonsRow = LinearLayout(themedContext).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.END or Gravity.CENTER_VERTICAL
            layoutParams = LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT
            ).apply {
                topMargin = dp(12)
            }
        }

        val reasonButton = TextView(themedContext).apply {
            setText(R.string.overlay_action_reject_reason)
            textSize = 13f
            setTypeface(typeface, Typeface.BOLD)
            setTextColor(color(R.color.lw_text_secondary))
            setPadding(dp(8), dp(8), dp(12), dp(8))
            setOnClickListener { v ->
                v.performHapticFeedback(HapticFeedbackConstants.KEYBOARD_TAP)
                openAssistantActivity(focusRejectionReason = true)
            }
        }

        val rejectButton = buildConfirmButton(
            textRes = R.string.assistant_confirm_reject,
            textColorRes = R.color.lw_error,
            bgRes = R.drawable.bg_floating_button_outline
        ) {
            AgentRuntimeManager.submitConfirmation(approved = false)
        }

        val approveButton = buildConfirmButton(
            textRes = R.string.assistant_confirm_approve,
            textColorRes = R.color.lw_accent_on,
            bgRes = R.drawable.bg_floating_button_primary
        ) {
            AgentRuntimeManager.submitConfirmation(approved = true)
        }.apply {
            layoutParams = LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT,
                ViewGroup.LayoutParams.WRAP_CONTENT
            ).apply {
                marginStart = dp(10)
            }
        }

        buttonsRow.addView(reasonButton)
        buttonsRow.addView(rejectButton)
        buttonsRow.addView(approveButton)

        card.addView(headerRow)
        card.addView(descView)
        card.addView(previewImage)
        card.addView(buttonsRow)

        val bounds = screenMetricsBounds()
        val cardWidth = (bounds.width - dp(32)).coerceAtLeast(dp(240))
        val params = WindowManager.LayoutParams(
            cardWidth,
            WindowManager.LayoutParams.WRAP_CONTENT,
            WindowManager.LayoutParams.TYPE_ACCESSIBILITY_OVERLAY,
            WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE or
                WindowManager.LayoutParams.FLAG_NOT_TOUCH_MODAL or
                WindowManager.LayoutParams.FLAG_LAYOUT_IN_SCREEN,
            PixelFormat.TRANSLUCENT
        ).apply {
            gravity = Gravity.BOTTOM or Gravity.CENTER_HORIZONTAL
            y = systemBottomInset() + dp(16)
        }

        try {
            windowManager.addView(card, params)
            confirmRoot = card
            confirmParams = params
            confirmTitleView = titleView
            confirmDescView = descView
            confirmPreviewImage = previewImage
        } catch (exc: Exception) {
            Log.w(TAG, "Could not attach floating confirmation card", exc)
        }
    }

    private fun buildConfirmButton(
        textRes: Int,
        @ColorRes textColorRes: Int,
        @DrawableRes bgRes: Int,
        onClick: () -> Unit
    ): TextView {
        return TextView(themedContext).apply {
            setText(textRes)
            textSize = 14f
            setTypeface(typeface, Typeface.BOLD)
            setTextColor(color(textColorRes))
            setBackgroundResource(bgRes)
            setPadding(dp(18), dp(9), dp(18), dp(9))
            setOnClickListener { v ->
                v.performHapticFeedback(HapticFeedbackConstants.KEYBOARD_TAP)
                onClick()
            }
        }
    }

    private fun renderTargetHighlight(targetBox: TargetBox?) {
        if (targetBox == null || windowManager == null) {
            removeHighlightView()
            return
        }
        val pad = dp(4)
        val params = highlightParams ?: WindowManager.LayoutParams(
            targetBox.w + pad * 2,
            targetBox.h + pad * 2,
            WindowManager.LayoutParams.TYPE_ACCESSIBILITY_OVERLAY,
            WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE or
                WindowManager.LayoutParams.FLAG_NOT_TOUCHABLE or
                WindowManager.LayoutParams.FLAG_LAYOUT_IN_SCREEN or
                WindowManager.LayoutParams.FLAG_LAYOUT_NO_LIMITS,
            PixelFormat.TRANSLUCENT
        ).apply {
            gravity = Gravity.TOP or Gravity.START
        }

        params.x = targetBox.x - pad
        params.y = targetBox.y - pad
        params.width = (targetBox.w + pad * 2).coerceAtLeast(dp(20))
        params.height = (targetBox.h + pad * 2).coerceAtLeast(dp(20))

        val existing = highlightView
        if (existing == null) {
            val boxView = View(themedContext).apply {
                setBackgroundResource(R.drawable.bg_target_highlight)
                visibility = if (temporarilyHidden) View.INVISIBLE else View.VISIBLE
            }
            try {
                windowManager.addView(boxView, params)
                highlightView = boxView
                highlightParams = params
            } catch (exc: Exception) {
                Log.w(TAG, "Could not attach live target highlight", exc)
            }
        } else {
            existing.visibility = if (temporarilyHidden) View.INVISIBLE else View.VISIBLE
            updateLayoutSafely(existing, params)
        }
    }

    // ------------------------------------------------------------ Helpers

    private fun setTemporaryHidden(hidden: Boolean) {
        temporarilyHidden = hidden
        val vis = if (hidden) View.INVISIBLE else View.VISIBLE
        pillRoot?.visibility = vis
        confirmRoot?.visibility = vis
        highlightView?.visibility = vis
    }

    private fun isPointOverlappingPill(px: Int, py: Int): Boolean {
        if (px < 0 || py < 0) return false
        val root = pillRoot ?: return false
        val params = pillParams ?: return false
        if (root.visibility != View.VISIBLE) return false
        val width = root.width.coerceAtLeast(dp(140))
        val height = root.height.coerceAtLeast(dp(44))
        return OverlayGeometry.pointOverlapsRect(
            px = px,
            py = py,
            left = params.x,
            top = params.y,
            right = params.x + width,
            bottom = params.y + height,
            paddingPx = dp(12)
        )
    }

    private fun openAssistantActivity(
        focusInterject: Boolean = false,
        focusRejectionReason: Boolean = false
    ) {
        val intent = Intent(service, AssistantActivity::class.java).apply {
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP)
            putExtra(AssistantActivity.EXTRA_ATTACH_ACTIVE_AGENT, true)
            if (focusInterject) {
                putExtra(AssistantActivity.EXTRA_FOCUS_INTERJECT, true)
            }
            if (focusRejectionReason) {
                putExtra(AssistantActivity.EXTRA_FOCUS_REJECTION_REASON, true)
            }
        }
        try {
            service.startActivity(intent)
        } catch (exc: Exception) {
            Log.w(TAG, "Could not reopen AssistantActivity from floating overlay", exc)
        }
    }

    private fun hideAllWindows() {
        removeConfirmAndHighlight()
        removeViewSafely(pillRoot)
        pillRoot = null
        pillParams = null
        pillSpinner = null
        pillIcon = null
        pillLabel = null
        pillDismissIcon = null
        pillActionsRow = null
        pillResumeButton = null
    }

    private fun removeConfirmAndHighlight() {
        removeViewSafely(confirmRoot)
        confirmRoot = null
        confirmParams = null
        confirmTitleView = null
        confirmDescView = null
        confirmPreviewImage = null
        removeHighlightView()
    }

    private fun removeHighlightView() {
        removeViewSafely(highlightView)
        highlightView = null
        highlightParams = null
    }

    private fun removeViewSafely(view: View?) {
        if (view == null || windowManager == null) return
        try {
            windowManager.removeViewImmediate(view)
        } catch (_: Exception) {
            // View was already detached.
        }
    }

    private fun updateLayoutSafely(view: View, params: WindowManager.LayoutParams) {
        val wm = windowManager ?: return
        try {
            wm.updateViewLayout(view, params)
        } catch (exc: Exception) {
            Log.w(TAG, "Failed to update floating overlay layout", exc)
        }
    }

    private data class ScreenBounds(val width: Int, val height: Int)

    private data class TargetBox(val x: Int, val y: Int, val w: Int, val h: Int) {
        val centerY: Int get() = y + h / 2
    }

    private fun extractTargetBox(phoneAction: JSONObject?): TargetBox? {
        if (phoneAction == null) return null
        val actionType = phoneAction.optString("action", "")
        if (actionType != "click" && actionType != "type") return null
        val box = phoneAction.optJSONObject("box")
        if (box != null) {
            val bx = box.optInt("x", 0)
            val by = box.optInt("y", 0)
            val bw = box.optInt("w", 0)
            val bh = box.optInt("h", 0)
            if (bw > 0 && bh > 0) return TargetBox(bx, by, bw, bh)
        }
        val px = phoneAction.optInt("x", -1)
        val py = phoneAction.optInt("y", -1)
        if (px > 0 && py > 0) {
            val size = dp(36)
            return TargetBox(px - size / 2, py - size / 2, size, size)
        }
        return null
    }

    private fun decodePreviewBitmap(base64: String): Bitmap? {
        if (base64.isBlank() || base64.length > MAX_PREVIEW_BASE64_CHARS) return null
        return try {
            val bytes = Base64.decode(base64, Base64.DEFAULT)
            BitmapFactory.decodeByteArray(bytes, 0, bytes.size)
        } catch (_: IllegalArgumentException) {
            null
        }
    }

    private fun screenMetricsBounds(): ScreenBounds {
        val wm = windowManager
        if (wm != null) {
            val rect = wm.currentWindowMetrics.bounds
            return ScreenBounds(rect.width(), rect.height())
        }
        val dm = service.resources.displayMetrics
        return ScreenBounds(dm.widthPixels, dm.heightPixels)
    }

    private fun systemTopInset(): Int {
        val wm = windowManager ?: return dp(28)
        return wm.currentWindowMetrics.windowInsets
            .getInsets(WindowInsets.Type.systemBars()).top
    }

    private fun systemBottomInset(): Int {
        val wm = windowManager ?: return dp(24)
        return wm.currentWindowMetrics.windowInsets
            .getInsets(WindowInsets.Type.systemBars()).bottom
    }

    private fun color(@ColorRes resId: Int): Int = ContextCompat.getColor(themedContext, resId)

    private fun colorStateList(@ColorRes resId: Int): ColorStateList = ColorStateList.valueOf(color(resId))

    private fun dp(value: Int): Int = (value * density).toInt()

    companion object {
        private const val TAG = "LW-AgentOverlay"
        private const val EDGE_MARGIN_DP = 10
        private const val DEFAULT_TOP_OFFSET_DP = 56
        private const val OVERLAY_HIDE_SETTLE_MS = 110L
        private const val MAX_PREVIEW_BASE64_CHARS = 600_000
    }
}
