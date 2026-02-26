from __future__ import annotations
from pathlib import Path

import logging
import json
from typing import Any, AsyncIterator, Dict

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .config import AgentConfig
from .gateway import GatewayClient
from .settings import build_runtime_settings

log = logging.getLogger(__name__)
settings = build_runtime_settings()
config = settings.app_config
client = GatewayClient(config)
app = FastAPI(title="OpenClaw OpenAI Proxy", version="0.1.0")


def _resolve_valves_path() -> Path | None:
    cfg_path = config.pipeline.__dict__.get("valves_config")
    if not cfg_path:
        return None
    raw_path = Path(cfg_path)
    if not raw_path.is_absolute():
        raw_path = settings.config_path.parent / raw_path
    return raw_path


def _load_valves_config() -> Dict[str, Any]:
    raw_path = _resolve_valves_path()
    if not raw_path:
        return {}

    try:
        return json.loads(raw_path.read_text())
    except FileNotFoundError:
        log.warning("Valves config %s not found", raw_path)
    except Exception:
        log.exception("Failed to load valves config from %s", raw_path)
    return {}


def _save_valves_config(payload: Dict[str, Any]) -> None:
    raw_path = _resolve_valves_path()
    if not raw_path:
        raise RuntimeError("valves_config path is not configured")
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False))


def _serialize_agent(agent: AgentConfig) -> Dict[str, Any]:
    return {
        "id": agent.id,
        "object": "model",
        "created": 0,
        "owned_by": "openclaw",
        "name": agent.name or agent.id,
        "description": agent.description,
        "metadata": {
            "agent_id": agent.agent_id,
            "tags": agent.tags,
            "profile_image_url": agent.profile_image_url,
        },
    }


def _serialize_pipeline() -> Dict[str, Any]:
    pipeline = config.pipeline
    return {
        "id": pipeline.id,
        "object": "pipeline",
        "name": pipeline.name,
        "type": "filter",
        "pipelines": pipeline.pipelines,
        "priority": pipeline.priority,
        "description": pipeline.description,
        "valves": bool(pipeline.__dict__.get("valves_config")),
    }


@app.on_event("shutdown")
async def shutdown_event() -> None:
    await client.close()


@app.get("/healthz")
async def healthz() -> Dict[str, str]:
    return {"status": "ok"}


@app.get("/v1/models")
async def list_models() -> Dict[str, Any]:
    return {
        "object": "list",
        "data": [_serialize_agent(agent) for agent in config.agents],
        "pipelines": [_serialize_pipeline()],
    }


@app.get("/models")
async def list_models_alias() -> Dict[str, Any]:
    """Compatibility alias without /v1 prefix."""
    return await list_models()



async def _forward_chat_completion(payload: Dict[str, Any]):
    model_id = payload.get("model")
    if not model_id:
        raise HTTPException(status_code=400, detail="Missing 'model' in payload")

    try:
        agent = client.resolve_agent(model_id)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    payload["model"] = f"openclaw:{agent.agent_id}"

    stream = bool(payload.get("stream", False))
    payload["stream"] = False
    result = await client.chat_completions(payload, False)

    assert isinstance(result, httpx.Response)
    data = result.json()
    return JSONResponse(content=data)


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    payload = await request.json()
    return await _forward_chat_completion(payload)


@app.post("/chat/completions")
async def chat_completions_alias(request: Request):
    """Compatibility alias without /v1 prefix."""
    return await chat_completions(request)



@app.post("/{pipeline_id}/filter/inlet")
async def pipeline_inlet(pipeline_id: str, request: Request):
    if pipeline_id != config.pipeline.id:
        raise HTTPException(status_code=404, detail="Pipeline not found")

    payload = await request.json()
    body = payload.get("body", {})

    metadata = body.get("__metadata__", {})
    chat_id = metadata.get("chat_id")

    if config.pipeline.enforce_user and chat_id:
        body["user"] = chat_id

    enforce_prefix = config.pipeline.enforce_prefix
    if enforce_prefix:
        model_id = body.get("model")
        if isinstance(model_id, str) and not model_id.startswith(enforce_prefix):
            body["model"] = f"{enforce_prefix}{model_id}"

    return body


@app.post("/{pipeline_id}/filter/outlet")
async def pipeline_outlet(pipeline_id: str, request: Request):
    if pipeline_id != config.pipeline.id:
        raise HTTPException(status_code=404, detail="Pipeline not found")

    payload = await request.json()
    return payload.get("body", payload)


