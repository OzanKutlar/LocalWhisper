"""FastAPI server for LocalWhisper (CUDA Qwen3-ASR + PC client WebSocket hub)."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

SERVER_DIR = Path(__file__).resolve().parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from agent_engine import AgentEngine  # noqa: E402
from asr_engine import AsrEngine  # noqa: E402
from client_hub import ClientDispatchError, ClientHub, NoClientConnectedError  # noqa: E402
from handoff import HandoffError, HandoffStore, save_upload  # noqa: E402
from phone_hub import PhoneHub  # noqa: E402
from discovery import (  # noqa: E402
    DEFAULT_DISCOVERY_PORT as DISCOVERY_PORT_FALLBACK,
    default_server_name,
    start_discovery_responder,
)
from llm_asr_engine import LlmAsrEngine  # noqa: E402
from llm_capabilities import (  # noqa: E402
    LlmCapabilities,
    choose_asr_backend,
    probe_llm_capabilities,
)
from updater import (  # noqa: E402
    CREATE_NEW_PROCESS_GROUP,
    CREATE_NO_WINDOW,
    REPO_ROOT,
    get_git_commit,
    read_update_state,
    write_update_state,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
LOGGER = logging.getLogger("localwhisper.server")

DEFAULT_HOST = os.environ.get("LOCALWHISPER_HOST", "0.0.0.0")
DEFAULT_PORT = int(os.environ.get("LOCALWHISPER_PORT", "8765"))
DEFAULT_MODEL = os.environ.get("LOCALWHISPER_MODEL", "Qwen/Qwen3-ASR-1.7B")
DEFAULT_DEVICE = os.environ.get("LOCALWHISPER_DEVICE", "cuda:0")
DEFAULT_LLM_BASE_URL = os.environ.get("LOCALWHISPER_LLM_BASE_URL", "http://127.0.0.1:34501/v1")
DEFAULT_LLM_MODEL = os.environ.get("LOCALWHISPER_LLM_MODEL", "llama-server")
DEFAULT_LLM_API_KEY = os.environ.get("LOCALWHISPER_LLM_API_KEY", "local")
ASR_BACKENDS = ("auto", "local", "llm")
DEFAULT_ASR_BACKEND = os.environ.get("LOCALWHISPER_ASR_BACKEND", "auto")
DEFAULT_DISCOVERY_PORT = int(
    os.environ.get("LOCALWHISPER_DISCOVERY_PORT", str(DISCOVERY_PORT_FALLBACK))
)
UPDATE_EXIT_CODE = 42
SUPERVISED_ENV_VAR = "LOCALWHISPER_SUPERVISED"
DEFAULT_HANDOFF_DIR = SERVER_DIR / "handoff"
FILE_TARGETS = ("pc", "phone")
# Uvicorn pings every WebSocket at this interval. Phones never ping on their own (battery), so a
# long interval keeps their radio asleep; PC clients still ping every 20 s from their side.
WS_PING_INTERVAL_S = 240.0
WS_PING_TIMEOUT_S = 60.0


class SendKeystrokesRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=16000)
    client_id: str | None = Field(default=None)


class AgentRunRequest(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=16000)
    client_id: str | None = Field(default=None)
    session_id: str | None = Field(default=None)
    history: list[dict[str, str]] | None = Field(default=None)
    phone_screen: dict[str, object] | None = Field(default=None)
    interjection: bool = Field(default=False)
    origin: str = Field(default="phone", max_length=16)
    attachments: list[dict[str, object]] | None = Field(default=None)


class AgentConfirmRequest(BaseModel):
    session_id: str = Field(..., min_length=1, max_length=64)
    confirmation_id: str = Field(..., min_length=1, max_length=64)
    approved: bool
    client_id: str | None = Field(default=None)
    phone_screen: dict[str, object] | None = Field(default=None)
    phone_action_result: dict[str, object] | None = Field(default=None)
    rejection_reason: str | None = Field(default=None, max_length=2000)


@dataclass
class AsrSelection:
    """ASR engine picked at startup; filled in by the app lifespan."""

    engine: AsrEngine | LlmAsrEngine | None = None
    backend: str = "pending"
    modalities: dict[str, bool] = field(default_factory=dict)


def _create_asr_engine(
    backend: str,
    caps: LlmCapabilities | None,
    model_id: str,
    device: str,
    llm_base_url: str,
    llm_model: str,
    llm_api_key: str,
) -> AsrEngine | LlmAsrEngine:
    if backend == "llm":
        LOGGER.info("ASR backend: llama-server (audio input supported); Qwen3-ASR will not be loaded")
        return LlmAsrEngine(
            base_url=llm_base_url,
            model=llm_model,
            api_key=llm_api_key,
            display_name=caps.model_name if caps is not None else "",
        )
    LOGGER.info("ASR backend: local %s on %s", model_id, device)
    return AsrEngine(model_id=model_id, device=device)


def build_asr_engine(
    requested_backend: str,
    model_id: str,
    device: str,
    llm_base_url: str,
    llm_model: str,
    llm_api_key: str,
) -> tuple[AsrEngine | LlmAsrEngine, str, dict[str, bool]]:
    """Probes llama-server (unless 'local'), picks the ASR backend and loads it. Blocking."""
    requested = str(requested_backend or "").strip().lower()
    if requested not in ASR_BACKENDS:
        raise ValueError(f"Unknown ASR backend '{requested_backend}'; use one of {', '.join(ASR_BACKENDS)}")
    caps: LlmCapabilities | None = None
    if requested != "local":
        caps = probe_llm_capabilities(llm_base_url, llm_api_key)
        LOGGER.info("llama-server modalities: %s (%s)", caps.modalities(), caps.detail)
    backend = choose_asr_backend(requested, caps)
    engine = _create_asr_engine(backend, caps, model_id, device, llm_base_url, llm_model, llm_api_key)
    engine.load()
    return engine, backend, caps.modalities() if caps is not None else {}


def create_app(
    model_id: str = DEFAULT_MODEL,
    device: str = DEFAULT_DEVICE,
    asr_backend: str = DEFAULT_ASR_BACKEND,
    llm_base_url: str = DEFAULT_LLM_BASE_URL,
    llm_model: str = DEFAULT_LLM_MODEL,
    llm_api_key: str = DEFAULT_LLM_API_KEY,
    bind_host: str = DEFAULT_HOST,
    bind_port: int = DEFAULT_PORT,
    relaunch_args: list[str] | None = None,
    on_update_exit: Callable[[], None] | None = None,
    discovery_port: int = DEFAULT_DISCOVERY_PORT,
    server_name: str | None = None,
    handoff_dir: Path | None = None,
) -> FastAPI:
    asr_selection = AsrSelection()
    display_name = (server_name or "").strip() or default_server_name()
    client_hub = ClientHub()
    phone_hub = PhoneHub()
    handoff_store = HandoffStore(handoff_dir or DEFAULT_HANDOFF_DIR)
    agent_engine = AgentEngine(
        client_hub=client_hub,
        llm_base_url=llm_base_url,
        llm_model=llm_model,
        llm_api_key=llm_api_key,
        enable_thinking=True,
        phone_hub=phone_hub,
    )
    shutting_down_for_update = False
    active_update_id = ""

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        # Discovery starts first so the phone can find the server while the ASR model loads.
        discovery_transport = None
        if 0 < discovery_port <= 65535:
            discovery_transport = await start_discovery_responder(
                port=discovery_port,
                name=display_name,
                http_port=bind_port,
                backend_getter=lambda: asr_selection.backend,
            )
        try:
            engine, backend, modalities = await asyncio.to_thread(
                build_asr_engine,
                asr_backend,
                model_id,
                device,
                llm_base_url,
                llm_model,
                llm_api_key,
            )
            asr_selection.engine = engine
            asr_selection.backend = backend
            asr_selection.modalities = modalities
            yield
        finally:
            if discovery_transport is not None:
                discovery_transport.close()

    def current_asr_engine() -> AsrEngine | LlmAsrEngine:
        engine = asr_selection.engine
        if engine is None:
            raise HTTPException(status_code=503, detail="ASR backend is still starting")
        return engine

    app = FastAPI(title="LocalWhisper Server", version="1.0.0", lifespan=lifespan)

    @app.get("/api/health")
    async def health() -> dict[str, object]:
        clients = await client_hub.list_clients()
        engine = asr_selection.engine
        return {
            "status": "ok",
            "model": engine.model_id if engine is not None else "starting",
            "agent_model": agent_engine.llm_model,
            "device": engine.device if engine is not None else "unknown",
            "gpu_name": engine.gpu_name if engine is not None else "unknown",
            "asr_backend": asr_selection.backend,
            "llm_modalities": asr_selection.modalities,
            "server_name": display_name,
            "connected_clients": clients,
            "connected_phones": await phone_hub.list_phones(),
        }

    @app.get("/api/clients")
    async def list_clients() -> dict[str, object]:
        return {"clients": await client_hub.list_clients()}

    @app.get("/api/phones")
    async def list_phones() -> dict[str, object]:
        return {"phones": await phone_hub.list_phones()}

    @app.get("/api/update/status")
    async def get_update_status() -> dict[str, object]:
        current_commit = await asyncio.to_thread(get_git_commit, REPO_ROOT)
        saved = await asyncio.to_thread(read_update_state)
        if shutting_down_for_update:
            return {
                "update_id": active_update_id or saved.get("update_id", ""),
                "phase": "closing",
                "ok": True,
                "message": "Closing server to run git pull...",
                "old_commit": saved.get("old_commit", current_commit),
                "new_commit": current_commit,
                "git_output": "",
                "git_summary": "",
            }
        return {
            "update_id": saved.get("update_id", ""),
            "phase": "online",
            "ok": bool(saved.get("ok", True)),
            "message": saved.get("message", f"Server online ({current_commit})"),
            "old_commit": saved.get("old_commit", current_commit),
            "new_commit": current_commit,
            "git_output": saved.get("git_output", ""),
            "git_summary": saved.get("git_summary", ""),
        }

    @app.post("/api/update")
    async def trigger_server_update() -> dict[str, object]:
        nonlocal shutting_down_for_update, active_update_id
        if shutting_down_for_update:
            return {
                "status": "closing",
                "update_id": active_update_id,
                "message": "Server update is already in progress...",
            }

        shutting_down_for_update = True
        active_update_id = uuid.uuid4().hex[:10]
        old_commit = await asyncio.to_thread(get_git_commit, REPO_ROOT)
        initial_state = {
            "update_id": active_update_id,
            "phase": "closing",
            "ok": True,
            "message": "Closing server to run git pull...",
            "old_commit": old_commit,
            "new_commit": old_commit,
            "git_output": "",
            "git_summary": "",
            "host": bind_host,
            "port": bind_port,
            "updated_at": time.time(),
        }
        await asyncio.to_thread(write_update_state, initial_state)

        supervised = os.environ.get(SUPERVISED_ENV_VAR) == "1"
        if not supervised:
            updater_script = SERVER_DIR / "updater.py"
            args_for_relaunch = relaunch_args if relaunch_args is not None else sys.argv[1:]
            cmd = [
                sys.executable,
                str(updater_script),
                "--parent-pid",
                str(os.getpid()),
                "--host",
                bind_host,
                "--port",
                str(bind_port),
                "--repo-dir",
                str(REPO_ROOT),
                "--update-id",
                active_update_id,
                "--relaunch-args-json",
                json.dumps(args_for_relaunch),
            ]
            if os.name == "nt":
                subprocess.Popen(
                    cmd,
                    cwd=str(REPO_ROOT),
                    creationflags=CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP,
                )
            else:
                subprocess.Popen(cmd, cwd=str(REPO_ROOT), start_new_session=True)

        exit_code = UPDATE_EXIT_CODE if supervised else 0

        def _terminate_self() -> None:
            time.sleep(0.3)
            LOGGER.info("Closing server for git pull update [update_id=%s]...", active_update_id)
            threading.Timer(1.5, lambda: os._exit(exit_code)).start()
            if supervised and on_update_exit is not None:
                on_update_exit()
                return
            if supervised:
                os._exit(exit_code)
            try:
                os.kill(os.getpid(), signal.SIGINT)
            except Exception:
                os._exit(exit_code)

        threading.Thread(target=_terminate_self, daemon=True).start()
        return {
            "status": "closing",
            "update_id": active_update_id,
            "old_commit": old_commit,
            "message": "Closing server to run git pull and relaunch...",
        }

    @app.post("/api/transcribe")
    async def transcribe(
        audio: UploadFile = File(...),
        language: str | None = Form(default=None),
    ) -> dict[str, object]:
        engine = current_asr_engine()
        audio_bytes = await audio.read()
        try:
            result = await asyncio.to_thread(engine.transcribe_bytes, audio_bytes, language)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            LOGGER.exception("ASR transcription failed")
            raise HTTPException(status_code=500, detail=f"ASR failed: {exc}") from exc

        return {
            "text": result.text,
            "language": result.language,
            "duration_ms": result.duration_ms,
        }

    @app.post("/api/send")
    async def send_keystrokes(request: SendKeystrokesRequest) -> dict[str, object]:
        try:
            target_id, target_name = await client_hub.send_keystrokes(
                text=request.text,
                client_id=request.client_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except NoClientConnectedError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except ClientDispatchError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

        return {
            "status": "sent",
            "client_id": target_id,
            "client_name": target_name,
        }

    @app.post("/api/agent")
    async def run_agent(request: AgentRunRequest) -> dict[str, object]:
        try:
            result = await agent_engine.run_turn(
                prompt=request.prompt,
                client_id=request.client_id,
                session_id=request.session_id,
                history=request.history,
                phone_screen=request.phone_screen,
                interjection=request.interjection,
                origin=request.origin,
                attachments=request.attachments,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            LOGGER.exception("Agent execution failed")
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return result.to_dict()

    @app.post("/api/agent/confirm")
    async def confirm_agent_tool(request: AgentConfirmRequest) -> dict[str, object]:
        try:
            result = await agent_engine.confirm_tool(
                session_id=request.session_id,
                confirmation_id=request.confirmation_id,
                approved=request.approved,
                client_id=request.client_id,
                phone_screen=request.phone_screen,
                phone_action_result=request.phone_action_result,
                rejection_reason=request.rejection_reason,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            LOGGER.exception("Agent confirmation failed")
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return result.to_dict()

    @app.post("/api/agent/stream")
    async def run_agent_stream(request: AgentRunRequest) -> StreamingResponse:
        async def _event_generator():
            try:
                async for event in agent_engine.run_turn_stream(
                    prompt=request.prompt,
                    client_id=request.client_id,
                    session_id=request.session_id,
                    history=request.history,
                    phone_screen=request.phone_screen,
                    interjection=request.interjection,
                    origin=request.origin,
                    attachments=request.attachments,
                ):
                    yield json.dumps(event) + "\n"
            except Exception as exc:
                LOGGER.exception("Agent stream failed")
                yield json.dumps({"type": "error", "detail": str(exc)}) + "\n"

        return StreamingResponse(_event_generator(), media_type="application/x-ndjson")

    @app.post("/api/agent/confirm/stream")
    async def confirm_agent_tool_stream(request: AgentConfirmRequest) -> StreamingResponse:
        async def _event_generator():
            try:
                async for event in agent_engine.confirm_tool_stream(
                    session_id=request.session_id,
                    confirmation_id=request.confirmation_id,
                    approved=request.approved,
                    client_id=request.client_id,
                    phone_screen=request.phone_screen,
                    phone_action_result=request.phone_action_result,
                    rejection_reason=request.rejection_reason,
                ):
                    yield json.dumps(event) + "\n"
            except Exception as exc:
                LOGGER.exception("Agent confirmation stream failed")
                yield json.dumps({"type": "error", "detail": str(exc)}) + "\n"

        return StreamingResponse(_event_generator(), media_type="application/x-ndjson")

    @app.post("/api/files")
    async def upload_file(
        file: UploadFile = File(...),
        target: str = Form(...),
        client_id: str | None = Form(default=None),
        source: str = Form(default=""),
    ) -> dict[str, object]:
        clean_target = target.strip().lower()
        if clean_target not in FILE_TARGETS:
            raise HTTPException(status_code=400, detail="target must be 'pc' or 'phone'")
        try:
            stored = await save_upload(handoff_store, file, source=source, target=clean_target)
        except HandoffError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        except OSError as exc:
            LOGGER.exception("Storing an uploaded file failed")
            raise HTTPException(status_code=500, detail=f"Could not store the file: {exc}") from exc
        offer = stored.offer_payload()
        if clean_target == "pc":
            delivered = await client_hub.notify(offer, client_id=client_id)
        else:
            delivered = await phone_hub.notify(offer)
        return {**stored.to_dict(), "delivered": delivered}

    @app.get("/api/files/{file_id}")
    async def download_file(file_id: str) -> FileResponse:
        stored = await asyncio.to_thread(handoff_store.get, file_id)
        if stored is None:
            raise HTTPException(status_code=404, detail="File not found or expired")
        return FileResponse(stored.data_path, media_type=stored.mime_type, filename=stored.name)

    @app.websocket("/ws/phone")
    async def phone_websocket(
        websocket: WebSocket,
        name: str = Query(default="Phone"),
    ) -> None:
        await websocket.accept()
        phone = await phone_hub.register(websocket, name)
        try:
            await websocket.send_text(
                json.dumps({"type": "welcome", "phone_id": phone.phone_id, "name": phone.name})
            )
            async for raw_message in websocket.iter_text():
                await phone_hub.handle_message(phone.phone_id, raw_message)
        except WebSocketDisconnect:
            pass
        except Exception as exc:
            LOGGER.warning("Phone link error for %s: %s", phone.name, exc)
        finally:
            await phone_hub.unregister(phone.phone_id)

    @app.websocket("/ws/client")
    async def pc_client_websocket(
        websocket: WebSocket,
        name: str = Query(default="PC-Client"),
    ) -> None:
        await websocket.accept()
        client = await client_hub.register(websocket, name)
        try:
            await websocket.send_text(
                json.dumps({"type": "welcome", "client_id": client.client_id, "name": client.name})
            )
            async for raw_message in websocket.iter_text():
                await _handle_client_message(client_hub, raw_message)
        except WebSocketDisconnect:
            pass
        except Exception as exc:
            LOGGER.warning("WebSocket error for client %s: %s", client.name, exc)
        finally:
            await client_hub.unregister(client.client_id)

    return app


async def _handle_client_message(hub: ClientHub, raw_message: str) -> None:
    try:
        message = json.loads(raw_message)
    except json.JSONDecodeError:
        LOGGER.warning("Ignoring malformed JSON from PC client")
        return

    msg_type = message.get("type")
    if msg_type == "ack":
        request_id = str(message.get("id", ""))
        if request_id:
            ok = bool(message.get("ok", False))
            error = message.get("error")
            await hub.resolve_ack(request_id, ok, str(error) if error else None)
    elif msg_type == "tool_result":
        request_id = str(message.get("id", ""))
        if request_id:
            ok = bool(message.get("ok", False))
            raw_result = message.get("result")
            result_dict = raw_result if isinstance(raw_result, dict) else None
            error = message.get("error")
            await hub.resolve_tool_result(
                request_id,
                ok,
                result_dict,
                str(error) if error else None,
            )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the LocalWhisper CUDA ASR server.")
    parser.add_argument("--host", default=DEFAULT_HOST, help="Bind host (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Bind port (default: 8765)")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Qwen ASR model ID")
    parser.add_argument("--device", default=DEFAULT_DEVICE, help="CUDA device (default: cuda:0)")
    parser.add_argument(
        "--asr-backend",
        choices=ASR_BACKENDS,
        default=DEFAULT_ASR_BACKEND,
        help=(
            "auto: use llama-server for ASR when its model accepts audio, else load the local model; "
            "local: always load the local ASR model; llm: always use llama-server (default: auto)"
        ),
    )
    parser.add_argument(
        "--llm-base-url",
        default=DEFAULT_LLM_BASE_URL,
        help="OpenAI-compatible LLM base URL for Agent mode (default: http://127.0.0.1:34501/v1)",
    )
    parser.add_argument(
        "--llm-model",
        default=DEFAULT_LLM_MODEL,
        help="Thinking LLM model ID for Agent mode (default: qwen3:8b)",
    )
    parser.add_argument(
        "--llm-api-key",
        default=DEFAULT_LLM_API_KEY,
        help="API key for the LLM endpoint if required",
    )
    parser.add_argument(
        "--discovery-port",
        type=int,
        default=DEFAULT_DISCOVERY_PORT,
        help="UDP port the Android app uses to find this server on the LAN (default: 8766)",
    )
    parser.add_argument(
        "--no-discovery",
        action="store_true",
        help="Do not answer LAN discovery broadcasts from the Android app",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    update_exit_requested = False
    server_holder: dict[str, uvicorn.Server] = {}

    def _request_update_exit() -> None:
        nonlocal update_exit_requested
        update_exit_requested = True
        srv = server_holder.get("server")
        if srv is not None:
            srv.should_exit = True

    app = create_app(
        model_id=args.model,
        device=args.device,
        asr_backend=args.asr_backend,
        llm_base_url=args.llm_base_url,
        llm_model=args.llm_model,
        llm_api_key=args.llm_api_key,
        bind_host=args.host,
        bind_port=args.port,
        relaunch_args=sys.argv[1:],
        on_update_exit=_request_update_exit,
        discovery_port=0 if args.no_discovery else args.discovery_port,
    )
    config = uvicorn.Config(
        app,
        host=args.host,
        port=args.port,
        log_level="info",
        ws_ping_interval=WS_PING_INTERVAL_S,
        ws_ping_timeout=WS_PING_TIMEOUT_S,
    )
    server = uvicorn.Server(config)
    server_holder["server"] = server
    server.run()
    if update_exit_requested:
        sys.exit(UPDATE_EXIT_CODE)


if __name__ == "__main__":
    main()
