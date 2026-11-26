package me.Integer.localwhisper

import android.app.Notification
import android.app.PendingIntent
import android.app.RemoteInput
import android.content.Intent
import android.os.Build
import android.os.Bundle
import android.os.Parcelable
import android.service.notification.NotificationListenerService
import android.service.notification.StatusBarNotification
import android.util.Log
import org.json.JSONObject
import java.util.concurrent.ConcurrentHashMap

/**
 * Collects notifications from user-allowed apps into NotificationStore, supports native
 * RemoteInput direct replies, and mirrors notifications to the PC over PhoneLink.
 */
class LocalWhisperNotificationListener : NotificationListenerService() {

    private data class ReplyAction(
        val pendingIntent: PendingIntent,
        val remoteInputs: Array<RemoteInput>
    )

    override fun onListenerConnected() {
        super.onListenerConnected()
        instance = this
        PhoneLink.ensureStarted(applicationContext)
        seedActiveNotifications()
    }

    override fun onListenerDisconnected() {
        release()
        super.onListenerDisconnected()
    }

    override fun onDestroy() {
        release()
        super.onDestroy()
    }

    private fun release() {
        if (instance === this) {
            instance = null
        }
        replyActionsByKey.clear()
    }

    override fun onNotificationPosted(sbn: StatusBarNotification?) {
        if (sbn == null) return
        val stored = processStatusBarNotification(sbn, mirrorIfNew = true)
        if (stored != null && ServerPreferences(this).mirrorNotificationsToPc) {
            PhoneLink.sendEvent("notification_posted", stored.toJson())
        }
    }

    override fun onNotificationRemoved(sbn: StatusBarNotification?) {
        if (sbn == null) return
        replyActionsByKey.remove(sbn.key)
        val removed = NotificationStore.removeBySbnKey(sbn.key) ?: return
        if (ServerPreferences(this).mirrorNotificationsToPc) {
            val payload = JSONObject().apply {
                put("id", removed.id)
                put("package_name", removed.packageName)
            }
            PhoneLink.sendEvent("notification_removed", payload)
        }
    }

    private fun seedActiveNotifications() {
        val current = try {
            activeNotifications
        } catch (exc: Exception) {
            Log.w(TAG, "Could not read active notifications", exc)
            null
        } ?: return
        for (sbn in current) {
            processStatusBarNotification(sbn, mirrorIfNew = false)
        }
    }

    private fun processStatusBarNotification(
        sbn: StatusBarNotification,
        @Suppress("UNUSED_PARAMETER") mirrorIfNew: Boolean
    ): StoredPhoneNotification? {
        val pkg = sbn.packageName?.orEmpty() ?: return null
        val prefs = ServerPreferences(this)
        if (!NotificationStore.shouldAcceptPackage(pkg, prefs.allowedNotificationPackages)) {
            return null
        }
        val notif = sbn.notification ?: return null
        if (sbn.isOngoing) return null
        if ((notif.flags and Notification.FLAG_GROUP_SUMMARY) != 0) return null
        if (notif.category == Notification.CATEGORY_TRANSPORT ||
            notif.category == Notification.CATEGORY_SERVICE ||
            notif.category == Notification.CATEGORY_PROGRESS
        ) {
            return null
        }

        val extras = notif.extras ?: Bundle.EMPTY
        if (extras.containsKey(Notification.EXTRA_MEDIA_SESSION)) return null

        val title = extras.getCharSequence(Notification.EXTRA_CONVERSATION_TITLE)?.toString()
            ?: extras.getCharSequence(Notification.EXTRA_TITLE)?.toString()
            ?: ""
        val bigText = extras.getCharSequence(Notification.EXTRA_BIG_TEXT)?.toString()
        val stdText = extras.getCharSequence(Notification.EXTRA_TEXT)?.toString()
        val bodyText = (bigText ?: stdText).orEmpty()
        val messages = extractMessagingStyleLines(extras)

        val replyAction = findReplyAction(notif)
        if (replyAction != null) {
            replyActionsByKey[sbn.key] = replyAction
        } else {
            replyActionsByKey.remove(sbn.key)
        }

        return NotificationStore.upsert(
            sbnKey = sbn.key,
            packageName = pkg,
            appName = resolveAppLabel(pkg),
            title = title,
            text = bodyText,
            messages = messages,
            postedAtMs = sbn.postTime.takeIf { it > 0L } ?: System.currentTimeMillis(),
            canReply = replyAction != null
        )
    }