@app.get("/pipelines")
async def pipelines() -> Dict[str, Any]:
    return {"data": [_serialize_pipeline()]}

@app.get("/{pipeline_id}/valves")
async def pipeline_valves(pipeline_id: str):
    if pipeline_id != config.pipeline.id:
        raise HTTPException(status_code=404, detail="Pipeline not found")

    valves = [
        {
            "id": "session-key-preview",
            "name": "Anteprima session key",
            "description": "Mostra la logica sha256(user_id + chat_id)",
            "value": "sha256(user_id:chat_id)[:64]",
            "mutable": False,
        }
    ]
    return {"data": valves}


@app.get("/{pipeline_id}/valves/spec")
async def pipeline_valves_spec(pipeline_id: str):
    if pipeline_id != config.pipeline.id:
        raise HTTPException(status_code=404, detail="Pipeline not found")

    spec = {
        "fields": [
            {
                "id": "sessionKeyFormat",
                "label": "Formato session key",
                "type": "text",
                "default": "sha256(user_id:chat_id)[:64]",
                "editable": False,
            }
        ]
    }
    return {"data": spec}

@app.get("/{pipeline_id}/valves")
async def pipeline_valves(pipeline_id: str):
    if pipeline_id != config.pipeline.id:
        raise HTTPException(status_code=404, detail="Pipeline not found")

    cfg = _load_valves_config()
    valves = cfg.get("values")
    if valves is None:
        valves = {}
    return valves


@app.get("/{pipeline_id}/valves/spec")
async def pipeline_valves_spec(pipeline_id: str):
    if pipeline_id != config.pipeline.id:
        raise HTTPException(status_code=404, detail="Pipeline not found")

    cfg = _load_valves_config()
    spec = cfg.get("schema", {})
    return spec


@app.post("/pipelines/add")
async def pipelines_add():
    raise HTTPException(status_code=405, detail="Remote pipeline download not supported")




@app.post("/{pipeline_id}/valves/update")
async def pipeline_valves_update(pipeline_id: str, request: Request):
    if pipeline_id != config.pipeline.id:
        raise HTTPException(status_code=404, detail="Pipeline not found")

    cfg = _load_valves_config()
    values = cfg.get("values")
    if not isinstance(values, dict):
        values = {}

    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid payload")

    values.update(payload)
    cfg["values"] = values
    _save_valves_config(cfg)
    return values

@app.post("/pipelines/upload")
async def pipelines_upload(request: Request):
    if not request.headers.get("content-type", "").startswith("multipart/form-data"):
        raise HTTPException(status_code=400, detail="Upload requires multipart/form-data")

    upload_dir = Path(config.pipeline.__dict__.get("upload_dir", "pipelines-uploaded"))
    upload_dir.mkdir(parents=True, exist_ok=True)

    form = await request.form()
    file = form.get("file")
    if file is None:
        raise HTTPException(status_code=400, detail="Missing file in form data")

    filename = Path(file.filename or "pipeline.py").name
    target_path = (upload_dir / filename).resolve()

    # Accetta solo file .py per sicurezza
    if not target_path.suffix.endswith(".py"):
        raise HTTPException(status_code=400, detail="Only .py files are allowed")

    with target_path.open("wb") as dest:
        dest.write(await file.read())

    return {
        "data": {
            "id": config.pipeline.id,
            "filename": filename,
            "path": str(target_path),
        }
    }

@app.get("/{pipeline_id}/valves")
async def pipeline_valves(pipeline_id: str):
    if pipeline_id != config.pipeline.id:
        raise HTTPException(status_code=404, detail="Pipeline not found")
    valves = [
        {
            "id": "session-key-preview",
            "name": "Anteprima session key",
            "description": "Mostra la logica sha256(user_id + chat_id)",
            "value": "sha256(user_id:chat_id)[:64]",
            "mutable": False,
        }
    ]
    return {"data": valves}


@app.get("/{pipeline_id}/valves/spec")
async def pipeline_valves_spec(pipeline_id: str):
    if pipeline_id != config.pipeline.id:
        raise HTTPException(status_code=404, detail="Pipeline not found")

    spec = {
        "fields": [
            {
                "id": "sessionKeyFormat",
                "label": "Formato session key",
                "type": "text",
                "default": "sha256(user_id:chat_id)[:64]",
                "editable": False,
            }
        ]
    }
    return {"data": spec}
