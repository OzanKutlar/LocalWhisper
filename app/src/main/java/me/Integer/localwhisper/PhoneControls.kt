package me.Integer.localwhisper

import android.Manifest
import android.app.NotificationManager
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.hardware.camera2.CameraCharacteristics
import android.hardware.camera2.CameraManager
import android.media.AudioManager
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import android.net.Uri
import android.os.BatteryManager
import android.os.Environment
import android.os.StatFs
import android.provider.ContactsContract
import android.view.KeyEvent
import androidx.core.content.ContextCompat
import org.json.JSONObject
import java.util.Locale
import kotlin.math.roundToInt

/**
 * Implements #9 Expanded Native Phone Controls: battery/storage/network status, ringer mode,
 * Do Not Disturb, flashlight, phone media keys, stream volume %, and SMS/dialer drafts.
 */
object PhoneControls {

    fun executeControl(context: Context, arguments: JSONObject): JSONObject {
        return when (val action = arguments.optString("action", "").lowercase(Locale.US)) {
            "get_status" -> getPhoneStatus(context)
            "set_ringer" -> setRingerMode(context, arguments.optString("mode", "normal"))
            "set_dnd" -> setDoNotDisturb(context, arguments.optBoolean("enabled", true))
            "set_flashlight" -> setFlashlight(context, arguments.optBoolean("enabled", true))
            "media_control" -> mediaControl(context, arguments.optString("command", "play_pause"))
            "set_volume" -> setStreamVolume(
                context,
                arguments.optString("stream", "media"),
                arguments.optInt("volume_pct", 50)
            )
            else -> JSONObject().apply {
                put("ok", false)
                put("reason", "unsupported_action")
                put("error", "Unsupported control_phone action: $action")
            }
        }
    }

    fun executeCommunication(context: Context, arguments: JSONObject): JSONObject {
        val action = arguments.optString("action", "").lowercase(Locale.US)
        val rawRecipient = arguments.optString("recipient", "").trim()
        if (rawRecipient.isEmpty()) {
            return JSONObject().apply {
                put("ok", false)
                put("reason", "missing_recipient")
                put("error", "recipient is required")
            }
        }
        val resolvedNumber = resolveContactOrNumber(context, rawRecipient)
            ?: return JSONObject().apply {
                put("ok", false)
                put("reason", "contact_not_found")
                put(
                    "error",
                    "Could not find a phone number for '$rawRecipient'. Grant Contacts permission in LocalWhisper settings or provide the phone number directly."
                )
            }

        return when (action) {
            "draft_sms" -> {
                val body = arguments.optString("message", "")
                val intent = Intent(Intent.ACTION_SENDTO, Uri.parse("smsto:${Uri.encode(resolvedNumber)}")).apply {
                    putExtra("sms_body", body)
                    addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                }
                context.startActivity(intent)
                JSONObject().apply {
                    put("ok", true)
                    put("action", "draft_sms")
                    put("recipient", rawRecipient)
                    put("number", resolvedNumber)
                }
            }
            "open_dialer" -> {
                val intent = Intent(Intent.ACTION_DIAL, Uri.parse("tel:${Uri.encode(resolvedNumber)}")).apply {
                    addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                }
                context.startActivity(intent)
                JSONObject().apply {
                    put("ok", true)
                    put("action", "open_dialer")
                    put("recipient", rawRecipient)
                    put("number", resolvedNumber)
                }
            }
            else -> JSONObject().apply {
                put("ok", false)
                put("error", "Unsupported phone_communication action: $action")
            }
        }
    }

    private fun getPhoneStatus(context: Context): JSONObject {
        val bm = context.getSystemService(BatteryManager::class.java)
        val batteryPct = bm?.getIntProperty(BatteryManager.BATTERY_PROPERTY_CAPACITY) ?: -1
        val isCharging = bm?.isCharging ?: false

        val cm = context.getSystemService(ConnectivityManager::class.java)
        val caps = cm?.activeNetwork?.let { cm.getNetworkCapabilities(it) }
        val network = when {
            caps == null -> "offline"
            caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) -> "wifi"
            caps.hasTransport(NetworkCapabilities.TRANSPORT_CELLULAR) -> "cellular"
            else -> "connected"
        }

