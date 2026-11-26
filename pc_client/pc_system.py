"""Windows PC hardware, audio device, brightness, power and status tools for LocalWhisper."""

from __future__ import annotations

import ctypes
import logging
import os
import subprocess
import time
from typing import Any

LOGGER = logging.getLogger("localwhisper.pc_system")

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
NVIDIA_SMI_TIMEOUT_S = 3
SHUTDOWN_TIMEOUT_S = 5
DEFAULT_POWER_DELAY_S = 30
MAX_POWER_DELAY_S = 3600
TOP_PROCESS_COUNT = 5
MAX_AUDIO_DEVICES = 40

# Windows Core Audio constants
E_RENDER = 0
E_CAPTURE = 1
E_CONSOLE = 0
E_MULTIMEDIA = 1
E_COMMUNICATIONS = 2
DEVICE_STATE_ACTIVE = 0x00000001
CLSCTX_ALL = 23

# Windows display off message constants
HWND_BROADCAST = 0xFFFF
WM_SYSCOMMAND = 0x0112
SC_MONITORPOWER = 0xF170
MONITOR_POWER_OFF = 2

AUDIO_ACTIONS = frozenset(
    {
        "list_devices",
        "get_state",
        "set_output_device",
        "set_input_device",
        "set_volume",
        "mute_mic",
        "unmute_mic",
        "toggle_mic_mute",
    }
)
DISPLAY_ACTIONS = frozenset({"get_brightness", "set_brightness", "turn_off_display"})
POWER_ACTIONS = frozenset({"sleep", "hibernate", "shutdown", "restart", "cancel_shutdown"})


def _is_windows() -> bool:
    return os.name == "nt" and hasattr(ctypes, "windll")


def clamp_percentage(raw: Any, field_name: str = "value") -> int:
    if raw is None or isinstance(raw, bool):
        raise ValueError(f"'{field_name}' must be an integer from 0 to 100")
    try:
        value = int(round(float(raw)))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"'{field_name}' must be an integer from 0 to 100") from exc
    return max(0, min(value, 100))


def validate_power_action(action: Any, delay_seconds: Any = None) -> tuple[str, int]:
    clean = str(action or "").strip().lower()
    if clean not in POWER_ACTIONS:
        raise ValueError(f"Unsupported power action '{action}'; choose one of {sorted(POWER_ACTIONS)}")
    if delay_seconds is None or isinstance(delay_seconds, bool):
        delay = DEFAULT_POWER_DELAY_S
    else:
        try:
            delay = int(delay_seconds)
        except (TypeError, ValueError) as exc:
            raise ValueError("delay_seconds must be an integer") from exc
    return clean, max(0, min(delay, MAX_POWER_DELAY_S))


