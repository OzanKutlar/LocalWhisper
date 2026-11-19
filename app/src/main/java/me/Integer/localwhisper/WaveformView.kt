package me.Integer.localwhisper

import android.content.Context
import android.graphics.Canvas
import android.graphics.Paint
import android.graphics.RectF
import android.util.AttributeSet
import android.util.TypedValue
import android.view.View
import kotlin.math.abs

class WaveformView @JvmOverloads constructor(
    context: Context,
    attrs: AttributeSet? = null,
    defStyleAttr: Int = 0
) : View(context, attrs, defStyleAttr) {

    private val barLevels = FloatArray(BAR_COUNT) { IDLE_LEVEL }
    private val barRect = RectF()
    private val barPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.FILL
        color = resolvePrimaryColor(context)
    }

    fun pushAmplitude(normalizedAmplitude: Float) {
        val clamped = normalizedAmplitude.coerceIn(0f, 1f)
        for (i in 0 until BAR_COUNT - 1) {
            barLevels[i] = barLevels[i + 1]
        }
        barLevels[BAR_COUNT - 1] = IDLE_LEVEL + clamped * (1f - IDLE_LEVEL)
        postInvalidateOnAnimation()
    }

    fun reset() {
        for (i in 0 until BAR_COUNT) {
            barLevels[i] = IDLE_LEVEL
        }
        invalidate()
    }

    override fun onDraw(canvas: Canvas) {
        super.onDraw(canvas)
        val drawWidth = width - paddingLeft - paddingRight
        val drawHeight = height - paddingTop - paddingBottom
        if (drawWidth <= 0 || drawHeight <= 0) return

        val slotWidth = drawWidth.toFloat() / BAR_COUNT
        val barWidth = (slotWidth * 0.52f).coerceAtLeast(4f)
        val radius = barWidth / 2f
        val centerY = paddingTop + drawHeight / 2f
        val halfCount = (BAR_COUNT - 1) / 2f

        for (i in 0 until BAR_COUNT) {
            val distFromCenter = abs(i - halfCount) / halfCount
            val envelope = 1f - (distFromCenter * 0.35f)
            val barHeight = (barLevels[i] * envelope * drawHeight)
                .coerceIn(barWidth, drawHeight.toFloat())
            val left = paddingLeft + i * slotWidth + (slotWidth - barWidth) / 2f
            val top = centerY - barHeight / 2f
            barRect.set(left, top, left + barWidth, top + barHeight)
            canvas.drawRoundRect(barRect, radius, radius, barPaint)
        }
    }

    private fun resolvePrimaryColor(context: Context): Int {
        val typedValue = TypedValue()
        val found = context.theme.resolveAttribute(
            com.google.android.material.R.attr.colorPrimary,
            typedValue,
            true
        )
        return if (found) typedValue.data else 0xFF6750A4.toInt()
    }

    companion object {
        private const val BAR_COUNT = 28
        private const val IDLE_LEVEL = 0.08f
    }
}
