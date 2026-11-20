package me.Integer.localwhisper

import android.app.SearchManager
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.provider.AlarmClock
import android.util.Log
import java.util.Locale

/**
 * Executes native Android system intents requested by the LocalWhisper Agent
 * (Idea #7: Alarms, Timers, URLs, Web Search, and App Launching).
 */
object AndroidActionExecutor {

    private const val TAG = "LW-AndroidAction"

    fun executeAll(context: Context, actions: List<AndroidClientAction>): List<String> {
        if (actions.isEmpty()) return emptyList()
        val outcomes = ArrayList<String>(actions.size)
        for (action in actions) {
            outcomes.add(executeSingle(context, action))
        }
        return outcomes
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
        val hour = action.hour.coerceIn(0, 23)
        val minute = action.minute.coerceIn(0, 59)
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
        val timeStr = String.format(Locale.US, "%02d:%02d", hour, minute)
        return "Alarm set for $timeStr"
    }

    private fun setTimer(context: Context, action: AndroidClientAction): String {
        val seconds = action.seconds.coerceAtLeast(1)
        val intent = Intent(AlarmClock.ACTION_SET_TIMER).apply {
            putExtra(AlarmClock.EXTRA_LENGTH, seconds)
            if (action.label.isNotBlank()) {
                putExtra(AlarmClock.EXTRA_MESSAGE, action.label)
            }
            putExtra(AlarmClock.EXTRA_SKIP_UI, true)
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        }
        context.startActivity(intent)
        return "Timer set (${seconds}s)"
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
            val fallbackUrl = "https://www.google.com/search?q=" + Uri.encode(trimmed)
            openUrl(context, fallbackUrl)
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
        val resolveInfos = pm.queryIntentActivities(mainIntent, 0)
        val lowerQuery = query.lowercase(Locale.US)
        val matched = resolveInfos.firstOrNull { info ->
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
