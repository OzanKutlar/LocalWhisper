package me.Integer.localwhisper

import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class AgentRuntimeLogicTest {

    @Test
    fun pauseGatePassesImmediatelyWhenNotPaused() = runBlocking {
        val gate = PauseGate()
        assertFalse(gate.isPaused)
        gate.awaitUnpaused()
        assertFalse(gate.isPaused)
    }

    @Test
    fun pauseGateSuspendsUntilResumed() = runBlocking {
        val gate = PauseGate()
        gate.pause()
        assertTrue(gate.isPaused)
        var resumedReached = false
        val waiter = launch {
            gate.awaitUnpaused()
            resumedReached = true
        }
        delay(40L)
        assertFalse(resumedReached)
        gate.resume()
        waiter.join()
        assertTrue(resumedReached)
        assertFalse(gate.isPaused)
    }

    @Test
    fun confirmCardAvoidsBottomTargetsByPlacingAtTop() {
        assertTrue(OverlayGeometry.shouldPlaceConfirmCardAtTop(targetCenterY = 1800, screenHeight = 2400))
        assertFalse(OverlayGeometry.shouldPlaceConfirmCardAtTop(targetCenterY = 400, screenHeight = 2400))
        assertFalse(OverlayGeometry.shouldPlaceConfirmCardAtTop(targetCenterY = null, screenHeight = 2400))
    }

    @Test
    fun pillSnapsToNearestHorizontalEdge() {
        assertEquals(
            20,
            OverlayGeometry.snapXToNearestEdge(currentX = 120, viewWidth = 200, screenWidth = 1080, edgeMarginPx = 20)
        )
        assertEquals(
            860,
            OverlayGeometry.snapXToNearestEdge(currentX = 700, viewWidth = 200, screenWidth = 1080, edgeMarginPx = 20)
        )
    }

    @Test
    fun pointOverlapsRectIncludesPadding() {
        assertTrue(
            OverlayGeometry.pointOverlapsRect(
                px = 105,
                py = 195,
                left = 100,
                top = 200,
                right = 300,
                bottom = 260,
                paddingPx = 10
            )
        )
        assertFalse(
            OverlayGeometry.pointOverlapsRect(
                px = 500,
                py = 500,
                left = 100,
                top = 200,
                right = 300,
                bottom = 260,
                paddingPx = 10
            )
        )
    }
}
