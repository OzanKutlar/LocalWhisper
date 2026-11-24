package me.Integer.localwhisper

import android.content.Context
import android.util.Log
import java.io.File
import java.io.IOException

/**
 * Keeps the most recent recording that has not been transcribed yet, so a failed or
 * interrupted transcription can be retried. Stored in noBackupFilesDir so it survives
 * cache clearing and is never uploaded to cloud backup.
 */
class RetainedRecording(context: Context) {

    private val directory = File(context.applicationContext.noBackupFilesDir, DIRECTORY_NAME)
    private val file = File(directory, FILE_NAME)

    /**
     * Moves [source] into the unsent slot, replacing an older unsent recording.
     * Returns the kept file, or null when [source] holds no audio (it is then deleted)
     * or could not be moved (it is then left where it was).
     */
    fun retain(source: File?): File? {
        if (source == null || !source.isFile) return null
        if (source.length() <= WAV_HEADER_BYTES) {
            deleteFile(source)
            return null
        }
        if (source.absolutePath == file.absolutePath) return file
        return try {
            moveIntoSlot(source)
            file
        } catch (exc: IOException) {
            Log.w(TAG, "Could not keep the recording for a retry", exc)
            null
        } catch (exc: SecurityException) {
            Log.w(TAG, "Could not keep the recording for a retry", exc)
            null
        }
    }

    /** The unsent recording, or null when there is none with audio in it. */
    fun current(): File? = file.takeIf { it.isFile && it.length() > WAV_HEADER_BYTES }

    fun durationMs(): Long = current()?.let { wavDurationMs(it.length()) } ?: 0L

    fun discard() {
        deleteFile(file)
    }

    private fun moveIntoSlot(source: File) {
        if (!directory.isDirectory && !directory.mkdirs()) {
            throw IOException("Could not create ${directory.absolutePath}")
        }
        if (file.exists() && !file.delete()) {
            throw IOException("Could not replace the previous unsent recording")
        }
        if (source.renameTo(file)) return
        try {
            source.copyTo(file, overwrite = true)
        } catch (exc: IOException) {
            deleteFile(file)
            throw exc
        }
        deleteFile(source)
    }

    private fun deleteFile(target: File) {
        if (target.exists() && !target.delete()) {
            Log.w(TAG, "Could not delete ${target.absolutePath}")
        }
    }

    companion object {
        private const val TAG = "LW-RetainedRec"
        private const val DIRECTORY_NAME = "recordings"
        private const val FILE_NAME = "unsent.wav"
        const val WAV_HEADER_BYTES = 44L
        private const val BYTES_PER_SECOND = 32_000L // 16 kHz mono 16-bit

        /** Audio length of a 16 kHz mono 16-bit WAV file of [sizeBytes] bytes. */
        fun wavDurationMs(sizeBytes: Long): Long {
            val audioBytes = sizeBytes - WAV_HEADER_BYTES
            if (audioBytes <= 0L) return 0L
            return audioBytes * 1000L / BYTES_PER_SECOND
        }
    }
}
