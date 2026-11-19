package me.Integer.localwhisper

import android.content.Context
import android.graphics.Canvas
import android.graphics.LinearGradient
import android.graphics.Paint
import android.graphics.RectF
import android.graphics.Shader
import android.os.SystemClock
import android.util.AttributeSet
import android.view.View
import androidx.core.content.ContextCompat
import kotlin.math.log10
import kotlin.math.max
import kotlin.math.min
import kotlin.math.pow

/**
 * Recorder-style waveform: a fixed "now" playhead in the middle, recorded bars scrolling
 * left out of it and fading with age, and placeholder dots on the right drifting into it.
 *
 * pushAmplitude() may be called from any thread; all drawing state lives on the UI thread.
 */
class WaveformView @JvmOverloads constructor(
    context: Context,
    attrs: AttributeSet? = null,
    defStyleAttr: Int = 0
) : View(context, attrs, defStyleAttr) {

    // Ring buffer of bars; `head` is the newest, age k lives at (head - k) mod MAX_BARS.
    private val targets = FloatArray(MAX_BARS)
    private val heights = FloatArray(MAX_BARS)
    private val velocities = FloatArray(MAX_BARS)
    private var head = 0
    private var count = 0

    private var lastSampleAtMs = SystemClock.uptimeMillis()
    private var lastFrameMs = SystemClock.uptimeMillis()
    private var quietSamples = 0
    private var dimFactor = 1f
    private var isAnimating = false

    @Volatile
    private var sampleGeneration = 0

    private val density = resources.displayMetrics.density
    private val barWidthPx = BAR_WIDTH_DP * density
    private val slotPx = (BAR_WIDTH_DP + BAR_GAP_DP) * density
    private val capRadiusPx = CAP_RADIUS_DP * density

    private val barRect = RectF()
    private val barPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply { style = Paint.Style.FILL }
    private val dotPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.FILL
        color = ContextCompat.getColor(context, R.color.lw_text_secondary)
    }
    private val playheadColor = ContextCompat.getColor(context, R.color.lw_record_coral)
    private val glowPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE
        strokeCap = Paint.Cap.ROUND
        strokeWidth = GLOW_WIDTH_DP * density
        color = playheadColor
    }
    private val linePaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE
        strokeCap = Paint.Cap.ROUND
        strokeWidth = PLAYHEAD_WIDTH_DP * density
        color = playheadColor
    }
    private val capPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.FILL
        color = playheadColor
    }

    private val frameRunnable = object : Runnable {
        override fun run() {
            if (!isAnimating) return
            stepAnimation()
            invalidate()
            postOnAnimation(this)
        }
    }

    // ------------------------------------------------------------ Public API

    fun setActive(active: Boolean) {
        if (active == isAnimating) return
        isAnimating = active
        removeCallbacks(frameRunnable)
        if (active) {
            lastFrameMs = SystemClock.uptimeMillis()
            postOnAnimation(frameRunnable)
        }
    }

    /** Raw linear loudness in 0..1. Safe to call from the audio thread. */
    fun pushAmplitude(level: Float) {
        if (level.isNaN()) return
        val clamped = level.coerceIn(0f, 1f)
        val generation = sampleGeneration
        post {
            if (generation == sampleGeneration) {
                addSample(clamped)
            }
        }
    }

    fun reset() {
        sampleGeneration++
        for (i in 0 until MAX_BARS) {
            targets[i] = 0f
            heights[i] = 0f
            velocities[i] = 0f
        }
        head = 0
        count = 0
        quietSamples = 0
        dimFactor = 1f
        lastSampleAtMs = SystemClock.uptimeMillis()
        invalidate()
    }

    // ------------------------------------------------------------ Lifecycle

    override fun onSizeChanged(w: Int, h: Int, oldw: Int, oldh: Int) {
        super.onSizeChanged(w, h, oldw, oldh)
        if (w <= 0) return
        val left = paddingLeft.toFloat()
        val playheadX = paddingLeft + (w - paddingLeft - paddingRight) / 2f
        val colors = intArrayOf(
            ContextCompat.getColor(context, R.color.lw_wave_start),
            ContextCompat.getColor(context, R.color.lw_wave_mid),
            ContextCompat.getColor(context, R.color.lw_wave_end),
            ContextCompat.getColor(context, R.color.lw_wave_hot)
        )
        barPaint.shader = LinearGradient(
            left, 0f, playheadX, 0f,
            colors,
            floatArrayOf(0f, 0.38f, 0.72f, 1f),
            Shader.TileMode.CLAMP
        )
    }

    override fun onDetachedFromWindow() {
        setActive(false)
        super.onDetachedFromWindow()
    }

    // ------------------------------------------------------------ Samples

    private fun addSample(rawLevel: Float) {
        val level = toDisplayLevel(rawLevel)
        head = (head + 1) % MAX_BARS
        targets[head] = level
        heights[head] = 0f
        velocities[head] = 0f
        if (count < MAX_BARS) count++
        lastSampleAtMs = SystemClock.uptimeMillis()
        quietSamples = if (level < SILENCE_LEVEL) min(quietSamples + 1, SILENCE_SAMPLES) else 0
    }

    /** Maps linear loudness to 0..1 on a decibel scale so soft speech still moves. */
    private fun toDisplayLevel(raw: Float): Float {
        val db = 20f * log10(max(raw, MIN_LINEAR_LEVEL))
        return ((db - DB_FLOOR) / (DB_CEILING - DB_FLOOR)).coerceIn(0f, 1f)
    }

    private fun indexForAge(age: Int): Int = (head - age + MAX_BARS) % MAX_BARS

    // ------------------------------------------------------------ Animation

    private fun stepAnimation() {
        val now = SystemClock.uptimeMillis()
        val dt = ((now - lastFrameMs) / 1000f).coerceIn(0f, MAX_FRAME_DT_S)
        lastFrameMs = now
        stepSprings(dt)
        stepDim(dt)
    }

    private fun stepSprings(dt: Float) {
        for (age in 0 until count) {
            val index = indexForAge(age)
            val newest = age == 0
            val stiffness = if (newest) NEWEST_STIFFNESS else SETTLE_STIFFNESS
            val damping = if (newest) NEWEST_DAMPING else SETTLE_DAMPING
            val accel = stiffness * (targets[index] - heights[index]) - damping * velocities[index]
            velocities[index] += accel * dt
            heights[index] = (heights[index] + velocities[index] * dt)
                .coerceIn(MIN_SPRING_HEIGHT, MAX_OVERSHOOT)
        }
    }

    private fun stepDim(dt: Float) {
        val silent = quietSamples >= SILENCE_SAMPLES
        val target = if (silent) DIM_LEVEL else 1f
        val duration = if (silent) DIM_TIME_S else BRIGHTEN_TIME_S
        dimFactor += (target - dimFactor) * (dt / duration).coerceIn(0f, 1f)
    }

    private fun scrollFraction(): Float {
        if (count == 0) return 0f
        val elapsed = (SystemClock.uptimeMillis() - lastSampleAtMs).toFloat()
        return (elapsed / SAMPLE_INTERVAL_MS).coerceIn(0f, 1f)
    }

    // ------------------------------------------------------------ Drawing

    override fun onDraw(canvas: Canvas) {
        super.onDraw(canvas)
        val drawWidth = width - paddingLeft - paddingRight
        val drawHeight = height - paddingTop - paddingBottom
        if (drawWidth <= 0 || drawHeight <= 0) return

        val centerY = paddingTop + drawHeight / 2f
        val playheadX = paddingLeft + drawWidth / 2f
        val halfWidth = drawWidth / 2f
        val fraction = scrollFraction()

        drawRecordedBars(canvas, playheadX, halfWidth, centerY, drawHeight.toFloat(), fraction)
        drawUpcomingDots(canvas, playheadX, halfWidth, centerY, fraction)
        drawPlayhead(canvas, playheadX, drawHeight.toFloat())
    }

    private fun drawRecordedBars(
        canvas: Canvas,
        playheadX: Float,
        halfWidth: Float,
        centerY: Float,
        drawHeight: Float,
        fraction: Float
    ) {
        val visible = min(count, (halfWidth / slotPx).toInt() + 2)
        val maxBarHeight = drawHeight * MAX_BAR_FRACTION
        val radius = barWidthPx / 2f
        for (age in 0 until visible) {
            val centerX = playheadX - (age + fraction + 0.5f) * slotPx
            val distance = (playheadX - centerX) / halfWidth
            if (distance >= 1f) break
            val fade = 1f - distance.pow(FADE_EXPONENT)
            val scale = 1f - SHRINK_AMOUNT * distance
            val barHeight = (heights[indexForAge(age)] * scale * maxBarHeight)
                .coerceIn(barWidthPx, drawHeight)
            barPaint.alpha = (fade * dimFactor * 255f).toInt().coerceIn(0, 255)
            barRect.set(
                centerX - radius,
                centerY - barHeight / 2f,
                centerX + radius,
                centerY + barHeight / 2f
            )
            canvas.drawRoundRect(barRect, radius, radius, barPaint)
        }
    }

    private fun drawUpcomingDots(
        canvas: Canvas,
        playheadX: Float,
        halfWidth: Float,
        centerY: Float,
        fraction: Float
    ) {
        val dotCount = min((halfWidth / slotPx).toInt() + 2, MAX_BARS)
        val radius = barWidthPx / 2f
        for (i in 0 until dotCount) {
            val centerX = playheadX + (i + 1.5f - fraction) * slotPx
            val distance = (centerX - playheadX) / halfWidth
            if (distance >= 1f) break
            val fade = 1f - distance.pow(FADE_EXPONENT)
            dotPaint.alpha = (fade * DOT_ALPHA * dimFactor * 255f).toInt().coerceIn(0, 255)
            canvas.drawCircle(centerX, centerY, radius, dotPaint)
        }
    }

    private fun drawPlayhead(canvas: Canvas, playheadX: Float, drawHeight: Float) {
        val top = paddingTop + capRadiusPx
        val bottom = paddingTop + drawHeight - capRadiusPx
        val alpha = (dimFactor * 255f).toInt().coerceIn(0, 255)

        glowPaint.alpha = (GLOW_ALPHA * alpha).toInt()
        canvas.drawLine(playheadX, top, playheadX, bottom, glowPaint)

        linePaint.alpha = alpha
        canvas.drawLine(playheadX, top, playheadX, bottom, linePaint)

        capPaint.alpha = alpha
        canvas.drawCircle(playheadX, top, capRadiusPx, capPaint)
        canvas.drawCircle(playheadX, bottom, capRadiusPx, capPaint)
    }

    companion object {
        private const val MAX_BARS = 128
        private const val SAMPLE_INTERVAL_MS = 50f // matches the recorder's 50 ms chunks

        private const val BAR_WIDTH_DP = 3.5f
        private const val BAR_GAP_DP = 3f
        private const val MAX_BAR_FRACTION = 0.92f
        private const val FADE_EXPONENT = 1.6f
        private const val SHRINK_AMOUNT = 0.18f
        private const val DOT_ALPHA = 0.45f

        private const val DB_FLOOR = -50f
        private const val DB_CEILING = -6f
        private const val MIN_LINEAR_LEVEL = 1e-5f

        private const val NEWEST_STIFFNESS = 900f
        private const val NEWEST_DAMPING = 27f   // underdamped: small overshoot
        private const val SETTLE_STIFFNESS = 500f
        private const val SETTLE_DAMPING = 45f   // ~critically damped: settles calmly
        private const val MIN_SPRING_HEIGHT = -0.2f
        private const val MAX_OVERSHOOT = 1.2f
        private const val MAX_FRAME_DT_S = 0.033f

        private const val SILENCE_LEVEL = 0.12f
        private const val SILENCE_SAMPLES = 20 // ~1 s of 50 ms chunks
        private const val DIM_LEVEL = 0.6f
        private const val DIM_TIME_S = 0.3f
        private const val BRIGHTEN_TIME_S = 0.12f

        private const val PLAYHEAD_WIDTH_DP = 2f
        private const val GLOW_WIDTH_DP = 7f
        private const val CAP_RADIUS_DP = 3.5f
        private const val GLOW_ALPHA = 0.28f
    }
}
