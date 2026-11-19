package me.Integer.localwhisper

import android.content.Intent
import android.os.RemoteException
import android.speech.RecognitionService
import android.speech.SpeechRecognizer
import android.util.Log

/**
 * Minimal recognizer required by Android for a VoiceInteractionService to be accepted
 * as the default assistant. LocalWhisper does its own recording in AssistantActivity,
 * so platform SpeechRecognizer requests are rejected immediately with a busy error.
 */
class LocalWhisperRecognitionService : RecognitionService() {

    override fun onStartListening(recognizerIntent: Intent?, listener: Callback?) {
        if (listener == null) {
            Log.w(TAG, "onStartListening called without a callback")
            return
        }
        reportBusy(listener)
    }

    override fun onStopListening(listener: Callback?) {
        // Nothing to stop: listening is never started.
    }

    override fun onCancel(listener: Callback?) {
        // Nothing to cancel: listening is never started.
    }

    private fun reportBusy(listener: Callback) {
        try {
            listener.error(SpeechRecognizer.ERROR_RECOGNIZER_BUSY)
        } catch (exc: RemoteException) {
            Log.e(TAG, "Failed to report recognizer error to client", exc)
        }
    }

    companion object {
        private const val TAG = "LW-Recognition"
    }
}
