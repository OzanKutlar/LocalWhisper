# LocalWhisper

A local voice-to-keystroke system made of three components:

1. **Android Client (`app/`, package `me.Integer.localwhisper`)** — Triggered via Android's Digital Assistant button, records 16 kHz WAV audio with a live waveform overlay, displays the transcription for editing (with quick-insert AutoHotkey key chips like `{Enter}`), and sends the final text to the Server PC.
2. **Server PC (`server/`)** — Python FastAPI server running **`Qwen/Qwen3-ASR-1.7B`** on CUDA (`cuda:0`, `bfloat16`, designed for RTX 5060 Ti) that transcribes audio and dispatches confirmed keystrokes to connected PC clients over WebSocket.
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

> Make sure Windows Firewall allows inbound TCP on port `8765` on your private LAN.

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

---

## 3. Android Client Setup

Build and install the APK on your connected Android device:

```bash
python push.py
```

1. Open **LocalWhisper** on your phone.
2. Enter your Server PC's LAN IP (port `8765`) and tap **Save & Test Connection**.
3. Grant Microphone permission.
4. Tap **Set as Default Digital Assistant** and select **LocalWhisper**.
5. Press your phone's Assistant button (or tap **Launch Voice Overlay**), speak, review/edit the transcript (adding `{Enter}`, `{Tab}`, `^a`, etc. if desired), and tap **Send to PC**.
