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
