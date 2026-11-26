package me.Integer.localwhisper

import android.Manifest
import android.app.NotificationManager
import android.app.role.RoleManager
import android.content.ComponentName
import android.content.DialogInterface
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.repeatOnLifecycle
import android.provider.Settings
import android.util.Log
import android.widget.TextView
import android.widget.Toast
import androidx.annotation.ColorRes
import androidx.appcompat.app.AppCompatActivity
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat
import androidx.core.view.ViewCompat
import androidx.core.view.WindowCompat
import androidx.core.view.WindowInsetsCompat
import androidx.lifecycle.lifecycleScope
import com.google.android.material.dialog.MaterialAlertDialogBuilder
import com.google.android.material.snackbar.Snackbar
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import me.Integer.localwhisper.databinding.ActivityMainBinding
import me.Integer.localwhisper.databinding.DialogLanguageBinding
import me.Integer.localwhisper.databinding.DialogServerAddressBinding

class MainActivity : AppCompatActivity() {

    private lateinit var binding: ActivityMainBinding
    private lateinit var prefs: ServerPreferences
    private val api = LocalWhisperApi()
    private var isTestingConnection = false
    private var isUpdatingServer = false
    private var isDiscovering = false

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        WindowCompat.setDecorFitsSystemWindows(window, false)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)
        prefs = ServerPreferences(this)

        applyWindowInsets()
        setupRows()
        refreshServerRows()
        resetConnectionStatus()
        PhoneLink.ensureStarted(applicationContext)
        observePhoneLinkState()
        maybeAutoDiscover()
    }

    override fun onResume() {
        super.onResume()
        updatePermissionRow()
        updateDefaultAssistantRow()
        updateKeyboardModeRow()
        updateAgentModeRow()
        updateAgentResumeTimeoutRow()
        updateTypeOnPhoneRow()
        updateDndAccessRow()
        updatePhonePermissionsRow()
    }

    // ------------------------------------------------------------ Layout

    private fun applyWindowInsets() {
        val horizontal = dpToPx(12)
        val baseTop = dpToPx(24)
        val baseBottom = dpToPx(24)
        ViewCompat.setOnApplyWindowInsetsListener(binding.rootScroll) { _, insets ->
            val bars = insets.getInsets(WindowInsetsCompat.Type.systemBars())
            binding.contentContainer.setPadding(
                horizontal + bars.left,
                baseTop + bars.top,
                horizontal + bars.right,
                baseBottom + bars.bottom
            )
            insets
        }
    }

    private fun setupRows() {
        binding.serverAddressRow.setOnClickListener { showServerAddressDialog() }
        binding.findServerRow.setOnClickListener { discoverServers(autoApplySingle = false) }
        binding.languageRow.setOnClickListener { showLanguageDialog() }
        binding.testConnectionRow.setOnClickListener { testConnection() }
        binding.updateServerRow.setOnClickListener { confirmAndUpdateServer() }
        binding.micPermissionRow.setOnClickListener { handleMicPermissionRequest() }
        binding.defaultAssistantRow.setOnClickListener { openAssistantSettings() }
        binding.launchOverlayRow.setOnClickListener { launchVoiceOverlay() }
        binding.keyboardModeRow.setOnClickListener { toggleStartInKeyboardMode() }
        binding.agentModeRow.setOnClickListener { toggleStartInAgentMode() }
        binding.agentResumeTimeoutRow.setOnClickListener { showAgentResumeTimeoutDialog() }
        binding.typeOnPhoneRow.setOnClickListener { openAccessibilitySettings() }
        binding.phoneLinkStatusRow.setOnClickListener { PhoneLink.reconnectNow(this) }
        binding.dndAccessRow.setOnClickListener { openDndAccessSettings() }
        binding.phonePermissionsRow.setOnClickListener { requestPhoneControlPermissions() }
    }

    private fun refreshServerRows() {
        binding.serverAddressValue.text =
            getString(R.string.server_address_format, prefs.serverHost, prefs.serverPort)
        val language = prefs.language
        binding.languageValue.text =
            if (language.isEmpty()) getString(R.string.row_language_auto) else language
    }

    // ------------------------------------------------------------ Discovery

    /** Searches once by itself while the address is still the factory default the user never changed. */
    private fun maybeAutoDiscover() {
        if (prefs.hasUserSetServer || prefs.serverHost != ServerPreferences.DEFAULT_HOST) return
        discoverServers(autoApplySingle = true)
    }

    private fun discoverServers(autoApplySingle: Boolean) {
        if (isDiscovering) return
        isDiscovering = true
        showDiscoveryStatus(getString(R.string.discovery_searching), R.color.lw_accent)
        lifecycleScope.launch {
            val servers = try {
                ServerDiscovery.discover(this@MainActivity, prefs.serverPort)
            } catch (exc: CancellationException) {
                throw exc
            } catch (exc: Exception) {
                Log.w(TAG, "Server discovery failed", exc)
                emptyList()
            } finally {
                isDiscovering = false
            }
            onDiscoveryFinished(servers, autoApplySingle)
        }
    }

    private fun onDiscoveryFinished(servers: List<DiscoveredServer>, autoApplySingle: Boolean) {
        if (servers.isEmpty()) {
            showDiscoveryStatus(getString(R.string.discovery_none_found), R.color.lw_error)
            return
        }
        showDiscoveryStatus(getString(R.string.discovery_found_format, servers.size), R.color.lw_success)
        if (autoApplySingle && servers.size == 1) {
            applyDiscoveredServer(servers[0])
        } else {
            showDiscoveredServersDialog(servers)
        }
    }

    private fun showDiscoveredServersDialog(servers: List<DiscoveredServer>) {
        DiscoveredServerPicker.show(this, servers) { applyDiscoveredServer(it) }
    }

    private fun applyDiscoveredServer(server: DiscoveredServer) {
        prefs.applyDiscoveredServer(server)
        PhoneLink.reconnectNow(this)
        refreshServerRows()
        showDiscoveryStatus(
            getString(R.string.discovery_applied_format, server.name, server.host, server.port),
            R.color.lw_success
        )
        resetConnectionStatus()
        testConnection()
    }

    private fun offerDiscoveryAfterFailure() {
        if (isDiscovering || isUpdatingServer || isFinishing) return
        Snackbar.make(binding.root, R.string.discovery_offer_after_failure, Snackbar.LENGTH_LONG)
            .setAction(R.string.discovery_action_search) { discoverServers(autoApplySingle = false) }
            .show()
    }

    private fun showDiscoveryStatus(text: String, @ColorRes colorRes: Int) {
        setColoredText(binding.findServerSubtitle, text, colorRes)
    }

    // ------------------------------------------------------------ Dialogs

    private fun showServerAddressDialog() {
        val dialogBinding = DialogServerAddressBinding.inflate(layoutInflater)
        dialogBinding.dialogHostInput.setText(prefs.serverHost)
        dialogBinding.dialogPortInput.setText(prefs.serverPort.toString())

        val dialog = MaterialAlertDialogBuilder(this)
            .setTitle(R.string.row_server_address)
            .setView(dialogBinding.root)
            .setNegativeButton(R.string.dialog_cancel, null)
            .setPositiveButton(R.string.dialog_save, null)
            .create()
        // Override the positive button so invalid input keeps the dialog open.
        dialog.setOnShowListener {
            dialog.getButton(DialogInterface.BUTTON_POSITIVE).setOnClickListener {
                if (saveServerAddress(dialogBinding)) {
                    dialog.dismiss()
                }
            }
        }
        dialog.show()
    }

    private fun saveServerAddress(dialogBinding: DialogServerAddressBinding): Boolean {
        val host = ServerPreferences.sanitizeHost(
            dialogBinding.dialogHostInput.text?.toString().orEmpty()
        )
        val port = dialogBinding.dialogPortInput.text?.toString()?.trim()?.toIntOrNull()
        val hostValid = host.isNotEmpty()
        val portValid = port != null && port in 1..65535

        dialogBinding.dialogHostLayout.error =
            if (hostValid) null else getString(R.string.invalid_host)
        dialogBinding.dialogPortLayout.error =
            if (portValid) null else getString(R.string.invalid_port)
        if (!hostValid || port == null || !portValid) {
            return false
        }

        prefs.serverHost = host
        prefs.serverPort = port
        prefs.hasUserSetServer = true
        PhoneLink.reconnectNow(this)
        refreshServerRows()
        resetConnectionStatus()
        return true
    }

    private fun showLanguageDialog() {
        val dialogBinding = DialogLanguageBinding.inflate(layoutInflater)
        dialogBinding.dialogLanguageInput.setText(prefs.language)
        MaterialAlertDialogBuilder(this)
            .setTitle(R.string.row_language)
            .setView(dialogBinding.root)
            .setNegativeButton(R.string.dialog_cancel, null)
            .setPositiveButton(R.string.dialog_save) { _, _ ->
                prefs.language = dialogBinding.dialogLanguageInput.text?.toString().orEmpty()
                refreshServerRows()
            }
            .show()
    }

    // ------------------------------------------------------------ Connection

    private fun testConnection() {
        if (isTestingConnection) return
        val baseUrl = prefs.buildBaseUrl()
        if (baseUrl == null) {
            showTestResult(getString(R.string.invalid_server_config), R.color.lw_error)
            showConnectionStatus(getString(R.string.main_status_offline), R.color.lw_error)
            return
        }

        isTestingConnection = true
        showTestResult(getString(R.string.testing_connection), R.color.lw_accent)
        showConnectionStatus(getString(R.string.main_status_checking), R.color.lw_accent)

        lifecycleScope.launch {
            val result = api.checkHealth(baseUrl)
            isTestingConnection = false
            result.onSuccess { health ->
                showTestResult(formatHealthSummary(health), R.color.lw_text_secondary)
                showConnectionStatus(getString(R.string.main_status_connected), R.color.lw_success)
            }.onFailure { exc ->
                val reason = exc.message ?: baseUrl
                showTestResult(getString(R.string.connection_failed, reason), R.color.lw_error)
                showConnectionStatus(getString(R.string.main_status_offline), R.color.lw_error)
                offerDiscoveryAfterFailure()
            }
        }
    }

    private fun resetConnectionStatus() {
        showConnectionStatus(getString(R.string.main_status_untested), R.color.lw_text_secondary)
        showTestResult(getString(R.string.row_test_connection_hint), R.color.lw_text_secondary)
    }

    private fun showConnectionStatus(text: String, @ColorRes colorRes: Int) {
        setColoredText(binding.connectionStatusLine, text, colorRes)
    }

    private fun showTestResult(text: String, @ColorRes colorRes: Int) {
        setColoredText(binding.testConnectionSubtitle, text, colorRes)
    }

    private fun showUpdateStatus(text: String, @ColorRes colorRes: Int) {
        setColoredText(binding.updateServerSubtitle, text, colorRes)
    }

    private fun confirmAndUpdateServer() {
        if (isUpdatingServer) return
        val baseUrl = prefs.buildBaseUrl()
        if (baseUrl == null) {
            showUpdateStatus(getString(R.string.invalid_server_config), R.color.lw_error)
            return
        }
        MaterialAlertDialogBuilder(this)
            .setTitle(R.string.update_confirm_title)
            .setMessage(R.string.update_confirm_message)
            .setNegativeButton(R.string.dialog_cancel, null)
            .setPositiveButton(R.string.update_confirm_button) { _, _ ->
                runServerUpdateFlow(baseUrl)
            }
            .show()
    }

    private fun runServerUpdateFlow(baseUrl: String) {
        if (isUpdatingServer) return
        isUpdatingServer = true
        showUpdateStatus(getString(R.string.update_stage_closing), R.color.lw_accent)
        showConnectionStatus(getString(R.string.update_stage_closing), R.color.lw_accent)

        lifecycleScope.launch {
            val triggerResult = api.triggerServerUpdate(baseUrl)
            if (triggerResult.isFailure) {
                val reason = triggerResult.exceptionOrNull()?.message ?: baseUrl
                showUpdateStatus(getString(R.string.update_stage_failed, reason), R.color.lw_error)
                showConnectionStatus(getString(R.string.main_status_offline), R.color.lw_error)
                isUpdatingServer = false
                return@launch
            }

            val trigger = triggerResult.getOrThrow()
            val expectedUpdateId = trigger.updateId
            var sawServerClosedOrUpdating = false
            var latestGitSummary = ""

            for (attempt in 0 until UPDATE_MAX_POLL_ATTEMPTS) {
                delay(UPDATE_POLL_INTERVAL_MS)
                val statusResult = api.fetchUpdateStatus(baseUrl)
                statusResult.onSuccess { status ->
                    if (status.gitSummary.isNotBlank()) {
                        latestGitSummary = status.gitSummary
                    }
                    when (status.phase) {
                        "closing" -> {
                            showUpdateStatus(getString(R.string.update_stage_closing), R.color.lw_accent)
                            showConnectionStatus(getString(R.string.update_stage_closing), R.color.lw_accent)
                        }
                        "git_pull" -> {
                            sawServerClosedOrUpdating = true
                            showUpdateStatus(getString(R.string.update_stage_git_pull), R.color.lw_accent)
                            showConnectionStatus(getString(R.string.update_stage_git_pull), R.color.lw_accent)
                        }
                        "relaunching" -> {
                            sawServerClosedOrUpdating = true
                            val msg = if (latestGitSummary.isNotBlank()) {
                                getString(R.string.update_stage_relaunching_with_git, latestGitSummary)
                            } else {
                                getString(R.string.update_stage_relaunching)
                            }
                            showUpdateStatus(msg, R.color.lw_accent)
                            showConnectionStatus(getString(R.string.update_stage_relaunching), R.color.lw_accent)
                        }
                        "online" -> {
                            val isUpdatedInstance = expectedUpdateId.isEmpty() ||
                                status.updateId == expectedUpdateId ||
                                sawServerClosedOrUpdating
                            if (isUpdatedInstance && attempt >= 2) {
                                val oldHash = status.oldCommit.ifBlank { trigger.oldCommit.ifBlank { "HEAD" } }
                                val newHash = status.newCommit.ifBlank { oldHash }
                                val summary = status.gitSummary.ifBlank { "Up to date" }
                                val finalColor = if (status.ok) R.color.lw_success else R.color.lw_error
                                showUpdateStatus(
                                    getString(R.string.update_stage_complete, oldHash, newHash, summary),
                                    finalColor
                                )
                                showConnectionStatus(getString(R.string.main_status_connected), R.color.lw_success)
                                isUpdatingServer = false
                                testConnection()
                                return@launch
                            }
                        }
                    }
                }.onFailure {
                    // Connection refused means old server has closed and either git pull or CUDA model load is running
                    if (!sawServerClosedOrUpdating) {
                        sawServerClosedOrUpdating = true
                        showUpdateStatus(getString(R.string.update_stage_git_pull), R.color.lw_accent)
                        showConnectionStatus(getString(R.string.update_stage_git_pull), R.color.lw_accent)
                    } else {
                        val msg = if (latestGitSummary.isNotBlank()) {
                            getString(R.string.update_stage_relaunching_with_git, latestGitSummary)
                        } else {
                            getString(R.string.update_stage_relaunching)
                        }
                        showUpdateStatus(msg, R.color.lw_accent)
                        showConnectionStatus(getString(R.string.update_stage_relaunching), R.color.lw_accent)
                    }
                }
            }

            showUpdateStatus(
                getString(R.string.update_stage_failed, "Timed out waiting for server to relaunch"),
                R.color.lw_error
            )
            showConnectionStatus(getString(R.string.main_status_offline), R.color.lw_error)
            isUpdatingServer = false
        }
    }

    private fun setColoredText(view: TextView, text: String, @ColorRes colorRes: Int) {
        view.text = text
        view.setTextColor(ContextCompat.getColor(this, colorRes))
    }

    private fun formatHealthSummary(health: HealthResponse): String {
        val clientNames = if (health.clients.isEmpty()) {
            getString(R.string.health_no_clients)
        } else {
            health.clients.joinToString(", ") { it.name }
        }
        return getString(
            R.string.health_summary,
            health.gpuName,
            health.device,
            health.model,
            health.agentModel,
            clientNames
        )
    }

    // ------------------------------------------------------------ Assistant

    private fun updateDefaultAssistantRow() {
        val isActive = isDefaultAssistant()
        val text = getString(
            if (isActive) R.string.default_assistant_active else R.string.default_assistant_inactive
        )
        val color = if (isActive) R.color.lw_success else R.color.lw_accent
        setColoredText(binding.defaultAssistantSubtitle, text, color)
    }

    private fun isDefaultAssistant(): Boolean {
        val roleManager = getSystemService(RoleManager::class.java) ?: return false
        return try {
            roleManager.isRoleAvailable(RoleManager.ROLE_ASSISTANT) &&
                roleManager.isRoleHeld(RoleManager.ROLE_ASSISTANT)
        } catch (exc: Exception) {
            Log.w(TAG, "Could not query assistant role", exc)
            false
        }
    }

    private fun openAssistantSettings() {
        val intents = listOf(
            Intent(Settings.ACTION_VOICE_INPUT_SETTINGS),
            Intent(Settings.ACTION_MANAGE_DEFAULT_APPS_SETTINGS)
        )
        for (intent in intents) {
            try {
                startActivity(intent)
                return
            } catch (exc: Exception) {
                Log.d(TAG, "Settings screen ${intent.action} unavailable, trying next", exc)
            }
        }
        Toast.makeText(this, R.string.default_assistant_fallback_toast, Toast.LENGTH_LONG).show()
    }

    private fun launchVoiceOverlay() {
        // Keyboard mode does not record audio, so the mic permission is only needed for voice.
        if (!hasRecordAudioPermission() && !prefs.startInKeyboardMode) {
            handleMicPermissionRequest()
            return
        }
        val intent = Intent(this, AssistantActivity::class.java).apply {
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TASK)
        }
        startActivity(intent)
    }

    // ------------------------------------------------------------ Keyboard mode

    private fun toggleStartInKeyboardMode() {
        prefs.startInKeyboardMode = !prefs.startInKeyboardMode
        updateKeyboardModeRow()
    }

    private fun updateKeyboardModeRow() {
        binding.keyboardModeSwitch.isChecked = prefs.startInKeyboardMode
    }

    private fun toggleStartInAgentMode() {
        prefs.startInAgentMode = !prefs.startInAgentMode
        updateAgentModeRow()
    }

    private fun updateAgentModeRow() {
        binding.agentModeSwitch.isChecked = prefs.startInAgentMode
    }

    private fun updateAgentResumeTimeoutRow() {
        val seconds = prefs.agentResumeTimeoutSeconds
        binding.agentResumeTimeoutValue.text = if (seconds <= 0) {
            getString(R.string.agent_resume_timeout_instant)
        } else {
            getString(R.string.agent_resume_timeout_seconds_format, seconds)
        }
    }

    private fun showAgentResumeTimeoutDialog() {
        val options = ServerPreferences.RESUME_TIMEOUT_OPTIONS_SECONDS
        val labels = options.map { seconds ->
            if (seconds == 0) {
                getString(R.string.agent_resume_timeout_option_instant)
            } else {
                getString(R.string.agent_resume_timeout_option_seconds, seconds)
            }
        }.toTypedArray()
        val currentSeconds = prefs.agentResumeTimeoutSeconds
        val checkedIndex = options.indexOfFirst { it == currentSeconds }.coerceAtLeast(0)

        MaterialAlertDialogBuilder(this)
            .setTitle(R.string.row_agent_resume_timeout)
            .setSingleChoiceItems(labels, checkedIndex) { dialog, which ->
                val selected = options.getOrElse(which) { ServerPreferences.DEFAULT_AGENT_RESUME_TIMEOUT_S }
                prefs.agentResumeTimeoutSeconds = selected
                updateAgentResumeTimeoutRow()
                dialog.dismiss()
            }
            .setNegativeButton(R.string.dialog_cancel, null)
            .show()
    }

    // ------------------------------------------------------------ Phone typing

    private fun updateTypeOnPhoneRow() {
        val enabled = isLocalTypingEnabled()
        val text = getString(
            if (enabled) R.string.type_on_phone_active else R.string.type_on_phone_inactive
        )
        val color = if (enabled) R.color.lw_success else R.color.lw_accent
        setColoredText(binding.typeOnPhoneSubtitle, text, color)
    }

    /** Reads the system list of enabled accessibility services, so it is correct even before the service binds. */
    private fun isLocalTypingEnabled(): Boolean {
        val enabled = Settings.Secure.getString(
            contentResolver,
            Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES
        ) ?: return false
        val expected = ComponentName(this, LocalTypingService::class.java)
        return enabled.split(':').any { ComponentName.unflattenFromString(it) == expected }
    }

    private fun observePhoneLinkState() {
        lifecycleScope.launch {
            repeatOnLifecycle(Lifecycle.State.STARTED) {
                PhoneLink.state.collect { state ->
                    when (state) {
                        PhoneLinkState.CONNECTED -> setColoredText(
                            binding.phoneLinkStatusSubtitle,
                            getString(R.string.phone_link_connected),
                            R.color.lw_success
                        )
                        PhoneLinkState.CONNECTING -> setColoredText(
                            binding.phoneLinkStatusSubtitle,
                            getString(R.string.phone_link_connecting),
                            R.color.lw_accent
                        )
                        PhoneLinkState.DISCONNECTED -> setColoredText(
                            binding.phoneLinkStatusSubtitle,
                            getString(R.string.phone_link_disconnected),
                            R.color.lw_text_secondary
                        )
                    }
                }
            }
        }
    }

    private fun updateDndAccessRow() {
        val nm = getSystemService(NotificationManager::class.java)
        val granted = nm?.isNotificationPolicyAccessGranted == true
        val text = getString(if (granted) R.string.dnd_access_granted else R.string.dnd_access_missing)
        val color = if (granted) R.color.lw_success else R.color.lw_text_secondary
        setColoredText(binding.dndAccessSubtitle, text, color)
    }

    private fun openDndAccessSettings() {
        try {
            startActivity(Intent(Settings.ACTION_NOTIFICATION_POLICY_ACCESS_SETTINGS))
        } catch (exc: Exception) {
            Log.w(TAG, "DND settings unavailable", exc)
        }
    }

    private fun updatePhonePermissionsRow() {
        val contacts = ContextCompat.checkSelfPermission(this, Manifest.permission.READ_CONTACTS) ==
            PackageManager.PERMISSION_GRANTED
        val photos = FileHandoff.hasMediaImagesPermission(this)
        val text = if (contacts && photos) {
            getString(R.string.phone_permissions_granted)
        } else {
            getString(R.string.phone_permissions_hint)
        }
        val color = if (contacts && photos) R.color.lw_success else R.color.lw_text_secondary
        setColoredText(binding.phonePermissionsSubtitle, text, color)
    }

    private fun requestPhoneControlPermissions() {
        val perms = mutableListOf(Manifest.permission.READ_CONTACTS)
        if (Build.VERSION.SDK_INT >= 33) {
            perms.add(Manifest.permission.READ_MEDIA_IMAGES)
            perms.add(Manifest.permission.POST_NOTIFICATIONS)
        } else {
            perms.add(Manifest.permission.READ_EXTERNAL_STORAGE)
        }
        ActivityCompat.requestPermissions(this, perms.toTypedArray(), PHONE_PERMS_REQUEST_CODE)
    }

    private fun openAccessibilitySettings() {
        try {
            startActivity(Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS))
        } catch (exc: Exception) {
            Log.w(TAG, "Accessibility settings unavailable", exc)
            Toast.makeText(this, R.string.local_typing_settings_fallback, Toast.LENGTH_LONG).show()
        }
    }

    // ------------------------------------------------------------ Permission

    private fun handleMicPermissionRequest() {
        if (hasRecordAudioPermission()) {
            Toast.makeText(this, R.string.mic_permission_already_granted, Toast.LENGTH_SHORT).show()
            updatePermissionRow()
            return
        }
        ActivityCompat.requestPermissions(
            this,
            arrayOf(Manifest.permission.RECORD_AUDIO),
            RECORD_AUDIO_REQUEST_CODE
        )
    }

    private fun updatePermissionRow() {
        val granted = hasRecordAudioPermission()
        binding.micPermissionSwitch.isChecked = granted
        val text = getString(
            if (granted) R.string.mic_permission_granted else R.string.mic_permission_missing
        )
        val color = if (granted) R.color.lw_accent else R.color.lw_text_secondary
        setColoredText(binding.micPermissionSubtitle, text, color)
    }

    private fun hasRecordAudioPermission(): Boolean {
        return ContextCompat.checkSelfPermission(
            this,
            Manifest.permission.RECORD_AUDIO
        ) == PackageManager.PERMISSION_GRANTED
    }

    override fun onRequestPermissionsResult(
        requestCode: Int,
        permissions: Array<out String>,
        grantResults: IntArray
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode == PHONE_PERMS_REQUEST_CODE) {
            updatePhonePermissionsRow()
            return
        }
        if (requestCode != RECORD_AUDIO_REQUEST_CODE) {
            return
        }
        val granted = grantResults.isNotEmpty() &&
            grantResults[0] == PackageManager.PERMISSION_GRANTED
        if (!granted) {
            Toast.makeText(this, R.string.mic_permission_denied, Toast.LENGTH_SHORT).show()
        }
        updatePermissionRow()
    }

    private fun dpToPx(dp: Int): Int {
        return (dp * resources.displayMetrics.density).toInt()
    }

    companion object {
        private const val TAG = "LW-Main"
        private const val RECORD_AUDIO_REQUEST_CODE = 1001
        private const val PHONE_PERMS_REQUEST_CODE = 1002
        private const val UPDATE_POLL_INTERVAL_MS = 750L
        private const val UPDATE_MAX_POLL_ATTEMPTS = 120 // ~90s budget for git pull + CUDA model reload
    }
}
