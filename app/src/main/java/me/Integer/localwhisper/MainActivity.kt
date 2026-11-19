package me.Integer.localwhisper

import android.Manifest
import android.content.pm.PackageManager
import android.os.Bundle
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat
import me.Integer.localwhisper.databinding.ActivityMainBinding

class MainActivity : AppCompatActivity() {

    private lateinit var binding: ActivityMainBinding

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)

        setupViews()
        updateStatus()
    }

    override fun onResume() {
        super.onResume()
        updateStatus()
    }

    private fun setupViews() {
        binding.requestPermissionButton.setOnClickListener {
            handleMicPermissionRequest()
        }
    }

    private fun handleMicPermissionRequest() {
        if (hasRecordAudioPermission()) {
            Toast.makeText(
                this,
                getString(R.string.mic_permission_already_granted),
                Toast.LENGTH_SHORT
            ).show()
            updateStatus()
            return
        }

        ActivityCompat.requestPermissions(
            this,
            arrayOf(Manifest.permission.RECORD_AUDIO),
            RECORD_AUDIO_REQUEST_CODE
        )
    }

    private fun hasRecordAudioPermission(): Boolean {
        return ContextCompat.checkSelfPermission(
            this,
            Manifest.permission.RECORD_AUDIO
        ) == PackageManager.PERMISSION_GRANTED
    }

    private fun updateStatus() {
        val granted = hasRecordAudioPermission()
        binding.statusText.text = if (granted) {
            getString(R.string.mic_permission_granted)
        } else {
            getString(R.string.mic_permission_missing)
        }
        binding.requestPermissionButton.isEnabled = !granted
    }

    override fun onRequestPermissionsResult(
        requestCode: Int,
        permissions: Array<out String>,
        grantResults: IntArray
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode != RECORD_AUDIO_REQUEST_CODE) {
            return
        }

        val granted = grantResults.isNotEmpty() &&
            grantResults[0] == PackageManager.PERMISSION_GRANTED
        if (!granted) {
            Toast.makeText(
                this,
                getString(R.string.mic_permission_denied),
                Toast.LENGTH_SHORT
            ).show()
        }
        updateStatus()
    }

    companion object {
        private const val RECORD_AUDIO_REQUEST_CODE = 1001
    }
}
