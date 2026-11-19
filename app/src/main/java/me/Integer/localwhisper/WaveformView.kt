package me.Integer.localwhisper

import android.content.Context
import android.graphics.Canvas
import android.graphics.LinearGradient
import android.graphics.Paint
import android.graphics.RectF
import android.graphics.Shader
import android.util.AttributeSet
import android.view.View
import androidx.core.content.ContextCompat
import kotlin.math.abs
import kotlin.math.exp
import kotlin.math.sin

class WaveformView @JvmOverloads constructor(
    context: Context,
    attrs: AttributeSet? = null,
    defStyleAttr: Int = 0
) : View(context, attrs, defStyleAttr) {

    private val displayedLevels = FloatArray(BAR_COUNT) { IDLE_LEVEL }
    private val barRect = RectF()
    private val barPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.FILL
    }

    @Volatile
    private var latestAmplitude = 0f
    private var smoothedAmplitude = 0f
    private var wavePhase = 0f
    private var isAnimating = false

    private val frameRunnable = object : Runnable {
        override fun run() {
            if (!isAnimating) return
            stepAnimation()
            invalidate()
            postOnAnimation(this)
        }
    }

    fun setActive(active: Boolean) {
        if (active == isAnimating) return
        isAnimating = active
        removeCallbacks(frameRunnable)
        if (active) {
            postOnAnimation(frameRunnable)
        }
    }

    fun pushAmplitude(normalizedAmplitude: Float) {
        latestAmplitude = normalizedAmplitude.coerceIn(0f, 1f)
    }

    fun reset() {
        latestAmplitude = 0f
        smoothedAmplitude = 0f
        wavePhase = 0f
        for (i in 0 until BAR_COUNT) {
            displayedLevels[i] = IDLE_LEVEL
        }
        invalidate()
    }

    override fun onSizeChanged(w: Int, h: Int, oldw: Int, oldh: Int) {
        super.onSizeChanged(w, h, oldw, oldh)
        if (w <= 0) return
        val colors = intArrayOf(
            ContextCompat.getColor(context, R.color.lw_wave_start),
            ContextCompat.getColor(context, R.color.lw_wave_mid),
            ContextCompat.getColor(context, R.color.lw_wave_end)
        )
        barPaint.shader = LinearGradient(
            0f, 0f, w.toFloat(), 0f,
            colors,
            floatArrayOf(0f, 0.5f, 1f),
            Shader.TileMode.CLAMP
        )
    }

    override fun onDetachedFromWindow() {
        setActive(false)
        super.onDetachedFromWindow()
    }

    private fun stepAnimation() {
        wavePhase += PHASE_SPEED
        val raw = latestAmplitude
        val ampLerp = if (raw > smoothedAmplitude) 0.42f else 0.16f
        smoothedAmplitude += (raw - smoothedAmplitude) * ampLerp

        val center = (BAR_COUNT - 1) / 2f
        for (i in 0 until BAR_COUNT) {
            val normDist = abs(i - center) / center
            val bellEnvelope = exp((-normDist * normDist * 2.5).toDouble()).toFloat()
            val ripple = 0.52f + 0.48f * sin((wavePhase - normDist * 5.2f).toDouble()).toFloat()
            val secondary = 0.72f + 0.28f * sin((wavePhase * 1.7f + i * 0.65f).toDouble()).toFloat()

            val idleBreathing = IDLE_LEVEL + 0.09f * bellEnvelope * ripple
            val voiceEnergy = smoothedAmplitude * bellEnvelope * (0.45f + 0.55f * ripple * secondary)
            val target = (idleBreathing + voiceEnergy * 0.92f).coerceIn(IDLE_LEVEL, 1f)

            val barLerp = if (target > displayedLevels[i]) 0.36f else 0.18f
            displayedLevels[i] += (target - displayedLevels[i]) * barLerp
        }
    }

    override fun onDraw(canvas: Canvas) {
        super.onDraw(canvas)
        val drawWidth = width - paddingLeft - paddingRight
        val drawHeight = height - paddingTop - paddingBottom
        if (drawWidth <= 0 || drawHeight <= 0) return

        val slotWidth = drawWidth.toFloat() / BAR_COUNT
        val barWidth = (slotWidth * 0.48f).coerceAtLeast(4f)
        val radius = barWidth / 2f
        val centerY = paddingTop + drawHeight / 2f

        for (i in 0 until BAR_COUNT) {
            val barHeight = (displayedLevels[i] * drawHeight).coerceIn(barWidth, drawHeight.toFloat())
            val left = paddingLeft + i * slotWidth + (slotWidth - barWidth) / 2f
            val top = centerY - barHeight / 2f
            barRect.set(left, top, left + barWidth, top + barHeight)
            canvas.drawRoundRect(barRect, radius, radius, barPaint)
        }
    }

    companion object {
        private const val BAR_COUNT = 31
        private const val IDLE_LEVEL = 0.07f
        private const val PHASE_SPEED = 0.14f
    }
}
