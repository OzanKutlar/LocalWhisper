package me.Integer.localwhisper

import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.first

/**
 * Suspendable pause gate for the background agent loop.
 * When paused, callers hitting [awaitUnpaused] suspend cleanly until [resume] is called
 * or the parent coroutine is cancelled.
 */
class PauseGate {
    private val pausedFlow = MutableStateFlow(false)

    val isPaused: Boolean
        get() = pausedFlow.value

    fun pause() {
        pausedFlow.value = true
    }

    fun resume() {
        pausedFlow.value = false
    }

    suspend fun awaitUnpaused() {
        if (!pausedFlow.value) return
        pausedFlow.first { !it }
    }
}

/** Pure layout calculations for the floating corner pill and confirmation card. */
object OverlayGeometry {

    /** Places the floating confirmation card at the top when the target sits in the lower half. */
    fun shouldPlaceConfirmCardAtTop(targetCenterY: Int?, screenHeight: Int): Boolean {
        if (targetCenterY == null || screenHeight <= 0) return false
        return targetCenterY > screenHeight / 2
    }

    /** Snaps a dragged overlay's horizontal coordinate to whichever screen edge is closer. */
    fun snapXToNearestEdge(
        currentX: Int,
        viewWidth: Int,
        screenWidth: Int,
        edgeMarginPx: Int
    ): Int {
        if (screenWidth <= 0) return edgeMarginPx
        val safeMargin = edgeMarginPx.coerceAtLeast(0)
        val maxRight = (screenWidth - viewWidth - safeMargin).coerceAtLeast(safeMargin)
        val centerX = currentX + viewWidth / 2
        return if (centerX < screenWidth / 2) safeMargin else maxRight
    }

    fun clampCoordinate(value: Int, minVal: Int, maxVal: Int): Int {
        val safeMax = maxOf(minVal, maxVal)
        return value.coerceIn(minVal, safeMax)
    }

    fun pointOverlapsRect(
        px: Int,
        py: Int,
        left: Int,
        top: Int,
        right: Int,
        bottom: Int,
        paddingPx: Int = 0
    ): Boolean {
        if (px < 0 || py < 0) return false
        val pad = paddingPx.coerceAtLeast(0)
        return px in (left - pad)..(right + pad) && py in (top - pad)..(bottom + pad)
    }
}
