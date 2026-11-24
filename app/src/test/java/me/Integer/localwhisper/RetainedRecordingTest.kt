package me.Integer.localwhisper

import org.junit.Assert.assertEquals
import org.junit.Test

class RetainedRecordingTest {

    @Test
    fun headerOnlyFileHasNoDuration() {
        assertEquals(0L, RetainedRecording.wavDurationMs(RetainedRecording.WAV_HEADER_BYTES))
    }

    @Test
    fun oneSecondOfAudio() {
        assertEquals(1000L, RetainedRecording.wavDurationMs(32_044L))
    }

    @Test
    fun halfSecondOfAudio() {
        assertEquals(500L, RetainedRecording.wavDurationMs(16_044L))
    }

    @Test
    fun invalidSizesHaveNoDuration() {
        assertEquals(0L, RetainedRecording.wavDurationMs(-5L))
        assertEquals(0L, RetainedRecording.wavDurationMs(0L))
        assertEquals(0L, RetainedRecording.wavDurationMs(10L))
    }
}
