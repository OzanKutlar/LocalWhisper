package me.Integer.localwhisper

import android.app.SearchManager
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.SystemClock
import android.provider.AlarmClock
import android.util.Log
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext
import java.util.Locale

/**
 * Executes native Android system intents requested by the LocalWhisper Agent
 * (alarms, timers, URLs, web search and app launching).
 *
 * Clock apps handle one SET_ALARM / SET_TIMER request at a time and drop requests that arrive
 * while the previous one is still being processed, so clock intents are spaced out.
 */
object AndroidActionExecutor {

    private const val TAG = "LW-AndroidAction"
    private const val CLOCK_INTENT_GAP_MS = 1_500L
    private const val MAX_ACTIONS_PER_TURN = 10
    private val CLOCK_INTENT_TYPES = setOf("set_alarm", "set_timer")

    private val clockMutex = Mutex()

    @Volatile
    private var lastClockIntentAtMs = 0L

    /** True for intents handled by the clock app, which must not be sent back to back. */
    fun isClockIntent(intentType: String): Boolean =
        intentType.trim().lowercase(Locale.US) in CLOCK_INTENT_TYPES

    /** (hour, minute) for an alarm, or null when the time is invalid. A missing minute (-1) means :00. */
    fun resolveAlarmTime(hour: Int, minute: Int): Pair<Int, Int>? {
        val cleanMinute = if (minute < 0) 0 else minute
        if (hour !in 0..23 || cleanMinute !in 0..59) return null
        return hour to cleanMinute
    }

    /** Runs actions in order; clock intents wait for the previous one to settle. */
    suspend fun executeAllSpaced(context: Context, actions: List<AndroidClientAction>): List<String> {
        if (actions.isEmpty()) return emptyList()
        val outcomes = ArrayList<String>(minOf(actions.size, MAX_ACTIONS_PER_TURN))
        for (action in actions.take(MAX_ACTIONS_PER_TURN)) {
            outcomes.add(executeSpaced(context, action))
        }
        if (actions.size > MAX_ACTIONS_PER_TURN) {
            outcomes.add("Skipped ${actions.size - MAX_ACTIONS_PER_TURN} extra phone actions (limit $MAX_ACTIONS_PER_TURN)")
        }
        return outcomes
    }

    /** Runs one action; clock intents are serialized at least [CLOCK_INTENT_GAP_MS] apart. */
    suspend fun executeSpaced(context: Context, action: AndroidClientAction): String {
        if (!isClockIntent(action.intentType)) {
            return withContext(Dispatchers.Main) { executeSingle(context, action) }
        }
        return clockMutex.withLock {
            val waitMs = lastClockIntentAtMs + CLOCK_INTENT_GAP_MS - SystemClock.elapsedRealtime()
            if (lastClockIntentAtMs > 0L && waitMs > 0L) {
                delay(waitMs.coerceAtMost(CLOCK_INTENT_GAP_MS))
            }
            val outcome = withContext(Dispatchers.Main) { executeSingle(context, action) }
            lastClockIntentAtMs = SystemClock.elapsedRealtime()
            Log.i(TAG, "Clock intent ${action.intentType}: $outcome")
            outcome
        }
    }

    /** Kept for callers that cannot suspend; does not space out clock intents. */
    fun executeAll(context: Context, actions: List<AndroidClientAction>): List<String> {
        if (actions.isEmpty()) return emptyList()
        return actions.take(MAX_ACTIONS_PER_TURN).map { executeSingle(context, it) }
    }

    fun executeSingle(context: Context, action: AndroidClientAction): String {
        return try {
            when (action.intentType.lowercase(Locale.US)) {
                "set_alarm" -> setAlarm(context, action)
                "set_timer" -> setTimer(context, action)
                "open_url" -> openUrl(context, action.urlOrQuery.ifEmpty { action.label })
                "web_search" -> webSearch(context, action.urlOrQuery.ifEmpty { action.label })
                "launch_app" -> launchApp(context, action.label.ifEmpty { action.urlOrQuery })
                else -> "Unsupported phone action: ${action.intentType}"
            }
        } catch (exc: Exception) {
            Log.w(TAG, "Failed to execute Android action ${action.intentType}", exc)
            "Could not run ${action.intentType}: ${exc.message ?: "error"}"
        }
    }

