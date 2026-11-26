package me.Integer.localwhisper

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class AndroidActionExecutorTest {

    @Test
    fun alarmTimeDefaultsMissingMinuteToZero() {
        assertEquals(7 to 0, AndroidActionExecutor.resolveAlarmTime(7, -1))
        assertEquals(23 to 59, AndroidActionExecutor.resolveAlarmTime(23, 59))
    }

    @Test
    fun invalidAlarmTimesAreRejectedInsteadOfBecomingMidnight() {
        assertNull(AndroidActionExecutor.resolveAlarmTime(-1, 30))
        assertNull(AndroidActionExecutor.resolveAlarmTime(24, 0))
        assertNull(AndroidActionExecutor.resolveAlarmTime(7, 60))
    }

    @Test
    fun onlyAlarmsAndTimersAreClockIntents() {
        assertTrue(AndroidActionExecutor.isClockIntent("set_alarm"))
        assertTrue(AndroidActionExecutor.isClockIntent(" SET_TIMER "))
        assertFalse(AndroidActionExecutor.isClockIntent("open_url"))
        assertFalse(AndroidActionExecutor.isClockIntent("launch_app"))
    }
}
