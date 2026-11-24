package me.Integer.localwhisper

import android.Manifest
import android.annotation.SuppressLint
import android.content.Context
import android.content.pm.PackageManager
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import androidx.core.content.ContextCompat
import java.io.File
import java.io.FileOutputStream
import java.io.RandomAccessFile
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.util.concurrent.atomic.AtomicBoolean
import kotlin.math.min
import kotlin.math.sqrt

class WavAudioRecorder(
    private val context: Context,
    private val onAmplitude: (Float) -> Unit,
    private val onMaxDurationReached: () -> Unit
) {

    private val isRecording = AtomicBoolean(false)
    private var audioRecord: AudioRecord? = null
    private var recordingThread: Thread? = null
    private var outputFile: File? = null

    @SuppressLint("MissingPermission")
    fun start(targetFile: File): Result<Unit> {
        if (ContextCompat.checkSelfPermission(context, Manifest.permission.RECORD_AUDIO) !=
            PackageManager.PERMISSION_GRANTED
        ) {
            return Result.failure(SecurityException("RECORD_AUDIO permission not granted"))
        }
        if (isRecording.get()) {
            return Result.failure(IllegalStateException("Recorder is already active"))
        }

        val minBufBytes = AudioRecord.getMinBufferSize(
            SAMPLE_RATE_HZ,
            AudioFormat.CHANNEL_IN_MONO,
            AudioFormat.ENCODING_PCM_16BIT
        )
        if (minBufBytes <= 0) {
            return Result.failure(IllegalStateException("Invalid AudioRecord buffer size: $minBufBytes"))
        }

        val bufferBytes = maxOf(minBufBytes * 2, READ_CHUNK_BYTES)
        val recorder = AudioRecord(
            MediaRecorder.AudioSource.VOICE_RECOGNITION,
            SAMPLE_RATE_HZ,
            AudioFormat.CHANNEL_IN_MONO,
            AudioFormat.ENCODING_PCM_16BIT,
            bufferBytes
        )
        if (recorder.state != AudioRecord.STATE_INITIALIZED) {
            recorder.release()
            return Result.failure(IllegalStateException("AudioRecord failed to initialize"))
        }

        outputFile = targetFile
        audioRecord = recorder
        isRecording.set(true)
        recorder.startRecording()

        recordingThread = Thread({ captureLoop(recorder, targetFile) }, "LocalWhisper-AudioCapture")
        recordingThread?.start()
        return Result.success(Unit)
    }

    /**
     * Stops capture and waits until the WAV header has been written. Also safe after the
     * max-duration limit already ended capture on the audio thread, and when called twice.
     */
    fun stop(): File? {
        isRecording.set(false)
        finishCapture()
        return outputFile
    }

    private fun finishCapture() {
        val recorder = audioRecord ?: return
        try {
            recorder.stop()
        } catch (_: IllegalStateException) {
            // Recorder may already have stopped
        }
        val thread = recordingThread
        if (thread != null && thread !== Thread.currentThread()) {
            try {
                thread.join(THREAD_JOIN_TIMEOUT_MS)
            } catch (_: InterruptedException) {
                Thread.currentThread().interrupt()
            }
        }
        recorder.release()
        audioRecord = null
        recordingThread = null
    }

    fun cancel() {
        val file = stop()
        if (file != null && file.exists()) {
            file.delete()
        }
        outputFile = null
    }

    private fun captureLoop(recorder: AudioRecord, targetFile: File) {
        val buffer = ByteArray(READ_CHUNK_BYTES)
        var totalDataBytes = 0L
        val startMs = System.currentTimeMillis()

        try {
            FileOutputStream(targetFile).use { fos ->
                fos.write(ByteArray(WAV_HEADER_SIZE))
                for (iteration in 0 until MAX_READ_ITERATIONS) {
                    if (!isRecording.get()) {
                        break
                    }
                    val elapsedMs = System.currentTimeMillis() - startMs
                    if (elapsedMs >= MAX_RECORDING_MS) {
                        isRecording.set(false)
                        onMaxDurationReached()
                        break
                    }

                    val readCount = recorder.read(buffer, 0, buffer.size)
                    if (readCount > 0) {
                        fos.write(buffer, 0, readCount)
                        totalDataBytes += readCount
                        onAmplitude(computeNormalizedRms(buffer, readCount))
                    }
                }
                fos.flush()
            }
            writeWavHeader(targetFile, totalDataBytes)
        } catch (_: Exception) {
            if (targetFile.exists()) {
                targetFile.delete()
            }
        }
    }

    private fun computeNormalizedRms(pcmBytes: ByteArray, length: Int): Float {
        val sampleCount = length / 2
        if (sampleCount <= 0) return 0f

        var sumSquares = 0.0
        var idx = 0
        while (idx + 1 < length) {
            val low = pcmBytes[idx].toInt() and 0xFF
            val high = pcmBytes[idx + 1].toInt()
            val sample = (high shl 8) or low
            sumSquares += (sample * sample).toDouble()
            idx += 2
        }
        val rms = sqrt(sumSquares / sampleCount) / 32768.0
        // Raw linear loudness (0..1); WaveformView applies its own decibel curve
        return min(1.0, rms).toFloat()
    }

    private fun writeWavHeader(file: File, totalAudioBytes: Long) {
        val totalDataLen = totalAudioBytes + 36L
        val byteRate = SAMPLE_RATE_HZ * CHANNELS * BITS_PER_SAMPLE / 8
        val header = ByteBuffer.allocate(WAV_HEADER_SIZE).order(ByteOrder.LITTLE_ENDIAN).apply {
            put("RIFF".toByteArray(Charsets.US_ASCII))
            putInt(totalDataLen.toInt())
            put("WAVE".toByteArray(Charsets.US_ASCII))
            put("fmt ".toByteArray(Charsets.US_ASCII))
            putInt(16) // PCM chunk size
            putShort(1) // AudioFormat = PCM
            putShort(CHANNELS.toShort())
            putInt(SAMPLE_RATE_HZ)
            putInt(byteRate)
            putShort((CHANNELS * BITS_PER_SAMPLE / 8).toShort())
            putShort(BITS_PER_SAMPLE.toShort())
            put("data".toByteArray(Charsets.US_ASCII))
            putInt(totalAudioBytes.toInt())
        }.array()

        RandomAccessFile(file, "rw").use { raf ->
            raf.seek(0)
            raf.write(header)
        }
    }

    companion object {
        private const val SAMPLE_RATE_HZ = 16000
        private const val CHANNELS = 1
        private const val BITS_PER_SAMPLE = 16
        private const val WAV_HEADER_SIZE = 44
        private const val READ_CHUNK_BYTES = 1600 // 50 ms chunks at 16 kHz mono 16-bit
        const val MAX_RECORDING_MS = 120_000L
        private const val MAX_READ_ITERATIONS = 10_000
        private const val THREAD_JOIN_TIMEOUT_MS = 2_000L
    }
}