    private fun setAlarm(context: Context, action: AndroidClientAction): String {
        val (hour, minute) = resolveAlarmTime(action.hour, action.minute)
            ?: throw IllegalArgumentException(
                "alarm needs hour 0-23 and minute 0-59 (got ${action.hour}:${action.minute})"
            )
        val intent = Intent(AlarmClock.ACTION_SET_ALARM).apply {
            putExtra(AlarmClock.EXTRA_HOUR, hour)
            putExtra(AlarmClock.EXTRA_MINUTES, minute)
            if (action.label.isNotBlank()) {
                putExtra(AlarmClock.EXTRA_MESSAGE, action.label)
            }
            putExtra(AlarmClock.EXTRA_SKIP_UI, true)
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        }
        context.startActivity(intent)
        return "Alarm set for " + String.format(Locale.US, "%02d:%02d", hour, minute)
    }

    private fun setTimer(context: Context, action: AndroidClientAction): String {
        require(action.seconds > 0) { "timer needs a positive number of seconds" }
        val intent = Intent(AlarmClock.ACTION_SET_TIMER).apply {
            putExtra(AlarmClock.EXTRA_LENGTH, action.seconds)
            if (action.label.isNotBlank()) {
                putExtra(AlarmClock.EXTRA_MESSAGE, action.label)
            }
            putExtra(AlarmClock.EXTRA_SKIP_UI, true)
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        }
        context.startActivity(intent)
        return "Timer set (${action.seconds}s)"
    }

    private fun openUrl(context: Context, rawUrl: String): String {
        val trimmed = rawUrl.trim()
        require(trimmed.isNotEmpty()) { "URL is empty" }
        val normalized = if ("://" in trimmed) trimmed else "https://$trimmed"
        val intent = Intent(Intent.ACTION_VIEW, Uri.parse(normalized)).apply {
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        }
        context.startActivity(intent)
        return "Opened $normalized"
    }

    private fun webSearch(context: Context, query: String): String {
        val trimmed = query.trim()
        require(trimmed.isNotEmpty()) { "Search query is empty" }
        val searchIntent = Intent(Intent.ACTION_WEB_SEARCH).apply {
            putExtra(SearchManager.QUERY, trimmed)
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        }
        return try {
            context.startActivity(searchIntent)
            "Searched '$trimmed'"
        } catch (_: Exception) {
            openUrl(context, "https://www.google.com/search?q=" + Uri.encode(trimmed))
        }
    }

    private fun launchApp(context: Context, targetApp: String): String {
        val query = targetApp.trim()
        require(query.isNotEmpty()) { "App name is empty" }
        val pm = context.packageManager

        pm.getLaunchIntentForPackage(query)?.let { directIntent ->
            directIntent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            context.startActivity(directIntent)
            return "Launched $query"
        }

        val mainIntent = Intent(Intent.ACTION_MAIN, null).apply {
            addCategory(Intent.CATEGORY_LAUNCHER)
        }
        val lowerQuery = query.lowercase(Locale.US)
        val matched = pm.queryIntentActivities(mainIntent, 0).firstOrNull { info ->
            val label = info.loadLabel(pm)?.toString()?.lowercase(Locale.US).orEmpty()
            val pkg = info.activityInfo?.packageName?.lowercase(Locale.US).orEmpty()
            label == lowerQuery || label.contains(lowerQuery) || pkg.contains(lowerQuery)
        }
        val targetPkg = matched?.activityInfo?.packageName
            ?: throw IllegalArgumentException("App '$query' not found on this phone")
        val launchIntent = pm.getLaunchIntentForPackage(targetPkg)
            ?: throw IllegalArgumentException("Cannot launch '$query'")
        launchIntent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        context.startActivity(launchIntent)
        return "Launched ${matched.loadLabel(pm)}"
    }
}
