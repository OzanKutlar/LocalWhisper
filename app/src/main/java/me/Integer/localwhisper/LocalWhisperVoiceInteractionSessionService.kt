package me.Integer.localwhisper

import android.content.Context
import android.content.Intent
import android.os.Bundle
import android.service.voice.VoiceInteractionSession
import android.service.voice.VoiceInteractionSessionService
import android.util.Log

class LocalWhisperVoiceInteractionSessionService : VoiceInteractionSessionService() {

    override fun onNewSession(args: Bundle?): VoiceInteractionSession {
        return LocalWhisperVoiceInteractionSession(this)
    }
}

private class LocalWhisperVoiceInteractionSession(
    context: Context
) : VoiceInteractionSession(context) {

    override fun onShow(args: Bundle?, showFlags: Int) {
        super.onShow(args, showFlags)
        launchAssistantUi()
        finish()
    }

    private fun launchAssistantUi() {
        val intent = Intent(context, AssistantActivity::class.java).apply {
            // CLEAR_TASK guarantees a fresh task so no stale screen (e.g. settings) sits underneath.
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TASK)
        }
        try {
            startAssistantActivity(intent)
        } catch (exc: Exception) {
            Log.e(TAG, "startAssistantActivity failed, trying startActivity fallback", exc)
            launchFallback(intent)
        }
    }

    private fun launchFallback(intent: Intent) {
        try {
            context.startActivity(intent)
        } catch (exc: Exception) {
            Log.e(TAG, "Fallback startActivity failed; assistant UI not shown", exc)
        }
    }

    companion object {
        private const val TAG = "LW-Session"
    }
}
