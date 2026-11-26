package me.Integer.localwhisper

import android.app.KeyguardManager
import android.content.Context
import android.os.PowerManager
import android.util.Log
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.json.JSONObject

/**
 * Executes incoming server RPC commands received over /ws/phone.
 * Never throws: every error is returned as a structured JSON object.
 */
object PhoneRpcDispatcher {

    private const val TAG = "LW-PhoneRpc"

    suspend fun dispatch(context: Context, command: String, arguments: JSONObject): JSONObject {
        return try {
            when (command) {
                "phone.snapshot" -> handleSnapshot(context)
                "phone.target_preview" -> handleTargetPreview(arguments)
                "phone.ui_action" -> handleUiAction(context, arguments)
                "android.intent" -> handleAndroidIntent(context, arguments)
                "clipboard.read" -> LocalClipboard.readWithForegroundAssist(context)
                "clipboard.write" -> handleClipboardWrite(context, arguments)
                "phone.control" -> PhoneControls.executeControl(context, arguments)
                "phone.communication" -> PhoneControls.executeCommunication(context, arguments)
                "phone.send_file" -> FileHandoff.sendPhoneFileToPc(context, arguments)
                "notifications.list" -> handleNotificationsList(arguments)
                "notifications.reply" -> handleNotificationReply(arguments)
                "notifications.dismiss" -> handleNotificationDismiss(arguments)
                else -> errorResult("unsupported_command", "Unsupported phone command: $command")
            }
        } catch (exc: Exception) {
            Log.w(TAG, "Phone RPC '$command' failed", exc)
            errorResult("phone_error", exc.message ?: "Phone command '$command' failed")
        }
    }

    private fun isPhoneUnlockedAndInteractive(context: Context): Boolean {
        val pm = context.getSystemService(PowerManager::class.java)
        val km = context.getSystemService(KeyguardManager::class.java)
        val interactive = pm?.isInteractive ?: true
        val locked = km?.isKeyguardLocked ?: false
        return interactive && !locked
    }

    private fun handleSnapshot(context: Context): JSONObject {
        if (!isPhoneUnlockedAndInteractive(context)) {
            return errorResult(
                "phone_locked",
                "The phone screen is off or locked. Unlock the phone to inspect or control its screen."
            )
        }
        LocalTypingService.noteRemoteControlActivity()
        val snapshot = LocalTypingService.capturePhoneScreenSnapshot()
        return JSONObject().apply {
            put("ok", true)
            put("phone_screen", snapshot)
        }
    }

    private fun handleTargetPreview(arguments: JSONObject): JSONObject {
        val action = arguments.optJSONObject("action") ?: arguments
        LocalTypingService.noteRemoteControlActivity()
        val b64 = LocalTypingService.renderPhoneTargetPreviewBase64(action)
        return JSONObject().apply {
            put("ok", true)
            put("image_jpeg_b64", b64)
        }
    }

    private suspend fun handleUiAction(context: Context, arguments: JSONObject): JSONObject {
        if (!isPhoneUnlockedAndInteractive(context)) {
            return errorResult(
                "phone_locked",
                "The phone screen is off or locked. Unlock the phone before running screen actions."
            )
        }
        val action = arguments.optJSONObject("action") ?: arguments
        LocalTypingService.noteRemoteControlActivity()
        val actionResult = LocalTypingService.executePhoneUiAction(action)
        val freshSnapshot = LocalTypingService.capturePhoneScreenSnapshot()
        return JSONObject().apply {
            put("ok", actionResult.optBoolean("ok", true))
            put("action_result", actionResult)
            put("phone_screen", freshSnapshot)
        }
    }

    private suspend fun handleAndroidIntent(context: Context, arguments: JSONObject): JSONObject =
        withContext(Dispatchers.Main) {
            val act = arguments.optJSONObject("action") ?: arguments
            val clientAction = AndroidClientAction(
                intentType = act.optString("intent_type", ""),
                label = act.optString("label", ""),
                hour = act.optInt("hour", -1),
                minute = act.optInt("minute", -1),
                seconds = act.optInt("seconds", 0),
                urlOrQuery = act.optString("url_or_query", "")
            )
            val outcome = AndroidActionExecutor.executeSpaced(context, clientAction)
            val ok = !outcome.startsWith("Could not") && !outcome.startsWith("Unsupported")
            JSONObject().apply {
                put("ok", ok)
                put("outcome", outcome)
                if (!ok) put("error", outcome)
            }
        }

    private suspend fun handleClipboardWrite(context: Context, arguments: JSONObject): JSONObject =
        withContext(Dispatchers.Main) {
            val text = arguments.optString("text", "")
            if (text.isEmpty()) {
                return@withContext errorResult("empty_text", "Clipboard text must not be empty")
            }
            val ok = LocalClipboard.copy(context, text)
            JSONObject().apply {
                put("ok", ok)
                put("length", text.length)
                if (!ok) put("error", "Could not copy text to the phone clipboard")
            }
        }

    private fun handleNotificationsList(arguments: JSONObject): JSONObject {
        if (!LocalWhisperNotificationListener.isRunning()) {
            return errorResult(
                "notification_access_disabled",
                "Notification access is disabled on the phone. Enable 'Notification access' in LocalWhisper settings."
            )
        }
        return NotificationStore.listJson(
            appQuery = arguments.optString("app_query", ""),
            search = arguments.optString("search", ""),
            sinceMinutes = arguments.optInt("since_minutes", 0)
        )
    }

    private fun handleNotificationReply(arguments: JSONObject): JSONObject {
        val id = arguments.optString("notification_id", "")
        val text = arguments.optString("text", "")
        return LocalWhisperNotificationListener.replyToNotification(id, text)
    }

    private fun handleNotificationDismiss(arguments: JSONObject): JSONObject {
        val id = arguments.optString("notification_id", "")
        return LocalWhisperNotificationListener.dismissNotification(id)
    }

    private fun errorResult(reason: String, message: String): JSONObject = JSONObject().apply {
        put("ok", false)
        put("reason", reason)
        put("error", message)
    }
}
