package me.Integer.TermuxAdditionApp

import android.app.ActivityManager
import android.content.Context
import android.content.Intent
import android.util.Log
import androidx.core.app.JobIntentService
import java.net.Socket

class TermuxReplyService : JobIntentService() {

    private val TAG = "TermuxReplyService"

    // A helper method to easily start this service
    companion object {
        private const val JOB_ID = 1000
        fun enqueueWork(context: Context, work: Intent) {
            enqueueWork(context, TermuxReplyService::class.java, JOB_ID, work)
        }
    }

    // This method will be executed on a background thread automatically
    override fun onHandleWork(intent: Intent) {
        val replyPort = intent.getIntExtra("reply_port", -1)
        val argument = intent.getStringExtra("my_argument")

        Log.d(TAG, "Service started. Port: $replyPort, Argument: $argument")

        if (replyPort == -1) {
            Log.e(TAG, "Invalid port number received.")
            return
        }

        var resultData = ""

        if (argument != null && argument.startsWith("kill ")) {
            val packageName = argument.substringAfter("kill ").trim()
            try {
                val am = getSystemService(Context.ACTIVITY_SERVICE) as ActivityManager
                am.killBackgroundProcesses(packageName)
                resultData = "Attempted to kill $packageName"
            } catch (e: Exception) {
                resultData = "Failed to kill $packageName: ${e.message}"
            }
        } else {
            resultData = "Hello from your Android App! You sent: $argument. Use 'kill <package_name>' to kill an app."
        }

        try {
            Log.d(TAG, "Attempting to connect to 127.0.0.1:$replyPort")
            val socket = Socket("127.0.0.1", replyPort)
            socket.getOutputStream().write(resultData.toByteArray())
            socket.close()
            Log.d(TAG, "Successfully sent data to socket and closed it.")
        } catch (e: Exception) {
            Log.e(TAG, "Error sending data back to Termux", e)
        }
    }
}
