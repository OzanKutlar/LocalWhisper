# LocalWhisper

A local voice-to-keystroke system made of three components:

1. **Android Client (`app/`, package `me.Integer.localwhisper`)** — Triggered via Android's Digital Assistant button, records 16 kHz WAV audio with a live waveform overlay, displays the transcription for editing (with quick-insert AutoHotkey key chips like `{Enter}`), and sends the final text to the Server PC.
2. **Server PC (`server/`)** — Python FastAPI server that transcribes audio and dispatches confirmed keystrokes to connected PC clients over WebSocket. Transcription uses your llama-server model when it accepts audio input, otherwise **`Qwen/Qwen3-ASR-1.7B`** on CUDA (`cuda:0`, `bfloat16`, designed for RTX 5060 Ti).
3. **PC Client (`pc_client/`)** — Windows client (can run on the same machine as the server or on a separate PC) that executes incoming text via **AutoHotkey v1.1** interpreted `Send,` syntax.

---

## 1. Server PC Setup (CUDA / RTX 5060 Ti)

Ensure your Python environment already has PyTorch with CUDA installed, then install the server dependencies:

```bash
pip install -r server/requirements.txt
```

Start the server:

```bat
server\run_server.bat
```

Or with custom options:

```bash
python server/main.py --host 0.0.0.0 --port 8765 --model Qwen/Qwen3-ASR-1.7B --device cuda:0
```

> Make sure Windows Firewall allows inbound **TCP 8765** and **UDP 8766** on your private LAN. UDP 8766 is only used so the Android app can find the server automatically; change it with `--discovery-port <n>` or turn it off with `--no-discovery`.

### ASR backend selection

At startup the server asks your llama-server (`--llm-base-url`) for its input modalities via `GET /props`. If the loaded model accepts **audio** (llama-server started with an audio-capable `--mmproj`), transcription is sent to llama-server and Qwen3-ASR is never loaded. Otherwise Qwen3-ASR is loaded on CUDA as before.

- `--asr-backend auto` (default): probe llama-server and pick automatically.
- `--asr-backend local`: always load Qwen3-ASR, without probing.
- `--asr-backend llm`: always use llama-server; startup fails if the model has no audio input.

The environment variable `LOCALWHISPER_ASR_BACKEND` sets the same default.

Start llama-server **before** LocalWhisper. The probe takes at most a few seconds and falls back to Qwen3-ASR if llama-server is not reachable. Check what it reports with `curl http://127.0.0.1:34501/props` and look for `"modalities": {"audio": true}`. The app's **Test connection** row shows which ASR is active.

---

## 2. PC Client Setup (Windows + AutoHotkey v1.1)

Install AutoHotkey v1.1 (e.g. v1.1.37.x) and the Python WebSocket client dependency:

```bash
pip install -r pc_client/requirements.txt
```

If running on the **same PC** as the server:

```bat
pc_client\run_client.bat
```

If running on a **separate PC** on your LAN:

```bash
python pc_client/client.py --server ws://<SERVER_LAN_IP>:8765 --name GamingPC
```

Optional flags:
- `--ahk-path "C:\Path\To\AutoHotkeyU64.exe"` — Override AutoHotkey v1.1 path.
- `--send-mode Event` — Use `SendMode, Event` instead of `Input` if a specific app or game ignores `SendInput`.
- `--typing instant` — Send everything at once instead of typing at a human-like pace (default `human`; long messages speed up automatically).

---

## 3. Android Client Setup

Build and install the APK on your connected Android device:

```bash
python push.py
```

1. Open **LocalWhisper** on your phone.
2. Tap **Find server on network** to pick your Server PC automatically (this also runs by itself on first launch), or enter its LAN IP (port `8765`) under **Server address**, then tap **Test connection**.
3. Grant Microphone permission.
4. Tap **Set as Default Digital Assistant** and select **LocalWhisper**.
5. Press your phone's Assistant button (or tap **Launch Voice Overlay**), speak, review/edit the transcript (adding `{Enter}`, `{Tab}`, `{^a}`, etc. if desired), and tap **Send to PC**.
6. Prefer typing? Tap the keyboard button in the overlay header to switch to **keyboard mode** and type your message directly (no microphone needed). Turn on **Start in keyboard mode** in the app to make it the default.
7. **Typing on the phone itself:** in the app, tap **Type on this phone** and enable *LocalWhisper typing* under Accessibility. Then tap the *Target:* line in the overlay and pick **This phone**. The text is inserted at the cursor of the field you were using (`{…}` tokens are typed literally, not as keys). If typing is not possible, the text is copied to the clipboard instead. On Android 13+, if the toggle is greyed out, open **App info → ⋮ → Allow restricted settings** first.

