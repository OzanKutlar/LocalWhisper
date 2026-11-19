package me.Integer.TermuxAdditionApp

import android.accessibilityservice.AccessibilityService
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo

class KillAccessibilityService : AccessibilityService() {

    companion object {
        var isRunning = false
        @Volatile
        var targetPackageToKill: String? = null
        @Volatile
        var killDeadlineMs: Long = 0L

        fun arm(packageName: String, timeoutMs: Long = 10000L) {
            targetPackageToKill = packageName
            killDeadlineMs = System.currentTimeMillis() + timeoutMs
        }

        fun disarm() {
            targetPackageToKill = null
            killDeadlineMs = 0L
        }
    }

    override fun onAccessibilityEvent(event: AccessibilityEvent) {
        val target = targetPackageToKill ?: return
        if (System.currentTimeMillis() > killDeadlineMs) {
            disarm()
            return
        }

        // Only act on com.android.settings window changes
        val pkgName = event.packageName?.toString()
        if (pkgName != "com.android.settings") return

        if (event.eventType == AccessibilityEvent.TYPE_WINDOW_STATE_CHANGED) {
            val rootNode = rootInActiveWindow ?: return

            // 1. Look for "Force stop" button in Settings
            val forceStopNodes = rootNode.findAccessibilityNodeInfosByText("Force stop")
            for (node in forceStopNodes) {
                if (node.isClickable && node.isEnabled) {
                    node.performAction(AccessibilityNodeInfo.ACTION_CLICK)
                }
            }

            // 2. Look for "OK" in the confirmation dialog
            val okNodes = rootNode.findAccessibilityNodeInfosByText("OK")
            for (node in okNodes) {
                if (node.isClickable) {
                    node.performAction(AccessibilityNodeInfo.ACTION_CLICK)
                    // Once OK is clicked for this target, immediately disarm
                    disarm()
                }
            }
        }
    }

    override fun onInterrupt() {}

    override fun onServiceConnected() {
        super.onServiceConnected()
        isRunning = true
    }

    override fun onDestroy() {
        super.onDestroy()
        isRunning = false
        disarm()
    }
}
