package me.Integer.TermuxAdditionApp

import android.Manifest
import android.app.AppOpsManager
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Process
import android.provider.Settings
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat
import me.Integer.TermuxAdditionApp.databinding.ActivityMainBinding

class MainActivity : AppCompatActivity() {

    private lateinit var binding: ActivityMainBinding

    private val runtimePermissions = arrayOf(
        Manifest.permission.KILL_BACKGROUND_PROCESSES,
        Manifest.permission.INTERNET,
        Manifest.permission.WAKE_LOCK
    )

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)

        binding.requestButton.setOnClickListener {
            handlePermissionRequest()
        }

        updateStatus()
    }

    private fun handlePermissionRequest() {
        when {
            !allRuntimePermissionsGranted() -> {
                ActivityCompat.requestPermissions(this, runtimePermissions, PERMISSION_REQUEST_CODE)
            }
            !hasUsageStatsPermission() -> {
                Toast.makeText(this, "Please enable Usage Access", Toast.LENGTH_LONG).show()
                startActivity(Intent(Settings.ACTION_USAGE_ACCESS_SETTINGS))
            }
            !Settings.canDrawOverlays(this) -> {
                Toast.makeText(this, "Please enable 'Display over other apps'", Toast.LENGTH_LONG).show()
                val intent = Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION, Uri.parse("package:$packageName"))
                startActivity(intent)
            }
            !isAccessibilityServiceEnabled() -> {
                Toast.makeText(this, "Please enable the Accessibility Service", Toast.LENGTH_LONG).show()
                startActivity(Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS))
            }
            else -> {
                updateStatus()
                Toast.makeText(this, "All permissions already granted", Toast.LENGTH_SHORT).show()
            }
        }
    }

    private fun allRuntimePermissionsGranted() = runtimePermissions.all {
        ContextCompat.checkSelfPermission(baseContext, it) == PackageManager.PERMISSION_GRANTED
    }

    private fun hasUsageStatsPermission(): Boolean {
        val appOps = getSystemService(Context.APP_OPS_SERVICE) as AppOpsManager
        val mode = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            appOps.unsafeCheckOpNoThrow(AppOpsManager.OPSTR_GET_USAGE_STATS, Process.myUid(), packageName)
        } else {
            @Suppress("DEPRECATION")
            appOps.checkOpNoThrow(AppOpsManager.OPSTR_GET_USAGE_STATS, Process.myUid(), packageName)
        }
        return mode == AppOpsManager.MODE_ALLOWED
    }

    private fun isAccessibilityServiceEnabled(): Boolean {
        return KillAccessibilityService.isRunning
    }

    private fun updateStatus() {
        val runtimeGranted = allRuntimePermissionsGranted()
        val usageGranted = hasUsageStatsPermission()
        val overlayGranted = Settings.canDrawOverlays(this)
        val accessibilityGranted = isAccessibilityServiceEnabled()

        if (runtimeGranted && usageGranted && overlayGranted && accessibilityGranted) {
            binding.statusText.text = "All permissions granted! App can now force stop other apps."
        } else {
            val status = StringBuilder("Status:\n")
            status.append("Runtime Perms: ${if (runtimeGranted) "OK" else "Missing"}\n")
            status.append("Usage Access: ${if (usageGranted) "OK" else "Missing"}\n")
            status.append("Overlay: ${if (overlayGranted) "OK" else "Missing"}\n")
            status.append("Accessibility: ${if (accessibilityGranted) "OK" else "Missing"}")
            binding.statusText.text = status.toString()
        }
    }

    override fun onResume() {
        super.onResume()
        updateStatus()
    }

    override fun onRequestPermissionsResult(
        requestCode: Int,
        permissions: Array<out String>,
        grantResults: IntArray
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode == PERMISSION_REQUEST_CODE) {
            updateStatus()
        }
    }

    companion object {
        private const val PERMISSION_REQUEST_CODE = 123
    }
}
