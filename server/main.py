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
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

SERVER_DIR = Path(__file__).resolve().parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from agent_engine import AgentEngine  # noqa: E402
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
DEFAULT_LLM_BASE_URL = os.environ.get("LOCALWHISPER_LLM_BASE_URL", "http://127.0.0.1:34501/v1")
DEFAULT_LLM_MODEL = os.environ.get("LOCALWHISPER_LLM_MODEL", "llama-server")
DEFAULT_LLM_API_KEY = os.environ.get("LOCALWHISPER_LLM_API_KEY", "local")


class SendKeystrokesRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=16000)
    client_id: str | None = Field(default=None)


class AgentRunRequest(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=16000)
    client_id: str | None = Field(default=None)
    session_id: str | None = Field(default=None)
    history: list[dict[str, str]] | None = Field(default=None)
    phone_screen: dict[str, object] | None = Field(default=None)


class AgentConfirmRequest(BaseModel):
    session_id: str = Field(..., min_length=1, max_length=64)
    confirmation_id: str = Field(..., min_length=1, max_length=64)
    approved: bool
    client_id: str | None = Field(default=None)


def create_app(
    model_id: str = DEFAULT_MODEL,
    device: str = DEFAULT_DEVICE,
    llm_base_url: str = DEFAULT_LLM_BASE_URL,
    llm_model: str = DEFAULT_LLM_MODEL,
    llm_api_key: str = DEFAULT_LLM_API_KEY,
) -> FastAPI:
    asr_engine = AsrEngine(model_id=model_id, device=device)
    client_hub = ClientHub()
    agent_engine = AgentEngine(
        client_hub=client_hub,
        llm_base_url=llm_base_url,
        llm_model=llm_model,
        llm_api_key=llm_api_key,
        enable_thinking=True,
    )

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
            "agent_model": agent_engine.llm_model,
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

    @app.post("/api/agent")
    async def run_agent(request: AgentRunRequest) -> dict[str, object]:
        try:
            result = await agent_engine.run_turn(
                prompt=request.prompt,
                client_id=request.client_id,
                session_id=request.session_id,
                history=request.history,
                phone_screen=request.phone_screen,
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
                ):
                    yield json.dumps(event) + "\n"
            except Exception as exc:
                LOGGER.exception("Agent confirmation stream failed")
                yield json.dumps({"type": "error", "detail": str(exc)}) + "\n"

        return StreamingResponse(_event_generator(), media_type="application/x-ndjson")

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
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    app = create_app(
        model_id=args.model,
        device=args.device,
        llm_base_url=args.llm_base_url,
        llm_model=args.llm_model,
        llm_api_key=args.llm_api_key,
    )
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
