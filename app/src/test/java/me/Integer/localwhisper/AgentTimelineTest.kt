package me.Integer.localwhisper

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class AgentTimelineTest {

    private fun tool(status: String, summary: String = "Click e4") =
        AgentToolCallInfo(tool = "click_screen_element", status = status, summary = summary)

    @Test
    fun toolStepsLandBetweenThinkingBlocks() {
        val entries = mutableListOf<TimelineEntry>()
        AgentTimeline.appendThinking(entries, "Let me")
        AgentTimeline.appendThinking(entries, " look.")
        AgentTimeline.reconcileTools(entries, listOf(tool("running", "Running: View PC Screen")))
        AgentTimeline.reconcileTools(entries, listOf(tool("ok", "View PC Screen")))
        AgentTimeline.appendThinking(entries, "\n\n")
        AgentTimeline.appendThinking(entries, "Now click.")
        AgentTimeline.reconcileTools(
            entries,
            listOf(tool("ok", "View PC Screen"), tool("pending_confirmation", "Waiting approval: Click e4"))
        )

        assertEquals(
            listOf(
                TimelineEntry.Thinking("Let me look."),
                TimelineEntry.Tool("click_screen_element", "ok", "View PC Screen"),
                TimelineEntry.Thinking("Now click."),
                TimelineEntry.Tool("click_screen_element", "pending_confirmation", "Waiting approval: Click e4")
            ),
            entries
        )
    }

    @Test
    fun resumeUpdatesWaitingStepInPlaceAndSkipsPhoneSteps() {
        val entries = mutableListOf<TimelineEntry>(
            TimelineEntry.Thinking("Plan"),
            TimelineEntry.Tool("click_screen_element", "pending_confirmation", "Waiting approval: Click e4"),
            TimelineEntry.Tool("android", "ok", "Timer set (60s)", local = true)
        )
        AgentTimeline.reconcileTools(entries, listOf(tool("running", "Running: Click e4")))
        AgentTimeline.reconcileTools(entries, listOf(tool("ok", "Click e4")))

        assertEquals(3, entries.size)
        assertEquals(TimelineEntry.Tool("click_screen_element", "ok", "Click e4"), entries[1])
        assertTrue((entries[2] as TimelineEntry.Tool).local)
    }

    @Test
    fun legacyTurnsBecomeThinkingThenTools() {
        val entries = AgentTimeline.fromLegacy(
            "Old thinking",
            listOf(tool("ok"), AgentToolCallInfo(tool = "android", status = "ok", summary = "Alarm set"))
        )
        assertEquals(3, entries.size)
        assertEquals(TimelineEntry.Thinking("Old thinking"), entries[0])
        assertTrue((entries[2] as TimelineEntry.Tool).local)
        assertEquals("Old thinking", AgentTimeline.thinkingText(entries))
        assertEquals(2, AgentTimeline.toolCount(entries))
    }
}
