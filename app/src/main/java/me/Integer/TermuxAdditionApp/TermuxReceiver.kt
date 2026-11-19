package me.Integer.TermuxAdditionApp

import android.app.ActivityManager
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.provider.Settings
import java.net.Socket

class TermuxReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val replyPort = intent.getIntExtra("reply_port", -1)
        val argument = intent.getStringExtra("my_argument")

        val resultData = if (argument != null && argument.startsWith("kill ")) {
            val packageName = argument.substringAfter("kill ").trim()
            try {
                if (KillAccessibilityService.isRunning) {
                    // Arm the accessibility service specifically for this package
                    KillAccessibilityService.arm(packageName)

                    // 1. Open App Settings for the package
                    val appSettingsIntent = Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS).apply {
                        data = Uri.parse("package:$packageName")
                        addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                    }
                    context.startActivity(appSettingsIntent)
                    
                    // The Accessibility Service will handle the rest
                    "Accessibility Service is attempting to Force Stop $packageName"
                } else {
                    // Fallback to legacy kill
                    val am = context.getSystemService(Context.ACTIVITY_SERVICE) as ActivityManager
                    am.killBackgroundProcesses(packageName)
                    "Accessibility Service NOT running. Attempted legacy kill for $packageName"
                }
            } catch (e: Exception) {
                "Failed to kill $packageName: ${e.message}"
            }
        } else {
            "Hello from your Android App! You sent: $argument. Use 'kill <package_name>' to kill an app."
        }

        // Send the result back to Termux via local TCP
        if (replyPort != -1) {
            Thread {
                try {
                    val socket = Socket("127.0.0.1", replyPort)
                    socket.getOutputStream().write(resultData.toByteArray())
                    socket.close()
                } catch (e: Exception) {
                    e.printStackTrace()
                }
            }.start()
        }
    }
}
