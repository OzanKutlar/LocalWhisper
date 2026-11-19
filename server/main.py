"""FastAPI server for LocalWhisper (CUDA Qwen3-ASR + PC client WebSocket hub)."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

SERVER_DIR = Path(__file__).resolve().parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from asr_engine import AsrEngine  # noqa: E402
from client_hub import ClientDispatchError, ClientHub, NoClientConnectedError  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
LOGGER = logging.getLogger("localwhisper.server")

DEFAULT_HOST = os.environ.get("LOCALWHISPER_HOST", "0.0.0.0")
DEFAULT_PORT = int(os.environ.get("LOCALWHISPER_PORT", "8765"))
DEFAULT_MODEL = os.environ.get("LOCALWHISPER_MODEL", "Qwen/Qwen3-ASR-1.7B")
DEFAULT_DEVICE = os.environ.get("LOCALWHISPER_DEVICE", "cuda:0")


class SendKeystrokesRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=16000)
    client_id: str | None = Field(default=None)


def create_app(model_id: str = DEFAULT_MODEL, device: str = DEFAULT_DEVICE) -> FastAPI:
    asr_engine = AsrEngine(model_id=model_id, device=device)
    client_hub = ClientHub()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await asyncio.to_thread(asr_engine.load)
        yield

    app = FastAPI(title="LocalWhisper Server", version="1.0.0", lifespan=lifespan)

    @app.get("/api/health")
    async def health() -> dict[str, object]:
        clients = await client_hub.list_clients()
        return {
            "status": "ok",
            "model": asr_engine.model_id,
            "device": asr_engine.device,
            "gpu_name": asr_engine.gpu_name,
            "connected_clients": clients,
        }

    @app.get("/api/clients")
    async def list_clients() -> dict[str, object]:
        return {"clients": await client_hub.list_clients()}

    @app.post("/api/transcribe")
    async def transcribe(
        audio: UploadFile = File(...),
        language: str | None = Form(default=None),
    ) -> dict[str, object]:
        audio_bytes = await audio.read()
        try:
            result = await asyncio.to_thread(asr_engine.transcribe_bytes, audio_bytes, language)
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the LocalWhisper CUDA ASR server.")
    parser.add_argument("--host", default=DEFAULT_HOST, help="Bind host (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Bind port (default: 8765)")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Qwen ASR model ID")
    parser.add_argument("--device", default=DEFAULT_DEVICE, help="CUDA device (default: cuda:0)")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    app = create_app(model_id=args.model, device=args.device)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
