package me.Integer.localwhisper

/** One step of an agent turn, in the order it happened: a block of thinking or a tool call. */
sealed class TimelineEntry {
    data class Thinking(val text: String) : TimelineEntry()

    /** [local] marks steps the phone added itself (Android intents); server updates never touch them. */
    data class Tool(
        val tool: String,
        val status: String,
        val summary: String,
        val local: Boolean = false
    ) : TimelineEntry()
}

/**
 * Builds a chronological timeline from the agent stream.
 *
 * The server sends thinking as text deltas and tool state as the full ordered list of tool
 * records for the turn. A tool record keeps its position in that list while its status changes
 * (running -> ok / waiting approval), so position N maps to the N-th server tool row here.
 */
object AgentTimeline {

    const val LOCAL_TOOL_NAME = "android"

    /** Appends to the current thinking block, or starts a new one after a tool step. */
    fun appendThinking(entries: MutableList<TimelineEntry>, delta: String) {
        if (delta.isEmpty()) return
        val last = entries.lastOrNull()
        if (last is TimelineEntry.Thinking) {
            entries[entries.lastIndex] = TimelineEntry.Thinking(last.text + delta)
        } else if (delta.isNotBlank()) {
            entries.add(TimelineEntry.Thinking(delta.trimStart()))
        }
    }

    /** Updates existing server tool rows in place and appends new ones at the current end. */
    fun reconcileTools(entries: MutableList<TimelineEntry>, tools: List<AgentToolCallInfo>) {
        val serverSlots = entries.indices.filter { index ->
            val entry = entries[index]
            entry is TimelineEntry.Tool && !entry.local
        }
        for ((position, tool) in tools.withIndex()) {
            val updated = TimelineEntry.Tool(tool = tool.tool, status = tool.status, summary = tool.summary)
            if (position < serverSlots.size) {
                entries[serverSlots[position]] = updated
            } else {
                entries.add(updated)
            }
        }
    }

    /** Timeline for turns saved before timelines existed: all thinking first, then the tools. */
    fun fromLegacy(thinking: String, toolCalls: List<AgentToolCallInfo>): List<TimelineEntry> {
        val entries = ArrayList<TimelineEntry>(toolCalls.size + 1)
        if (thinking.isNotBlank()) {
            entries.add(TimelineEntry.Thinking(thinking))
        }
        for (call in toolCalls) {
            entries.add(
                TimelineEntry.Tool(
                    tool = call.tool,
                    status = call.status,
                    summary = call.summary,
                    local = call.tool == LOCAL_TOOL_NAME
                )
            )
        }
        return entries
    }

    fun thinkingText(entries: List<TimelineEntry>): String {
        return entries.filterIsInstance<TimelineEntry.Thinking>()
            .map { it.text.trim() }
            .filter { it.isNotEmpty() }
            .joinToString("\n\n")
    }

    fun toolCalls(entries: List<TimelineEntry>): List<AgentToolCallInfo> {
        return entries.filterIsInstance<TimelineEntry.Tool>()
            .map { AgentToolCallInfo(tool = it.tool, status = it.status, summary = it.summary) }
    }

    fun toolCount(entries: List<TimelineEntry>): Int = entries.count { it is TimelineEntry.Tool }

    fun hasThinking(entries: List<TimelineEntry>): Boolean {
        return entries.any { it is TimelineEntry.Thinking && it.text.isNotBlank() }
    }
}