    private fun extractMessagingStyleLines(extras: Bundle): List<String> {
        val parcels: Array<Parcelable> = if (Build.VERSION.SDK_INT >= 33) {
            extras.getParcelableArray(Notification.EXTRA_MESSAGES, Parcelable::class.java)
        } else {
            @Suppress("DEPRECATION")
            extras.getParcelableArray(Notification.EXTRA_MESSAGES)
        } ?: return emptyList()

        val lines = ArrayList<String>(parcels.size)
        for (parcel in parcels) {
            val bundle = parcel as? Bundle ?: continue
            val text = bundle.getCharSequence("text")?.toString()?.trim().orEmpty()
            if (text.isEmpty()) continue
            val sender = bundle.getCharSequence("sender")?.toString()?.trim().orEmpty()
            lines.add(if (sender.isNotEmpty()) "$sender: $text" else text)
        }
        return lines
    }

    private fun findReplyAction(notification: Notification): ReplyAction? {
        val actions = notification.actions ?: return null
        for (action in actions) {
            val remoteInputs = action.remoteInputs ?: continue
            val pendingIntent = action.actionIntent ?: continue
            if (remoteInputs.any { it.allowFreeFormInput }) {
                return ReplyAction(pendingIntent, remoteInputs)
            }
        }
        return null
    }

    private fun resolveAppLabel(pkgName: String): String {
        return try {
            val info = packageManager.getApplicationInfo(pkgName, 0)
            packageManager.getApplicationLabel(info)?.toString().orEmpty().ifEmpty { pkgName }
        } catch (_: Exception) {
            pkgName
        }
    }

    companion object {
        private const val TAG = "LW-NotifListener"

        @Volatile
        private var instance: LocalWhisperNotificationListener? = null

        private val replyActionsByKey = ConcurrentHashMap<String, ReplyAction>()

        fun isRunning(): Boolean = instance != null

        fun replyToNotification(notificationId: String, replyText: String): JSONObject {
            val service = instance ?: return JSONObject().apply {
                put("ok", false)
                put("reason", "notification_access_disabled")
                put("error", "Enable 'Notification access' in LocalWhisper settings first.")
            }
            val trimmed = replyText.trim()
            if (trimmed.isEmpty()) {
                return JSONObject().apply {
                    put("ok", false)
                    put("reason", "empty_reply")
                    put("error", "Reply text must not be empty.")
                }
            }
            val entry = NotificationStore.getById(notificationId)
                ?: return JSONObject().apply {
                    put("ok", false)
                    put("reason", "not_found")
                    put("error", "Notification '$notificationId' was not found or has already been dismissed.")
                }
            val action = replyActionsByKey[entry.sbnKey]
                ?: return JSONObject().apply {
                    put("ok", false)
                    put("reason", "not_replyable")
                    put("error", "Notification '$notificationId' (${entry.appName}) does not support direct reply.")
                }

            return try {
                val fillInIntent = Intent()
                val results = Bundle()
                for (input in action.remoteInputs) {
                    if (input.allowFreeFormInput) {
                        results.putCharSequence(input.resultKey, trimmed)
                    }
                }
                RemoteInput.addResultsToIntent(action.remoteInputs, fillInIntent, results)
                action.pendingIntent.send(service, 0, fillInIntent)
                JSONObject().apply {
                    put("ok", true)
                    put("id", entry.id)
                    put("app_name", entry.appName)
                    put("title", entry.title)
                    put("replied_text", trimmed)
                }
            } catch (exc: Exception) {
                Log.w(TAG, "Failed to send RemoteInput reply for ${entry.id}", exc)
                JSONObject().apply {
                    put("ok", false)
                    put("reason", "reply_failed")
                    put("error", "Could not send reply: ${exc.message ?: "error"}")
                }
            }
        }

        fun dismissNotification(notificationId: String): JSONObject {
            val service = instance ?: return JSONObject().apply {
                put("ok", false)
                put("reason", "notification_access_disabled")
                put("error", "Enable 'Notification access' in LocalWhisper settings first.")
            }
            val entry = NotificationStore.getById(notificationId)
                ?: return JSONObject().apply {
                    put("ok", false)
                    put("reason", "not_found")
                    put("error", "Notification '$notificationId' was not found.")
                }
            return try {
                service.cancelNotification(entry.sbnKey)
                replyActionsByKey.remove(entry.sbnKey)
                NotificationStore.removeBySbnKey(entry.sbnKey)
                if (ServerPreferences(service).mirrorNotificationsToPc) {
                    PhoneLink.sendEvent(
                        "notification_removed",
                        JSONObject().put("id", entry.id).put("package_name", entry.packageName)
                    )
                }
                JSONObject().apply {
                    put("ok", true)
                    put("id", entry.id)
                    put("app_name", entry.appName)
                    put("dismissed", true)
                }
            } catch (exc: Exception) {
                JSONObject().apply {
                    put("ok", false)
                    put("error", "Could not dismiss notification: ${exc.message ?: "error"}")
                }
            }
        }
    }
}