> Only `{…}` tokens are treated as AutoHotkey keys; everything else (including `! + ^ #`) is typed literally. Inside braces, AutoHotkey syntax applies: `{Enter}`, `{Tab 3}`, `{Ctrl down}`, and modifier combos like `{^a}` (Ctrl+A), `{^+Tab}` (Ctrl+Shift+Tab) or `{!F4}` (Alt+F4).

---

## 4. PC Jarvis Popup

The PC client also runs a popup that matches the phone overlay. Install its packages into the same Python that runs the client:

```bash
python -m pip install -r pc_client/requirements.txt
```

- Press **`Ctrl+Alt+Space`** (change with `--hotkey`) to open the popup under your mouse. It starts listening at once and stops after about 1.2 s of silence.
- Start typing while it listens to switch to the keyboard. `Enter` sends, `Shift+Enter` adds a new line, `Esc` closes.
- The sparkle button switches between **Agent** and **dictation**. Dictating to *This PC* returns focus to the window you were in and types there.
- In Agent mode, paste (`Ctrl+V`) or drag an image onto the popup to attach it to your prompt.
- While the agent works you can shrink the popup to a corner pill (Pause, Interject, Open, Stop). The popup steps aside and is left out of screenshots whenever the agent looks at or acts on this PC.
- Flags: `--no-overlay` (headless), `--start-typing`, `--dictation-default`, `--language <hint>`.

## 5. Phone Link, Notifications & File Handoff

The phone keeps a WebSocket link to the server (`/ws/phone`) while the LocalWhisper app, its typing accessibility service or its notification listener is running. It holds no wake locks and sends no keep-alive pings, and it reconnects when the screen turns on or the network changes.

In the app, under **Phone link & permissions**:
- **Do Not Disturb access**: lets the agent toggle Do Not Disturb and silent mode.
- **Contacts, photos & notifications**: contact lookup for text drafts, sending your latest screenshot, and "file received" alerts.
- **Notification access**, **Allowed notification apps** and **Mirror notifications to PC**: the agent can read and reply to notifications from allowed apps, and mirrored notifications appear as corner cards on the PC with **Reply** and **Dismiss on phone**. Notification text is kept in memory only.

File and text handoff:
- **Send to PC** appears in every app's Share menu. Files go to `Downloads\\LocalWhisper` on the PC, links open in the PC browser, and text goes to the PC clipboard.
- Files sent to the phone are saved in `Downloads/LocalWhisper`.
- Controlling the phone screen from the PC popup only works while the phone is unlocked. A **Controlled from PC · Stop** pill shows on the phone the whole time.

## 6. Scenes

A scene is a named list of PC and phone steps that runs with one command. Scenes are stored in `server/scenes.json`; **Start Work**, **Meeting Mode** and **End of Day** are created on first run and can be edited.

- **Run:** tap a scene chip on the phone or in the PC popup, or ask: *"Run Meeting Mode"*. If any step would normally ask for approval (for example locking the PC), you are asked once for the whole scene.
- **Create:** *"Save a scene called Focus that mutes my mic and turns on Do Not Disturb on my phone"*.
- **Save a layout:** *"Save my current window layout as Start Work"*. The scene's other steps are kept.
- **Allowed steps:** AHK macros, app and window actions, media, URLs, PC audio, display and power, window layout, clipboard writes, phone intents, phone controls, and waits of up to 5 s. A scene can have up to 20 steps.
- **API:** `GET /api/scenes`, and `POST /api/scenes/{name}/run` with `{"approved": true}` after confirming.

## 7. Agent Tools That Ask First

| Tool | Asks for approval |
|---|---|
| Clicking or typing on the PC or phone screen | Always |
| `control_pc_power` | Sleep, hibernate, shutdown, restart (not `cancel_shutdown`) |
| `manage_pc_app` | Closing a window, locking the PC, launching an app that is not on the safe list |
| `read_pc_image`, `send_pc_file_to_phone`, `send_phone_file_to_pc` | Always |
| `reply_to_phone_notification` | Always |
| `save_scene`, `delete_scene` | Always |
| `run_scene` | Once, only when a step would normally ask |
| Typing, opening URLs or launching apps after screen, clipboard, notification or photo text was read in the same turn | Always |
