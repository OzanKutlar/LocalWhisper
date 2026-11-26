package me.Integer.localwhisper

import android.app.Activity
import com.google.android.material.dialog.MaterialAlertDialogBuilder

/** "Servers on your network" dialog shared by the settings screen and the assistant overlay. */
object DiscoveredServerPicker {

    fun show(activity: Activity, servers: List<DiscoveredServer>, onPick: (DiscoveredServer) -> Unit) {
        if (servers.isEmpty() || activity.isFinishing || activity.isDestroyed) return
        val labels = servers.map {
            activity.getString(R.string.discovery_item_format, it.name, it.host, it.port)
        }.toTypedArray()
        MaterialAlertDialogBuilder(activity)
            .setTitle(R.string.discovery_pick_title)
            .setItems(labels) { _, which ->
                servers.getOrNull(which)?.let(onPick)
            }
            .setNegativeButton(R.string.dialog_cancel, null)
            .show()
    }
}