def match_audio_device(
    devices: list[dict[str, Any]],
    query: str,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Matches an audio device by exact name, substring or all query tokens."""
    clean = " ".join(str(query or "").lower().split())
    if not clean:
        return None, devices
    for dev in devices:
        if str(dev.get("name", "")).strip().lower() == clean:
            return dev, []
    substr = [dev for dev in devices if clean in str(dev.get("name", "")).lower()]
    if len(substr) == 1:
        return substr[0], []
    if len(substr) > 1:
        return None, substr
    tokens = clean.split()
    token_hits = [
        dev for dev in devices if all(tok in str(dev.get("name", "")).lower() for tok in tokens)
    ]
    if len(token_hits) == 1:
        return token_hits[0], []
    return None, token_hits or devices


def parse_nvidia_smi_csv(stdout: str) -> list[dict[str, Any]]:
    """Parses `nvidia-smi --query-gpu=name,temperature.gpu,utilization.gpu,memory.used,memory.total --format=csv,noheader,nounits`."""
    gpus: list[dict[str, Any]] = []
    for raw_line in (stdout or "").splitlines()[:8]:
        line = raw_line.strip()
        if not line:
            continue
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 5:
            continue
        gpus.append(
            {
                "name": parts[0],
                "temperature_c": _optional_float(parts[1]),
                "utilization_pct": _optional_float(parts[2]),
                "vram_used_mb": _optional_float(parts[3]),
                "vram_total_mb": _optional_float(parts[4]),
            }
        )
    return gpus


def _optional_float(raw: str) -> float | None:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- Audio (pycaw + IPolicyConfig)

def run_control_pc_audio(arguments: dict[str, Any]) -> dict[str, Any]:
    action = str(arguments.get("action", "") or "").strip().lower()
    if action not in AUDIO_ACTIONS:
        raise ValueError(f"Unsupported audio action '{action}'; choose one of {sorted(AUDIO_ACTIONS)}")
    if not _is_windows():
        raise RuntimeError("Audio control is only supported on Windows")

    try:
        import comtypes  # noqa: PLC0415
    except ImportError as exc:
        raise RuntimeError("Audio control requires 'pycaw' and 'comtypes'. Run: pip install -r pc_client/requirements.txt") from exc

    comtypes.CoInitialize()
    try:
        return _dispatch_audio_com(action, arguments)
    finally:
        try:
            comtypes.CoUninitialize()
        except Exception:
            pass


def _dispatch_audio_com(action: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if action == "list_devices":
        return {
            "ok": True,
            "outputs": _enumerate_audio_endpoints(E_RENDER),
            "inputs": _enumerate_audio_endpoints(E_CAPTURE),
        }
    if action == "get_state":
        return {"ok": True, **_read_audio_state()}
    if action == "set_output_device":
        return _switch_audio_device(E_RENDER, str(arguments.get("device", "") or ""))
    if action == "set_input_device":
        return _switch_audio_device(E_CAPTURE, str(arguments.get("device", "") or ""))
    if action == "set_volume":
        level = clamp_percentage(arguments.get("volume_pct"), "volume_pct")
        _set_endpoint_volume(E_RENDER, level)
        return {"ok": True, "action": "set_volume", "volume_pct": level}
    if action in ("mute_mic", "unmute_mic", "toggle_mic_mute"):
        muted = _set_mic_mute(action)
        return {"ok": True, "action": action, "mic_muted": muted}
    raise ValueError(f"Unsupported audio action: {action}")


def _get_pycaw_enumerator() -> tuple[Any, Any]:
    try:
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume  # noqa: PLC0415
    except ImportError as exc:
        raise RuntimeError("Audio control requires 'pycaw'. Run: pip install -r pc_client/requirements.txt") from exc
    return AudioUtilities, IAudioEndpointVolume


def _enumerate_audio_endpoints(data_flow: int) -> list[dict[str, Any]]:
    AudioUtilities, _vol = _get_pycaw_enumerator()
    enumerator = AudioUtilities.GetDeviceEnumerator()
    default_id = ""
    try:
        default_id = enumerator.GetDefaultAudioEndpoint(data_flow, E_CONSOLE).GetId()
    except Exception:
        default_id = ""

    collection = enumerator.EnumAudioEndpoints(data_flow, DEVICE_STATE_ACTIVE)
    count = min(int(collection.GetCount()), MAX_AUDIO_DEVICES)
    devices: list[dict[str, Any]] = []
    for idx in range(count):
        raw_dev = collection.Item(idx)
        dev_id = raw_dev.GetId()
        wrapped = AudioUtilities.CreateDevice(raw_dev)
        name = (getattr(wrapped, "FriendlyName", None) or dev_id).strip()
        devices.append({"id": dev_id, "name": name, "is_default": dev_id == default_id})
    return devices


def _read_audio_state() -> dict[str, Any]:
    outputs = _enumerate_audio_endpoints(E_RENDER)
    inputs = _enumerate_audio_endpoints(E_CAPTURE)
    default_out = next((d["name"] for d in outputs if d["is_default"]), "")
    default_in = next((d["name"] for d in inputs if d["is_default"]), "")
    vol_pct, out_muted = _get_endpoint_volume_state(E_RENDER)
    _mic_pct, mic_muted = _get_endpoint_volume_state(E_CAPTURE)
    return {
        "default_output": default_out,
        "default_input": default_in,
        "volume_pct": vol_pct,
        "output_muted": out_muted,
        "mic_muted": mic_muted,
    }


def _endpoint_volume_interface(data_flow: int) -> Any:
    AudioUtilities, IAudioEndpointVolume = _get_pycaw_enumerator()
    enumerator = AudioUtilities.GetDeviceEnumerator()
    endpoint = enumerator.GetDefaultAudioEndpoint(data_flow, E_CONSOLE)
    interface = endpoint.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
    return ctypes.cast(interface, ctypes.POINTER(IAudioEndpointVolume))


def _get_endpoint_volume_state(data_flow: int) -> tuple[int | None, bool | None]:
    try:
        vol = _endpoint_volume_interface(data_flow)
        pct = int(round(float(vol.GetMasterVolumeLevelScalar()) * 100.0))
        muted = bool(vol.GetMute())
        return pct, muted
    except Exception as exc:
        LOGGER.debug("Could not read endpoint volume (flow=%d): %s", data_flow, exc)
        return None, None


def _set_endpoint_volume(data_flow: int, level_pct: int) -> None:
    vol = _endpoint_volume_interface(data_flow)
    vol.SetMasterVolumeLevelScalar(level_pct / 100.0, None)
    if level_pct > 0:
        try:
            vol.SetMute(0, None)
        except Exception:
            pass


def _set_mic_mute(action: str) -> bool:
    vol = _endpoint_volume_interface(E_CAPTURE)
    if action == "toggle_mic_mute":
        target_mute = not bool(vol.GetMute())
    else:
        target_mute = action == "mute_mic"
    vol.SetMute(1 if target_mute else 0, None)
    return bool(vol.GetMute())


def _switch_audio_device(data_flow: int, query: str) -> dict[str, Any]:
    devices = _enumerate_audio_endpoints(data_flow)
    matched, candidates = match_audio_device(devices, query)
    if matched is None:
        return {
            "ok": False,
            "reason": "device_not_matched",
            "message": f"Could not uniquely match audio device '{query}'.",
            "candidates": [d["name"] for d in candidates],
        }
    _set_default_endpoint_via_policy_config(str(matched["id"]))
    kind = "output" if data_flow == E_RENDER else "input"
    return {"ok": True, "kind": kind, "device": matched["name"]}


def _set_default_endpoint_via_policy_config(device_id: str) -> None:
    """Calls Windows' undocumented IPolicyConfig::SetDefaultEndpoint for all three roles."""
    import comtypes  # noqa: PLC0415
    from comtypes import COMMETHOD, GUID, HRESULT  # noqa: PLC0415

    class IPolicyConfig(comtypes.IUnknown):
        _iid_ = GUID("{f8679f50-850a-41cf-9c72-430f290290c8}")
        _methods_ = [
            COMMETHOD([], HRESULT, "GetMixFormat"),
            COMMETHOD([], HRESULT, "GetDeviceFormat"),
            COMMETHOD([], HRESULT, "ResetDeviceFormat"),
            COMMETHOD([], HRESULT, "SetDeviceFormat"),
            COMMETHOD([], HRESULT, "GetProcessingPeriod"),
            COMMETHOD([], HRESULT, "SetProcessingPeriod"),
            COMMETHOD([], HRESULT, "GetShareMode"),
            COMMETHOD([], HRESULT, "SetShareMode"),
            COMMETHOD([], HRESULT, "GetPropertyValue"),
            COMMETHOD([], HRESULT, "SetPropertyValue"),
            COMMETHOD(
                [],
                HRESULT,
                "SetDefaultEndpoint",
                (["in"], ctypes.c_wchar_p, "wszDeviceId"),
                (["in"], ctypes.c_uint, "eRole"),
            ),
            COMMETHOD([], HRESULT, "SetEndpointVisibility"),
        ]

    clsid = GUID("{870af99c-171d-4f9e-af0d-e63df40c2bc9}")
    policy = comtypes.CoCreateInstance(clsid, interface=IPolicyConfig, clsctx=CLSCTX_ALL)
    for role in (E_CONSOLE, E_MULTIMEDIA, E_COMMUNICATIONS):
        policy.SetDefaultEndpoint(device_id, role)


# ---------------------------------------------------------------- Display (brightness & sleep)

def run_control_pc_display(arguments: dict[str, Any]) -> dict[str, Any]:
    action = str(arguments.get("action", "") or "").strip().lower()
    if action not in DISPLAY_ACTIONS:
        raise ValueError(f"Unsupported display action '{action}'; choose one of {sorted(DISPLAY_ACTIONS)}")
    if not _is_windows():
        raise RuntimeError("Display control is only supported on Windows")

    if action == "turn_off_display":
        ctypes.windll.user32.SendMessageW(HWND_BROADCAST, WM_SYSCOMMAND, SC_MONITORPOWER, MONITOR_POWER_OFF)
        return {"ok": True, "action": "turn_off_display"}

    try:
        import screen_brightness_control as sbc  # noqa: PLC0415
    except ImportError as exc:
        raise RuntimeError(
            "Brightness control requires 'screen-brightness-control'. Run: pip install -r pc_client/requirements.txt"
        ) from exc

    monitor_names = list(sbc.list_monitors())
    if action == "get_brightness":
        levels: list[dict[str, Any]] = []
        for idx, name in enumerate(monitor_names, start=1):
            try:
                vals = sbc.get_brightness(display=idx - 1)
                pct = vals[0] if isinstance(vals, list) and vals else vals
            except Exception as exc:
                pct = f"unavailable ({exc})"
            levels.append({"monitor": idx, "name": name, "brightness_pct": pct})
        return {"ok": True, "monitors": levels}

    level = clamp_percentage(arguments.get("brightness_pct"), "brightness_pct")
    target_spec = arguments.get("monitor")
    display_arg = _resolve_brightness_target(monitor_names, target_spec)
    sbc.set_brightness(level, display=display_arg)
    return {
        "ok": True,
        "action": "set_brightness",
        "brightness_pct": level,
        "monitor": "all" if display_arg is None else target_spec,
    }


def _resolve_brightness_target(monitor_names: list[str], spec: Any) -> int | str | None:
    if spec is None or str(spec).strip().lower() in ("", "all"):
        return None
    if isinstance(spec, int) and not isinstance(spec, bool):
        if 1 <= spec <= len(monitor_names):
            return spec - 1
        raise ValueError(f"Monitor {spec} not found (detected {len(monitor_names)} brightness-capable monitor(s))")
    clean = str(spec).strip().lower()
    if clean in ("primary", "main"):
        return 0 if monitor_names else None
    if clean.isdigit():
        idx = int(clean)
        if 1 <= idx <= len(monitor_names):
            return idx - 1
        raise ValueError(f"Monitor {idx} not found")
    for idx, name in enumerate(monitor_names):
        if clean in name.lower():
            return idx
    raise ValueError(f"Monitor '{spec}' not found; available: {monitor_names}")


# ---------------------------------------------------------------- Power

def run_control_pc_power(arguments: dict[str, Any]) -> dict[str, Any]:
    action, delay_s = validate_power_action(arguments.get("action"), arguments.get("delay_seconds"))
    if not _is_windows():
        raise RuntimeError("Power control is only supported on Windows")

    if action == "sleep":
        ok = bool(ctypes.windll.powrprof.SetSuspendState(False, False, False))
        return {"ok": ok, "action": "sleep"}
    if action == "hibernate":
        ok = bool(ctypes.windll.powrprof.SetSuspendState(True, False, False))
        return {"ok": ok, "action": "hibernate"}
    if action == "cancel_shutdown":
        res = subprocess.run(
            ["shutdown.exe", "/a"],
            capture_output=True,
            text=True,
            timeout=SHUTDOWN_TIMEOUT_S,
            creationflags=CREATE_NO_WINDOW,
        )
        if res.returncode == 0:
            return {"ok": True, "action": "cancel_shutdown", "message": "Scheduled shutdown cancelled"}
        if res.returncode == 1116:
            return {"ok": True, "action": "cancel_shutdown", "message": "No shutdown was in progress"}
        raise RuntimeError(f"cancel_shutdown failed (exit {res.returncode}): {(res.stderr or res.stdout).strip()}")

    flag = "/s" if action == "shutdown" else "/r"
    res = subprocess.run(
        ["shutdown.exe", flag, "/t", str(delay_s)],
        capture_output=True,
        text=True,
        timeout=SHUTDOWN_TIMEOUT_S,
        creationflags=CREATE_NO_WINDOW,
    )
    if res.returncode != 0:
        raise RuntimeError(f"{action} failed (exit {res.returncode}): {(res.stderr or res.stdout).strip()}")
    return {
        "ok": True,
        "action": action,
        "delay_seconds": delay_s,
        "message": f"{action.capitalize()} scheduled in {delay_s}s (can be cancelled with cancel_shutdown)",
    }


# ---------------------------------------------------------------- Telemetry / status

def run_get_pc_status(_arguments: dict[str, Any]) -> dict[str, Any]:
    try:
        import psutil  # noqa: PLC0415
    except ImportError as exc:
        raise RuntimeError("PC status requires 'psutil'. Run: pip install -r pc_client/requirements.txt") from exc

    cpu_pct = round(float(psutil.cpu_percent(interval=0.25)), 1)
    mem = psutil.virtual_memory()
    disk_root = os.environ.get("SystemDrive", "C:") + "\\" if os.name == "nt" else "/"
    disk = psutil.disk_usage(disk_root)
    uptime_s = max(0, int(time.time() - psutil.boot_time()))

    battery_info: dict[str, Any] | None = None
    batt = psutil.sensors_battery()
    if batt is not None:
        battery_info = {
            "percent": round(float(batt.percent), 1),
            "plugged_in": bool(batt.power_plugged),
            "seconds_left": int(batt.secsleft) if isinstance(batt.secsleft, (int, float)) and batt.secsleft >= 0 else None,
        }

    top_procs = _collect_top_processes(psutil)
    gpus, gpu_status = _query_nvidia_gpus()

    result: dict[str, Any] = {
        "ok": True,
        "cpu_pct": cpu_pct,
        "ram_used_gb": round(mem.used / (1024**3), 2),
        "ram_total_gb": round(mem.total / (1024**3), 2),
        "ram_pct": round(float(mem.percent), 1),
        "disk_drive": disk_root,
        "disk_free_gb": round(disk.free / (1024**3), 1),
        "disk_total_gb": round(disk.total / (1024**3), 1),
        "disk_pct": round(float(disk.percent), 1),
        "uptime_seconds": uptime_s,
        "battery": battery_info,
        "gpus": gpus,
        "gpu_status": gpu_status,
        "top_processes": top_procs,
    }
    return result


def _collect_top_processes(psutil: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for proc in psutil.process_iter(["pid", "name", "memory_info"]):
        try:
            info = proc.info
            mem_info = info.get("memory_info")
            rss_mb = round(mem_info.rss / (1024 * 1024), 1) if mem_info else 0.0
            rows.append({"pid": int(info["pid"]), "name": str(info.get("name") or ""), "ram_mb": rss_mb})
        except (psutil.NoSuchProcess, psutil.AccessDenied, KeyError):
            continue
    rows.sort(key=lambda r: r["ram_mb"], reverse=True)
    return rows[:TOP_PROCESS_COUNT]


def _query_nvidia_gpus() -> tuple[list[dict[str, Any]], str]:
    try:
        res = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,temperature.gpu,utilization.gpu,memory.used,memory.total",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=NVIDIA_SMI_TIMEOUT_S,
            creationflags=CREATE_NO_WINDOW,
        )
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return [], "unavailable"
    if res.returncode != 0:
        return [], "unavailable"
    gpus = parse_nvidia_smi_csv(res.stdout)
    return gpus, ("ok" if gpus else "unavailable")