        val am = context.getSystemService(AudioManager::class.java)
        val ringer = when (am?.ringerMode) {
            AudioManager.RINGER_MODE_SILENT -> "silent"
            AudioManager.RINGER_MODE_VIBRATE -> "vibrate"
            else -> "normal"
        }
        val nm = context.getSystemService(NotificationManager::class.java)
        val dndActive = (nm?.currentInterruptionFilter ?: NotificationManager.INTERRUPTION_FILTER_ALL) !=
            NotificationManager.INTERRUPTION_FILTER_ALL

        val stat = StatFs(Environment.getDataDirectory().path)
        val freeGb = (stat.availableBytes / (1024.0 * 1024.0 * 1024.0) * 10.0).roundToInt() / 10.0

        return JSONObject().apply {
            put("ok", true)
            put("battery_pct", batteryPct)
            put("charging", isCharging)
            put("network", network)
            put("ringer_mode", ringer)
            put("dnd_enabled", dndActive)
            put("media_volume_pct", readVolumePct(am, AudioManager.STREAM_MUSIC))
            put("ring_volume_pct", readVolumePct(am, AudioManager.STREAM_RING))
            put("free_storage_gb", freeGb)
        }
    }

    private fun readVolumePct(am: AudioManager?, stream: Int): Int {
        if (am == null) return 0
        val max = am.getStreamMaxVolume(stream).coerceAtLeast(1)
        val cur = am.getStreamVolume(stream).coerceAtLeast(0)
        return ((cur * 100.0) / max).roundToInt()
    }

    private fun setRingerMode(context: Context, mode: String): JSONObject {
        val nm = context.getSystemService(NotificationManager::class.java)
        val am = context.getSystemService(AudioManager::class.java)
            ?: return JSONObject().put("ok", false).put("error", "AudioManager unavailable")
        val clean = mode.lowercase(Locale.US)
        if ((clean == "silent" || am.ringerMode == AudioManager.RINGER_MODE_SILENT) &&
            nm != null && !nm.isNotificationPolicyAccessGranted
        ) {
            return JSONObject().apply {
                put("ok", false)
                put("reason", "dnd_permission_required")
                put("error", "Grant 'Do Not Disturb access' in LocalWhisper settings to change silent/DND mode.")
            }
        }
        val target = when (clean) {
            "silent" -> AudioManager.RINGER_MODE_SILENT
            "vibrate" -> AudioManager.RINGER_MODE_VIBRATE
            else -> AudioManager.RINGER_MODE_NORMAL
        }
        am.ringerMode = target
        return JSONObject().apply {
            put("ok", true)
            put("ringer_mode", clean)
        }
    }

    private fun setDoNotDisturb(context: Context, enabled: Boolean): JSONObject {
        val nm = context.getSystemService(NotificationManager::class.java)
            ?: return JSONObject().put("ok", false).put("error", "NotificationManager unavailable")
        if (!nm.isNotificationPolicyAccessGranted) {
            return JSONObject().apply {
                put("ok", false)
                put("reason", "dnd_permission_required")
                put("error", "Grant 'Do Not Disturb access' in LocalWhisper settings to toggle DND.")
            }
        }
        nm.setInterruptionFilter(
            if (enabled) NotificationManager.INTERRUPTION_FILTER_PRIORITY
            else NotificationManager.INTERRUPTION_FILTER_ALL
        )
        return JSONObject().apply {
            put("ok", true)
            put("dnd_enabled", enabled)
        }
    }

    private fun setFlashlight(context: Context, enabled: Boolean): JSONObject {
        val cm = context.getSystemService(CameraManager::class.java)
            ?: return JSONObject().put("ok", false).put("error", "CameraManager unavailable")
        val torchId = cm.cameraIdList.firstOrNull { id ->
            cm.getCameraCharacteristics(id).get(CameraCharacteristics.FLASH_INFO_AVAILABLE) == true
        } ?: return JSONObject().put("ok", false).put("error", "No flashlight found on this device")
        cm.setTorchMode(torchId, enabled)
        return JSONObject().apply {
            put("ok", true)
            put("flashlight_enabled", enabled)
        }
    }

    private fun mediaControl(context: Context, command: String): JSONObject {
        val am = context.getSystemService(AudioManager::class.java)
            ?: return JSONObject().put("ok", false).put("error", "AudioManager unavailable")
        when (val clean = command.lowercase(Locale.US)) {
            "play_pause", "next", "previous" -> {
                val keyCode = when (clean) {
                    "next" -> KeyEvent.KEYCODE_MEDIA_NEXT
                    "previous" -> KeyEvent.KEYCODE_MEDIA_PREVIOUS
                    else -> KeyEvent.KEYCODE_MEDIA_PLAY_PAUSE
                }
                am.dispatchMediaKeyEvent(KeyEvent(KeyEvent.ACTION_DOWN, keyCode))
                am.dispatchMediaKeyEvent(KeyEvent(KeyEvent.ACTION_UP, keyCode))
            }
            "volume_up" -> am.adjustStreamVolume(AudioManager.STREAM_MUSIC, AudioManager.ADJUST_RAISE, 0)
            "volume_down" -> am.adjustStreamVolume(AudioManager.STREAM_MUSIC, AudioManager.ADJUST_LOWER, 0)
            "mute" -> am.adjustStreamVolume(AudioManager.STREAM_MUSIC, AudioManager.ADJUST_MUTE, 0)
            else -> return JSONObject().put("ok", false).put("error", "Unsupported media command: $command")
        }
        return JSONObject().apply {
            put("ok", true)
            put("command", command)
        }
    }

    private fun setStreamVolume(context: Context, streamName: String, volumePct: Int): JSONObject {
        val am = context.getSystemService(AudioManager::class.java)
            ?: return JSONObject().put("ok", false).put("error", "AudioManager unavailable")
        val stream = when (streamName.lowercase(Locale.US)) {
            "ring" -> AudioManager.STREAM_RING
            "notification" -> AudioManager.STREAM_NOTIFICATION
            "alarm" -> AudioManager.STREAM_ALARM
            else -> AudioManager.STREAM_MUSIC
        }
        val clamped = volumePct.coerceIn(0, 100)
        val max = am.getStreamMaxVolume(stream).coerceAtLeast(1)
        val index = ((clamped / 100.0) * max).roundToInt().coerceIn(0, max)
        am.setStreamVolume(stream, index, 0)
        return JSONObject().apply {
            put("ok", true)
            put("stream", streamName)
            put("volume_pct", clamped)
        }
    }

    private fun resolveContactOrNumber(context: Context, query: String): String? {
        val hasLetters = query.any { it.isLetter() }
        if (!hasLetters) return query
        val hasPerm = ContextCompat.checkSelfPermission(
            context,
            Manifest.permission.READ_CONTACTS
        ) == PackageManager.PERMISSION_GRANTED
        if (!hasPerm) return null

        val projection = arrayOf(
            ContactsContract.CommonDataKinds.Phone.DISPLAY_NAME,
            ContactsContract.CommonDataKinds.Phone.NUMBER
        )
        val selection = "${ContactsContract.CommonDataKinds.Phone.DISPLAY_NAME} LIKE ?"
        val args = arrayOf("%$query%")
        context.contentResolver.query(
            ContactsContract.CommonDataKinds.Phone.CONTENT_URI,
            projection,
            selection,
            args,
            null
        )?.use { cursor ->
            val numIdx = cursor.getColumnIndex(ContactsContract.CommonDataKinds.Phone.NUMBER)
            if (numIdx >= 0 && cursor.moveToFirst()) {
                return cursor.getString(numIdx)?.trim()
            }
        }
        return null
    }
}
