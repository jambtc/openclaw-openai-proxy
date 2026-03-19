from __future__ import annotations

import asyncio
import base64
from pathlib import Path
import hashlib
import ipaddress
import json
import logging
import time
from urllib.parse import urlparse
import uuid
from typing import Any, Dict
from datetime import datetime

import httpx
import websockets
from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.websockets import WebSocketDisconnect
from fastapi.responses import JSONResponse, Response, StreamingResponse

from .backend import BackendClient
from .config import AgentConfig
from .settings import build_runtime_settings

log = logging.getLogger(__name__)

settings = build_runtime_settings()
config = settings.app_config
backend_client = BackendClient(config)

app = FastAPI(title="OpenClaw OpenAI Proxy", version="0.1.0")

# In-memory mapping for edge upload compatibility endpoints (/api/v1/files*).
# This is a Phase-1 store and will be replaced by persistent storage.
EDGE_FILE_STORE: dict[str, dict[str, Any]] = {}
EDGE_CHAT_STORE: dict[str, dict[str, Any]] = {}
EDGE_PENDING_PROVIDER_CONTEXTS: list[dict[str, Any]] = []
EDGE_PENDING_PROVIDER_TTL_SECONDS = 120
HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


def _json_for_log(payload: Any, max_chars: int = 4000) -> str:
    try:
        raw = json.dumps(payload, ensure_ascii=False, default=str)
    except Exception:
        raw = str(payload)
    if len(raw) > max_chars:
        return f"{raw[:max_chars]}...<truncated {len(raw) - max_chars} chars>"
    return raw


def _messages_for_log(messages: Any) -> str:
    if not isinstance(messages, list):
        return _json_for_log(messages)

    compact: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, dict):
            compact.append({"raw": str(message)})
            continue

        content = message.get("content")
        compact_content: Any
        if isinstance(content, str):
            compact_content = content
        elif isinstance(content, list):
            parts: list[Any] = []
            for part in content:
                if isinstance(part, dict):
                    parts.append(
                        {
                            "type": part.get("type"),
                            "text": part.get("text"),
                            "url": part.get("url"),
                        }
                    )
                else:
                    parts.append(part)
            compact_content = parts
        else:
            compact_content = content

        compact.append(
            {
                "id": message.get("id"),
                "role": message.get("role"),
                "content": compact_content,
                "files": message.get("files"),
            }
        )
    return _json_for_log(compact, max_chars=12000)


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


def _get_body(payload: Dict[str, Any]) -> Dict[str, Any]:
    # Requests from Open WebUI pipelines use {"body": {...}}; plain OpenAI calls send the body directly.
    body = payload.get("body")
    if isinstance(body, dict):
        return body
    if isinstance(payload, dict):
        return payload
    return {}


def _get_chat_id(payload: Dict[str, Any], body: Dict[str, Any]) -> str | None:
    # WebUI may place __metadata__ at top-level OR inside body (depending on path).
    meta = payload.get("__metadata__") or body.get("__metadata__") or {}
    if isinstance(meta, dict):
        cid = meta.get("chat_id")
        return cid if isinstance(cid, str) and cid else None
    return None


def _get_user_id(payload: Dict[str, Any]) -> str:
    u = payload.get("__user__") or {}
    if isinstance(u, dict):
        return str(u.get("id") or u.get("email") or "anon")
    return "anon"


def _session_key(user_id: str, chat_id: str) -> str:
    # sha256(user_id:chat_id) => 64 hex chars
    return hashlib.sha256(f"{user_id}:{chat_id}".encode("utf-8")).hexdigest()


@app.on_event("shutdown")
async def shutdown_event() -> None:
    await backend_client.close()


@app.get("/healthz")
async def healthz() -> Dict[str, str]:
    return {"status": "ok"}


# -------------------------
# OpenAI-compatible endpoints
# -------------------------
@app.get("/v1/models")
async def list_models(request: Request) -> Response:
    headers = _bridge_headers_from_request(request)

    try:
        be_response = await backend_client.get(path="/v1/models", headers=headers)
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502,
            detail={"message": "Failed forwarding model list request to BE", "error": str(exc)},
        ) from exc

    try:
        be_payload = be_response.json()
    except Exception:
        be_payload = {"raw_response": be_response.text}

    if isinstance(be_payload, dict) and be_response.status_code < 400:
        be_payload = dict(be_payload)
        # Non-standard extension used by Open WebUI (handy for discovering pipelines)
        be_payload.setdefault("pipelines", [_serialize_pipeline()])

    return JSONResponse(status_code=be_response.status_code, content=be_payload)


@app.get("/models")
async def list_models_alias(request: Request) -> Response:
    """Compatibility alias without /v1 prefix."""
    return await list_models(request)


def _resolve_agent(model_id: str) -> AgentConfig | None:
    for agent in config.agents:
        if agent.id == model_id:
            return agent
    return None


def _normalize_openai_model(
    payload: Dict[str, Any], require_model: bool = True
) -> None:
    model_id = payload.get("model")
    if not model_id:
        if require_model:
            raise HTTPException(status_code=400, detail="Missing 'model' in payload")
        return

    if not isinstance(model_id, str):
        if require_model:
            raise HTTPException(status_code=400, detail="Invalid 'model' in payload")
        return

    if model_id.startswith("openclaw:") or model_id.startswith("agent:"):
        return

    agent = _resolve_agent(model_id)
    if agent is not None:
        payload["model"] = f"openclaw:{agent.agent_id}"
        return

    # Dynamic model ids coming from BE /v1/models must pass through unchanged.
    payload["model"] = model_id


def _is_upstream_not_found(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    detail = payload.get("detail")
    if isinstance(detail, dict):
        return str(detail.get("raw", "")).strip().lower() == "not found"
    if isinstance(detail, str):
        return detail.strip().lower() == "not found"
    return False


def _extract_responses_input_text(input_value: Any) -> str:
    if isinstance(input_value, str):
        return input_value

    if isinstance(input_value, list):
        chunks: list[str] = []
        for item in input_value:
            if isinstance(item, str):
                chunks.append(item)
                continue
            if not isinstance(item, dict):
                continue

            item_type = item.get("type")
            if item_type in {"input_text", "text"} and isinstance(item.get("text"), str):
                chunks.append(item["text"])
                continue

            content = item.get("content")
            if isinstance(content, str):
                chunks.append(content)
                continue
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, str):
                        chunks.append(part)
                    elif isinstance(part, dict) and isinstance(part.get("text"), str):
                        chunks.append(part["text"])
        text = "\n".join([c for c in chunks if c]).strip()
        if text:
            return text

    if isinstance(input_value, dict):
        if isinstance(input_value.get("text"), str):
            return input_value["text"]
        if isinstance(input_value.get("content"), str):
            return input_value["content"]

    return str(input_value or "")


def _build_chat_fallback_payload_from_responses(
    payload: Dict[str, Any]
) -> Dict[str, Any]:
    text = _extract_responses_input_text(payload.get("input"))
    if not text:
        raise HTTPException(
            status_code=400,
            detail="Unsupported /v1/responses input for fallback translation",
        )

    chat_payload: Dict[str, Any] = {
        "model": payload.get("model") or "openclaw",
        "messages": [{"role": "user", "content": text}],
        "stream": False,
    }

    # Preserve common generation controls when present.
    for source_key, target_key in (
        ("user", "user"),
        ("temperature", "temperature"),
        ("top_p", "top_p"),
        ("stop", "stop"),
        ("n", "n"),
    ):
        value = payload.get(source_key)
        if value is not None:
            chat_payload[target_key] = value

    mot = payload.get("max_output_tokens")
    if mot is not None:
        chat_payload["max_tokens"] = mot

    return chat_payload


def _chat_completion_to_responses_shape(
    chat_payload: Dict[str, Any], chat_result: Dict[str, Any]
) -> Dict[str, Any]:
    choices = chat_result.get("choices") or []
    first_choice = choices[0] if choices else {}
    message = first_choice.get("message") if isinstance(first_choice, dict) else {}
    content = message.get("content") if isinstance(message, dict) else ""

    if isinstance(content, list):
        output_text = " ".join(str(x) for x in content)
    elif isinstance(content, str):
        output_text = content
    else:
        output_text = str(content or "")

    usage = chat_result.get("usage") or {}
    return {
        "id": f"resp_{uuid.uuid4().hex}",
        "object": "response",
        "created_at": chat_result.get("created") or int(time.time()),
        "status": "completed",
        "model": chat_result.get("model") or chat_payload.get("model"),
        "output": [
            {
                "id": f"msg_{uuid.uuid4().hex}",
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [
                    {
                        "type": "output_text",
                        "text": output_text,
                        "annotations": [],
                    }
                ],
            }
        ],
        "output_text": output_text,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
        },
    }


def _to_epoch_seconds(value: Any) -> int:
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        try:
            return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
        except ValueError:
            pass
    return int(time.time())


def _try_decode_unverified_jwt_user_id(auth_header: str | None) -> str | None:
    if not auth_header:
        return None
    if not auth_header.lower().startswith("bearer "):
        return None
    token = auth_header.split(" ", 1)[1].strip()
    parts = token.split(".")
    if len(parts) < 2:
        return None

    payload_part = parts[1]
    padding = "=" * (-len(payload_part) % 4)
    try:
        decoded = base64.urlsafe_b64decode(payload_part + padding).decode("utf-8")
        payload = json.loads(decoded)
    except Exception:
        return None

    for key in ("id", "user_id", "sub", "uid", "email"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _edge_user_id_from_request(request: Request) -> str:
    for header_name in ("x-debug-user", "x-openwebui-user-id"):
        value = request.headers.get(header_name)
        if value:
            return value

    jwt_user = _try_decode_unverified_jwt_user_id(request.headers.get("authorization"))
    if jwt_user:
        return jwt_user
    return "edge-user"


def _edge_backend_headers_from_request(request: Request, user_id: str) -> dict[str, str]:
    headers = _bridge_headers_from_request(request)
    if "x-debug-user" not in headers and user_id:
        headers["x-debug-user"] = user_id
    return headers


def _adapt_be_upload_to_box_shape(be_payload: dict[str, Any], user_id: str) -> dict[str, Any]:
    file_id = str(be_payload.get("upload_id") or uuid.uuid4())
    filename = str(be_payload.get("filename") or "uploaded-file")
    mime_type = be_payload.get("mime_type")
    size_bytes = be_payload.get("size_bytes")
    created_at = _to_epoch_seconds(be_payload.get("created_at"))
    updated_at = _to_epoch_seconds(be_payload.get("updated_at"))

    be_meta = {
        "upload_id": be_payload.get("upload_id"),
        "bucket": be_payload.get("bucket"),
        "object_key": be_payload.get("object_key"),
        "download_url": be_payload.get("download_url"),
        "public_url": be_payload.get("public_url"),
        "presigned_get_url": be_payload.get("presigned_get_url"),
    }

    file_model = {
        "id": file_id,
        "user_id": user_id,
        "hash": be_payload.get("sha256"),
        "filename": filename,
        "data": {"status": "pending"},
        "meta": {
            "name": filename,
            "content_type": mime_type,
            "size": size_bytes,
            "data": {"be_upload": be_meta},
        },
        "created_at": created_at,
        "updated_at": updated_at,
    }
    return {"status": True, **file_model}


def _edge_store_file(file_payload: dict[str, Any], be_payload: dict[str, Any]) -> None:
    file_id = str(file_payload["id"])
    EDGE_FILE_STORE[file_id] = {
        "file": {k: v for k, v in file_payload.items() if k != "status"},
        "be": be_payload,
        "processing_status": "completed",
        "error": None,
        "updated_at": int(time.time()),
    }


def _edge_get_file_record(file_id: str) -> dict[str, Any] | None:
    return EDGE_FILE_STORE.get(file_id)


def _edge_extract_file_ids(value: Any) -> list[str]:
    file_ids: list[str] = []

    def _visit(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                _visit(item)
            return

        if not isinstance(node, dict):
            return

        direct_id = node.get("id")
        file_obj = node.get("file")
        node_type = node.get("type")

        if node_type in {"file", "image", "doc", "text", "note", "chat", "collection"}:
            if isinstance(direct_id, str) and direct_id:
                file_ids.append(direct_id)
            if isinstance(file_obj, dict):
                nested_id = file_obj.get("id")
                if isinstance(nested_id, str) and nested_id:
                    file_ids.append(nested_id)

        for child in node.values():
            _visit(child)

    _visit(value)
    return list(dict.fromkeys(file_ids))


def _edge_store_chat_files(
    chat_id: str,
    user_id: str,
    file_ids: list[str],
    *,
    source: str,
    message_id: str | None = None,
    prompt_text: str | None = None,
) -> None:
    if not chat_id:
        return

    existing = EDGE_CHAT_STORE.get(chat_id) or {}
    merged_file_ids = list(
        dict.fromkeys([*(existing.get("file_ids") or []), *file_ids])
    )
    EDGE_CHAT_STORE[chat_id] = {
        "chat_id": chat_id,
        "user_id": user_id or str(existing.get("user_id") or ""),
        "file_ids": merged_file_ids,
        "message_id": message_id or existing.get("message_id"),
        "prompt_text": prompt_text or str(existing.get("prompt_text") or ""),
        "source": source,
        "updated_at": int(time.time()),
    }


def _edge_get_chat_record(chat_id: str) -> dict[str, Any] | None:
    return EDGE_CHAT_STORE.get(chat_id)


def _edge_extract_last_user_text(value: Any) -> str | None:
    messages = None
    if isinstance(value, dict):
        messages = value.get("messages")
        if messages is None and isinstance(value.get("chat"), dict):
            messages = value["chat"].get("messages")
            if messages is None:
                history = value["chat"].get("history")
                if isinstance(history, dict):
                    messages = history.get("messages")
    elif isinstance(value, list):
        messages = value

    if isinstance(messages, dict):
        messages = list(messages.values())
    if not isinstance(messages, list):
        return None

    for message in reversed(messages):
        if not isinstance(message, dict):
            continue
        if message.get("role") != "user":
            continue

        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
        if isinstance(content, list):
            parts: list[str] = []
            for part in content:
                if isinstance(part, str) and part.strip():
                    parts.append(part.strip())
                elif isinstance(part, dict):
                    text = part.get("text")
                    if isinstance(text, str) and text.strip():
                        parts.append(text.strip())
            joined = "\n".join(parts).strip()
            if joined:
                return joined
    return None


def _edge_extract_chat_id(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None

    candidates: list[Any] = [
        value.get("chat_id"),
        value.get("id"),
    ]

    chat_value = value.get("chat")
    if isinstance(chat_value, dict):
        candidates.extend([chat_value.get("id"), chat_value.get("chat_id")])

    data_value = value.get("data")
    if isinstance(data_value, dict):
        candidates.extend([data_value.get("id"), data_value.get("chat_id")])

    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return None


def _edge_extract_chat_context(value: Any) -> tuple[str | None, list[str], str | None]:
    if not isinstance(value, dict):
        return None, [], None
    return (
        _edge_extract_chat_id(value),
        _edge_extract_file_ids(value),
        _edge_extract_last_user_text(value),
    )


def _edge_cleanup_pending_provider_contexts() -> None:
    now_ts = int(time.time())
    EDGE_PENDING_PROVIDER_CONTEXTS[:] = [
        item
        for item in EDGE_PENDING_PROVIDER_CONTEXTS
        if now_ts - int(item.get("created_at") or 0) <= EDGE_PENDING_PROVIDER_TTL_SECONDS
    ]


def _edge_store_pending_provider_context(
    *,
    chat_id: str,
    user_id: str,
    prompt_text: str | None,
    documents: list[dict[str, str]],
) -> None:
    normalized_prompt = " ".join((prompt_text or "").split()).strip()
    if not chat_id or not normalized_prompt or not documents:
        return

    _edge_cleanup_pending_provider_contexts()
    EDGE_PENDING_PROVIDER_CONTEXTS.append(
        {
            "chat_id": chat_id,
            "user_id": user_id,
            "prompt_text": normalized_prompt,
            "documents": documents,
            "created_at": int(time.time()),
        }
    )
    print(
        "EDGE provider.pending "
        f"chat_id={chat_id} "
        f"user={user_id} "
        f"prompt={_json_for_log(normalized_prompt)} "
        f"docs={_json_for_log(documents)}",
        flush=True,
    )


def _edge_pop_pending_provider_context(
    *,
    prompt_text: str | None,
) -> dict[str, Any] | None:
    normalized_prompt = " ".join((prompt_text or "").split()).strip()
    if not normalized_prompt:
        return None

    _edge_cleanup_pending_provider_contexts()
    for index in range(len(EDGE_PENDING_PROVIDER_CONTEXTS) - 1, -1, -1):
        record = EDGE_PENDING_PROVIDER_CONTEXTS[index]
        if str(record.get("prompt_text") or "") != normalized_prompt:
            continue
        return EDGE_PENDING_PROVIDER_CONTEXTS.pop(index)
    return None


def _edge_provider_chat_id(request: Request, payload: dict[str, Any]) -> str | None:
    header_chat_id = request.headers.get("x-openwebui-chat-id")
    if header_chat_id:
        return header_chat_id

    metadata = payload.get("metadata")
    if isinstance(metadata, dict):
        chat_id = metadata.get("chat_id")
        if isinstance(chat_id, str) and chat_id:
            return chat_id
    return None


def _edge_build_public_document_url(be_payload: dict[str, Any], links_payload: dict[str, Any]) -> str | None:
    # Prefer the stable public_url exactly as returned by BE/MinIO.
    # In the current deployment OPC can read localhost:9000 from its local runtime,
    # so we must not rewrite it to a public domain or prioritize presigned variants.
    for candidate in (
        links_payload.get("public_url"),
        be_payload.get("public_url"),
    ):
        if isinstance(candidate, str) and candidate:
            return candidate

    for candidate in (
        links_payload.get("presigned_get_url"),
        be_payload.get("presigned_get_url"),
    ):
        if isinstance(candidate, str) and candidate:
            return candidate

    download_url = links_payload.get("download_url") or be_payload.get("download_url")
    if isinstance(download_url, str) and download_url:
        if download_url.startswith(("http://", "https://")):
            return download_url
        return f"{backend_client.base_url}{download_url if download_url.startswith('/') else '/' + download_url}"

    return None


async def _edge_resolve_chat_documents(
    chat_id: str,
    user_id: str,
    headers: dict[str, str],
) -> list[dict[str, str]]:
    chat_record = _edge_get_chat_record(chat_id)
    if not chat_record:
        return []

    if chat_record.get("user_id") not in {None, "", user_id}:
        return []

    documents: list[dict[str, str]] = []
    for file_id in chat_record.get("file_ids", []):
        file_record = _edge_get_file_record(file_id)
        if not file_record:
            continue

        be_payload = file_record.get("be") or {}
        upload_id = str(be_payload.get("upload_id") or file_id)
        filename = str((file_record.get("file") or {}).get("filename") or be_payload.get("filename") or upload_id)

        links_payload: dict[str, Any] = {}
        try:
            links_response = await backend_client.get(
                path=f"/api/v1/uploads/{upload_id}/links",
                headers=headers,
            )
            if links_response.status_code < 400:
                links_payload = links_response.json()
            print(
                "EDGE upload.links "
                f"chat_id={chat_id} "
                f"upload_id={upload_id} "
                f"status={links_response.status_code} "
                f"json={_json_for_log(links_payload)}",
                flush=True,
            )
        except Exception:
            links_payload = {}
            print(
                "EDGE upload.links "
                f"chat_id={chat_id} "
                f"upload_id={upload_id} "
                "status=error json={}",
                flush=True,
            )

        public_url = _edge_build_public_document_url(be_payload, links_payload)
        if not public_url:
            print(
                "EDGE upload.links unresolved "
                f"chat_id={chat_id} "
                f"upload_id={upload_id} "
                f"be={_json_for_log(be_payload)} "
                f"links={_json_for_log(links_payload)}",
                flush=True,
            )
            continue

        print(
            "EDGE upload.links selected "
            f"chat_id={chat_id} "
            f"upload_id={upload_id} "
            f"url={public_url}",
            flush=True,
        )

        documents.append(
            {
                "file_id": file_id,
                "upload_id": upload_id,
                "filename": filename,
                "public_url": public_url,
            }
        )

    return documents


async def _edge_enrich_chat_messages_for_chat_id(
    *,
    chat_id: str,
    user_id: str,
    headers: dict[str, str],
    messages: list[dict[str, Any]],
    log_prefix: str,
) -> list[dict[str, str]]:
    documents = await _edge_resolve_chat_documents(
        chat_id=chat_id,
        user_id=user_id,
        headers=headers,
    )
    if documents:
        _edge_append_document_context(messages, documents)
        print(
            f"{log_prefix} chat_id={chat_id} docs={_json_for_log(documents)}",
            flush=True,
        )
    return documents


def _edge_append_document_context(
    messages: list[dict[str, Any]],
    documents: list[dict[str, str]],
) -> None:
    if not messages or not documents:
        return

    def workspace_target(doc: dict[str, str]) -> str:
        raw_url = str(doc.get("public_url") or "")
        parsed = urlparse(raw_url)
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) >= 4:
            return f"boxedai_downloads/{parts[1]}/{parts[2]}/{doc['filename']}"
        upload_id = str(doc.get("upload_id") or "unknown-upload")
        return f"boxedai_downloads/unknown-user/{upload_id}/{doc['filename']}"

    lines = [
        "Attached documents:",
        "Use these document URLs as the primary source for this request.",
        "Do not use web_fetch for these URLs. web_fetch is disabled for security reasons.",
        "If you need the file content, use curl or wget from your runtime.",
        "Download each file into your workspace under the exact target path shown below before inspecting it.",
        "Do not ask the user to upload the file again if a document URL is already present.",
    ]
    for doc in documents:
        lines.append(f"- {doc['filename']}: {doc['public_url']}")
        lines.append(f"  save_to: {workspace_target(doc)}")
    injection = "\n".join(lines)

    for message in reversed(messages):
        if message.get("role") != "user":
            continue

        content = message.get("content")
        if isinstance(content, str):
            if any(doc["public_url"] in content for doc in documents):
                return
            message["content"] = f"{content.rstrip()}\n\n{injection}".strip()
            return

        if isinstance(content, list):
            text_parts = [
                part.get("text", "")
                for part in content
                if isinstance(part, dict) and part.get("type") in {"text", "input_text"}
            ]
            joined = "\n".join([part for part in text_parts if part])
            if any(doc["public_url"] in joined for doc in documents):
                return
            content.append({"type": "text", "text": injection})
            message["content"] = content
            return


def _edge_log_chat_sync(
    *,
    source: str,
    chat_id: str,
    user_id: str,
    file_ids: list[str],
    prompt_text: str | None,
) -> None:
    print(
        f"{source} chat_id={chat_id} user={user_id} "
        f"files={_json_for_log(file_ids)} "
        f"prompt={_json_for_log(prompt_text or '')}",
        flush=True,
    )


async def _edge_passthrough_http_response(
    request: Request,
    *,
    full_path: str,
    body: bytes,
) -> Response:
    upstream = await _edge_passthrough_http_request(request, full_path=full_path, body=body)
    return _edge_response_from_upstream(upstream)


async def _edge_passthrough_http_request(
    request: Request,
    *,
    full_path: str,
    body: bytes,
) -> httpx.Response:
    target_url = _edge_target_url(request, full_path)
    forwarded_headers = _edge_passthrough_headers(request)

    try:
        async with httpx.AsyncClient(
            timeout=config.edge.timeout_seconds, follow_redirects=False
        ) as client:
            return await client.request(
                method=request.method,
                url=target_url,
                headers=forwarded_headers,
                content=body,
            )
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502,
            detail={"message": "Edge passthrough upstream error", "error": str(exc)},
        ) from exc


def _edge_response_from_upstream(upstream: httpx.Response) -> Response:
    response = Response(
        content=upstream.content,
        status_code=upstream.status_code,
    )

    for name, value in upstream.headers.items():
        lower = name.lower()
        if lower in HOP_BY_HOP_HEADERS:
            continue
        if lower in {"content-length", "set-cookie"}:
            continue
        response.headers[name] = value

    set_cookie_values: list[str] = []
    try:
        set_cookie_values = upstream.headers.get_list("set-cookie")
    except Exception:
        try:
            set_cookie_values = [
                value
                for key, value in upstream.headers.multi_items()
                if key.lower() == "set-cookie"
            ]
        except Exception:
            set_cookie_values = []

    for cookie_value in set_cookie_values:
        response.raw_headers.append((b"set-cookie", cookie_value.encode("latin-1")))

    return response


def _edge_parse_json_bytes(body: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(body.decode("utf-8"))
        if isinstance(payload, dict):
            return payload
    except Exception:
        pass
    return {}


def _edge_parse_upstream_json(upstream: httpx.Response) -> dict[str, Any]:
    try:
        payload = upstream.json()
        if isinstance(payload, dict):
            return payload
    except Exception:
        pass
    return {}


async def _edge_fetch_be_download(
    download_url: str, headers: dict[str, str]
) -> httpx.Response:
    if download_url.startswith("http://") or download_url.startswith("https://"):
        def _get_sync() -> httpx.Response:
            with httpx.Client(timeout=config.backend.timeout_seconds) as client:
                return client.get(download_url, headers=headers)

        return await asyncio.to_thread(_get_sync)

    normalized_path = download_url if download_url.startswith("/") else f"/{download_url}"
    return await backend_client.get(path=normalized_path, headers=headers)


async def _forward_openai_json_to_be(
    path: str,
    payload: Dict[str, Any],
    headers: dict[str, str],
    *,
    require_model: bool,
    allow_streaming: bool,
    error_message: str,
) -> Response:
    _normalize_openai_model(payload, require_model=require_model)
    streaming_requested = (
        allow_streaming
        and config.backend.streaming_enabled
        and bool(payload.get("stream"))
    )
    if not streaming_requested:
        payload["stream"] = False

    print(
        "proxy→be request "
        f"path={path} "
        f"stream={bool(payload.get('stream'))} "
        f"headers={_json_for_log(headers)} "
        f"messages={_messages_for_log(payload.get('messages'))}",
        flush=True,
    )

    if streaming_requested:
        try:
            client, be_response = await backend_client.stream_json(
                path=path,
                payload=payload,
                headers=headers,
            )
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=502,
                detail={"message": error_message, "error": str(exc)},
            ) from exc

        content_type = be_response.headers.get("content-type", "")
        if be_response.status_code >= 400 or "text/event-stream" not in content_type:
            raw = await be_response.aread()
            try:
                be_payload = json.loads(raw.decode("utf-8"))
            except Exception:
                be_payload = {"raw_response": raw.decode("utf-8", errors="replace")}
            await be_response.aclose()
            await client.aclose()
            return JSONResponse(status_code=be_response.status_code, content=be_payload)

        response_headers: dict[str, str] = {}
        for name, value in be_response.headers.items():
            lower = name.lower()
            if lower in HOP_BY_HOP_HEADERS or lower == "content-length":
                continue
            response_headers[name] = value

        async def stream_body() -> Any:
            try:
                async for chunk in be_response.aiter_bytes():
                    yield chunk
            finally:
                await be_response.aclose()
                await client.aclose()

        return StreamingResponse(
            stream_body(),
            status_code=be_response.status_code,
            headers=response_headers,
        )

    try:
        be_response = await backend_client.post_json(
            path=path,
            payload=payload,
            headers=headers,
        )
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502,
            detail={"message": error_message, "error": str(exc)},
        ) from exc

    try:
        be_payload = be_response.json()
    except Exception:
        be_payload = {"raw_response": be_response.text}

    print(
        "proxy→be response "
        f"path={path} "
        f"status={be_response.status_code} "
        f"json={_json_for_log(be_payload)}",
        flush=True,
    )

    return JSONResponse(status_code=be_response.status_code, content=be_payload)


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    payload = await request.json()
    body = _get_body(payload)
    headers = _bridge_headers_from_request(request)
    chat_id = _edge_provider_chat_id(request, body)
    user_id = headers.get("x-debug-user", "")

    # Inject session key only when not already set (e.g. by openclaw_session_bridge function).
    if chat_id and user_id and not body.get("user"):
        body["user"] = _session_key(user_id, chat_id)

    if chat_id:
        await _edge_enrich_chat_messages_for_chat_id(
            chat_id=chat_id,
            user_id=user_id,
            headers=headers,
            messages=body.setdefault("messages", []),
            log_prefix="proxy→be chat.documents",
        )
    else:
        provider_prompt = _edge_extract_last_user_text(body)
        pending_context = _edge_pop_pending_provider_context(prompt_text=provider_prompt)
        if pending_context:
            _edge_append_document_context(
                body.setdefault("messages", []),
                pending_context.get("documents") or [],
            )
            print(
                "proxy→be pending.documents "
                f"chat_id={pending_context.get('chat_id')} "
                f"prompt={_json_for_log(provider_prompt or '')} "
                f"docs={_json_for_log(pending_context.get('documents') or [])}",
                flush=True,
            )

    # Debug: remove if noisy
    print(
        "proxy→be chat.completions "
        f"model={body.get('model')} "
        f"chat_id={(chat_id or '')[:16]} "
        f"payload_user={(body.get('user') or '')[:12]} "
        f"hdr_user={(headers.get('x-debug-user') or '')[:12]}",
        flush=True,
    )

    return await _forward_openai_json_to_be(
        path="/v1/chat/completions",
        payload=body,
        headers=headers,
        require_model=True,
        allow_streaming=True,
        error_message="Failed forwarding chat completion to BE",
    )


@app.post("/chat/completions")
async def chat_completions_alias(request: Request):
    """Compatibility alias without /v1 prefix."""
    return await chat_completions(request)


@app.post("/v1/completions")
async def completions(request: Request):
    payload = await request.json()
    body = _get_body(payload)
    headers = _bridge_headers_from_request(request)

    print(
        "proxy→be completions "
        f"model={body.get('model')} "
        f"payload_user={(body.get('user') or '')[:12]} "
        f"hdr_user={(headers.get('x-debug-user') or '')[:12]}",
        flush=True,
    )

    return await _forward_openai_json_to_be(
        path="/v1/completions",
        payload=body,
        headers=headers,
        require_model=True,
        allow_streaming=True,
        error_message="Failed forwarding completion to BE",
    )


@app.post("/completions")
async def completions_alias(request: Request):
    """Compatibility alias without /v1 prefix."""
    return await completions(request)


@app.post("/v1/responses")
async def responses(request: Request):
    payload = await request.json()
    body = _get_body(payload)
    headers = _bridge_headers_from_request(request)

    print(
        "proxy→be responses "
        f"model={body.get('model')} "
        f"payload_user={(body.get('user') or '')[:12]} "
        f"hdr_user={(headers.get('x-debug-user') or '')[:12]}",
        flush=True,
    )
    _normalize_openai_model(body, require_model=False)
    body["stream"] = False

    try:
        be_response = await backend_client.post_json(
            path="/v1/responses",
            payload=body,
            headers=headers,
        )
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502,
            detail={"message": "Failed forwarding response request to BE", "error": str(exc)},
        ) from exc

    try:
        be_payload = be_response.json()
    except Exception:
        be_payload = {"raw_response": be_response.text}

    # Some upstream deployments return 404 Not Found for /v1/responses.
    # In that case, fallback to chat.completions and translate the output shape.
    if be_response.status_code == 404 and _is_upstream_not_found(be_payload):
        fallback_payload = _build_chat_fallback_payload_from_responses(body)
        _normalize_openai_model(fallback_payload, require_model=True)
        fallback_payload["stream"] = False

        try:
            chat_response = await backend_client.post_json(
                path="/v1/chat/completions",
                payload=fallback_payload,
                headers=headers,
            )
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=502,
                detail={
                    "message": "Failed forwarding /v1/responses fallback to chat.completions",
                    "error": str(exc),
                },
            ) from exc

        try:
            chat_payload = chat_response.json()
        except Exception:
            chat_payload = {"raw_response": chat_response.text}

        if chat_response.status_code >= 400 or not isinstance(chat_payload, dict):
            return JSONResponse(status_code=chat_response.status_code, content=chat_payload)

        translated = _chat_completion_to_responses_shape(fallback_payload, chat_payload)
        translated["fallback"] = {
            "active": True,
            "reason": "upstream_/v1/responses_not_available",
        }
        return JSONResponse(status_code=200, content=translated)

    return JSONResponse(status_code=be_response.status_code, content=be_payload)


@app.post("/responses")
async def responses_alias(request: Request):
    """Compatibility alias without /v1 prefix."""
    return await responses(request)


def _bridge_headers_from_request(request: Request) -> dict[str, str]:
    headers: dict[str, str] = {}

    authorization = request.headers.get("authorization")
    if authorization:
        headers["authorization"] = authorization

    x_debug_user = request.headers.get("x-debug-user")
    if not x_debug_user:
        x_debug_user = request.headers.get("x-openwebui-user-id")
    if not x_debug_user:
        x_debug_user = _try_decode_unverified_jwt_user_id(authorization)
    if x_debug_user:
        headers["x-debug-user"] = x_debug_user

    return headers


def _edge_target_url(request: Request, full_path: str) -> str:
    box_base_url = config.edge.box_base_url
    if box_base_url is None:
        raise HTTPException(
            status_code=503, detail="Edge passthrough enabled but edge.box_base_url is not configured"
        )

    base = str(box_base_url).rstrip("/")
    if full_path:
        target_url = f"{base}/{full_path.lstrip('/')}"
    else:
        target_url = f"{base}/"

    query = str(request.url.query or "")
    if query:
        target_url = f"{target_url}?{query}"
    return target_url


def _edge_passthrough_headers(request: Request) -> dict[str, str]:
    headers: dict[str, str] = {}
    for name, value in request.headers.items():
        lower = name.lower()
        if lower in HOP_BY_HOP_HEADERS:
            continue
        if lower == "content-length":
            continue
        headers[name] = value

    host = request.headers.get("host", "")
    if host:
        headers["host"] = host
        headers.setdefault("x-forwarded-host", host)

    headers.setdefault(
        "x-forwarded-proto",
        request.headers.get("x-forwarded-proto") or request.url.scheme or "https",
    )

    forwarded_port = request.headers.get("x-forwarded-port")
    if not forwarded_port:
        if request.url.port:
            forwarded_port = str(request.url.port)
        elif headers["x-forwarded-proto"] == "https":
            forwarded_port = "443"
        else:
            forwarded_port = "80"
    headers["x-forwarded-port"] = forwarded_port

    return headers


def _edge_response_headers(source_headers: httpx.Headers) -> dict[str, str]:
    headers: dict[str, str] = {}
    for name, value in source_headers.items():
        lower = name.lower()
        if lower in HOP_BY_HOP_HEADERS:
            continue
        if lower == "content-length":
            continue
        headers[name] = value
    return headers


def _edge_ws_target_url(websocket: WebSocket, full_path: str) -> str:
    box_base_url = config.edge.box_base_url
    if box_base_url is None:
        raise HTTPException(
            status_code=503, detail="Edge passthrough enabled but edge.box_base_url is not configured"
        )

    raw_base = str(box_base_url).rstrip("/")
    if raw_base.startswith("https://"):
        ws_base = "wss://" + raw_base[len("https://") :]
    elif raw_base.startswith("http://"):
        ws_base = "ws://" + raw_base[len("http://") :]
    else:
        ws_base = raw_base

    if full_path:
        target_url = f"{ws_base}/ws/socket.io/{full_path.lstrip('/')}"
    else:
        target_url = f"{ws_base}/ws/socket.io/"

    query = str(websocket.url.query or "")
    if query:
        target_url = f"{target_url}?{query}"
    return target_url


def _edge_ws_passthrough_headers(websocket: WebSocket) -> dict[str, str]:
    headers: dict[str, str] = {}
    for name, value in websocket.headers.items():
        lower = name.lower()
        if lower in HOP_BY_HOP_HEADERS:
            continue
        if lower in {
            "host",
            "upgrade",
            "connection",
            "sec-websocket-key",
            "sec-websocket-version",
            "sec-websocket-extensions",
            "sec-websocket-protocol",
        }:
            continue
        headers[name] = value
    return headers


async def _edge_open_upstream_ws(
    target_url: str,
    headers: dict[str, str],
    timeout_seconds: float,
):
    connect_kwargs = {
        "open_timeout": timeout_seconds,
        "close_timeout": 5,
    }

    # Compatibility across websockets versions:
    # - legacy: extra_headers
    # - newer: additional_headers
    attempts = ("extra_headers", "additional_headers")
    last_exc: Exception | None = None

    for header_kw in attempts:
        try:
            return await websockets.connect(
                target_url,
                **connect_kwargs,
                **{header_kw: headers},
            )
        except TypeError as exc:
            last_exc = exc
            if "unexpected keyword argument" in str(exc):
                continue
            raise

    if last_exc is not None:
        raise last_exc
    raise RuntimeError("Failed to open upstream websocket")


# -------------------------
# Edge Gateway compatibility endpoints for Open WebUI files API
# -------------------------
@app.post("/api/v1/files")
@app.post("/api/v1/files/")
async def edge_upload_file(request: Request):
    content_type = request.headers.get("content-type", "")
    if not content_type.startswith("multipart/form-data"):
        raise HTTPException(status_code=400, detail="Upload requires multipart/form-data")

    body = await request.body()
    if not body:
        raise HTTPException(status_code=400, detail="Empty multipart body")

    user_id = _edge_user_id_from_request(request)
    headers = _edge_backend_headers_from_request(request, user_id=user_id)

    try:
        be_response = await backend_client.upload_multipart_raw(
            body=body,
            content_type=content_type,
            headers=headers,
        )
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502,
            detail={"message": "Failed forwarding edge upload to BE", "error": str(exc)},
        ) from exc

    try:
        be_payload = be_response.json()
    except Exception:
        be_payload = {"raw_response": be_response.text}

    log.info(
        "EDGE upload -> BE status=%s user=%s be_json=%s",
        be_response.status_code,
        user_id,
        _json_for_log(be_payload),
    )
    print(
        f"EDGE upload -> BE status={be_response.status_code} user={user_id} be_json={_json_for_log(be_payload)}",
        flush=True,
    )

    if be_response.status_code >= 400 or not isinstance(be_payload, dict):
        return JSONResponse(status_code=be_response.status_code, content=be_payload)

    adapted_payload = _adapt_be_upload_to_box_shape(be_payload, user_id=user_id)
    _edge_store_file(adapted_payload, be_payload=be_payload)
    log.info("EDGE upload -> Box shape json=%s", _json_for_log(adapted_payload))
    print(
        f"EDGE upload -> Box shape json={_json_for_log(adapted_payload)}",
        flush=True,
    )
    return JSONResponse(status_code=200, content=adapted_payload)


@app.get("/api/v1/files/{file_id}")
async def edge_get_file(file_id: str):
    record = _edge_get_file_record(file_id)
    if not record:
        raise HTTPException(status_code=404, detail="Not Found")
    return JSONResponse(status_code=200, content=record["file"])


@app.get("/api/v1/files/{file_id}/process/status")
async def edge_get_file_process_status(file_id: str, stream: bool = False):
    record = _edge_get_file_record(file_id)
    if not record:
        raise HTTPException(status_code=404, detail="Not Found")

    status_value = str(record.get("processing_status") or "completed")
    error_value = record.get("error")

    if stream:
        async def event_stream() -> Any:
            payload: dict[str, Any] = {"status": status_value}
            if status_value == "failed" and error_value:
                payload["error"] = error_value
            yield f"data: {json.dumps(payload)}\n\n"

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    return {"status": status_value}


@app.get("/api/v1/files/{file_id}/content")
async def edge_get_file_content(file_id: str, request: Request):
    record = _edge_get_file_record(file_id)
    if not record:
        raise HTTPException(status_code=404, detail="Not Found")

    be_payload = record.get("be", {})
    download_url = str(be_payload.get("download_url") or f"/api/v1/uploads/{file_id}/download")
    headers = _edge_backend_headers_from_request(request, user_id=str(record["file"]["user_id"]))

    try:
        upstream = await _edge_fetch_be_download(download_url=download_url, headers=headers)
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502,
            detail={"message": "Failed fetching file content from BE", "error": str(exc)},
        ) from exc

    media_type = upstream.headers.get("content-type", "application/octet-stream")
    response_headers: dict[str, str] = {}
    content_disposition = upstream.headers.get("content-disposition")
    if content_disposition:
        response_headers["content-disposition"] = content_disposition

    return Response(
        content=upstream.content,
        media_type=media_type,
        status_code=upstream.status_code,
        headers=response_headers,
    )


@app.get("/api/v1/files/{file_id}/content/html")
async def edge_get_file_content_html(file_id: str, request: Request):
    return await edge_get_file_content(file_id, request)


@app.post("/api/chat/completions")
@app.post("/api/v1/chat/completions")
async def edge_box_chat_completions(request: Request):
    body_bytes = await request.body()
    user_id = _edge_user_id_from_request(request)
    payload = _edge_parse_json_bytes(body_bytes)

    print(
        "EDGE browser chat.request "
        f"path={request.url.path} "
        f"user={user_id} "
        f"messages={_messages_for_log(payload.get('messages'))}",
        flush=True,
    )

    chat_id, file_ids, prompt_text = _edge_extract_chat_context(payload)
    message_id = payload.get("id")
    if isinstance(chat_id, str) and chat_id:
        _edge_store_chat_files(
            chat_id=chat_id,
            user_id=user_id,
            file_ids=file_ids,
            source="browser_chat_completions",
            message_id=message_id if isinstance(message_id, str) else None,
            prompt_text=prompt_text,
        )
        _edge_log_chat_sync(
            source="EDGE browser chat sync",
            chat_id=chat_id,
            user_id=user_id,
            file_ids=file_ids,
            prompt_text=prompt_text,
        )

        headers = _edge_backend_headers_from_request(request, user_id=user_id)
        await _edge_enrich_chat_messages_for_chat_id(
            chat_id=chat_id,
            user_id=user_id,
            headers=headers,
            messages=payload.setdefault("messages", []),
            log_prefix="EDGE browser chat.documents",
        )
        injected_documents = await _edge_resolve_chat_documents(
            chat_id=chat_id,
            user_id=user_id,
            headers=headers,
        )
        _edge_store_pending_provider_context(
            chat_id=chat_id,
            user_id=user_id,
            prompt_text=prompt_text,
            documents=injected_documents,
        )
        print(
            "EDGE browser chat.forward "
            f"path={request.url.path} "
            f"chat_id={chat_id} "
            f"messages={_messages_for_log(payload.get('messages'))}",
            flush=True,
        )
        body_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    return await _edge_passthrough_http_response(
        request,
        full_path="api/chat/completions" if request.url.path.endswith("/api/chat/completions") else "api/v1/chat/completions",
        body=body_bytes,
    )


@app.post("/api/v1/chats/new")
async def edge_box_chats_new(request: Request):
    body_bytes = await request.body()
    user_id = _edge_user_id_from_request(request)
    request_payload = _edge_parse_json_bytes(body_bytes)
    request_chat_id, request_file_ids, request_prompt = _edge_extract_chat_context(request_payload)

    upstream = await _edge_passthrough_http_request(
        request,
        full_path="api/v1/chats/new",
        body=body_bytes,
    )
    response_payload = _edge_parse_upstream_json(upstream)
    response_chat_id, response_file_ids, response_prompt = _edge_extract_chat_context(response_payload)

    resolved_chat_id = response_chat_id or request_chat_id
    resolved_file_ids = list(dict.fromkeys([*request_file_ids, *response_file_ids]))
    resolved_prompt = response_prompt or request_prompt
    if resolved_chat_id:
        _edge_store_chat_files(
            chat_id=resolved_chat_id,
            user_id=user_id,
            file_ids=resolved_file_ids,
            source="chats_new",
            prompt_text=resolved_prompt,
        )
        _edge_log_chat_sync(
            source="EDGE chats.new sync",
            chat_id=resolved_chat_id,
            user_id=user_id,
            file_ids=resolved_file_ids,
            prompt_text=resolved_prompt,
        )

    return _edge_response_from_upstream(upstream)


@app.api_route(
    "/api/v1/chats/{chat_id}",
    methods=["POST", "PUT", "PATCH"],
)
async def edge_box_chats_update(chat_id: str, request: Request):
    body_bytes = await request.body()
    user_id = _edge_user_id_from_request(request)
    request_payload = _edge_parse_json_bytes(body_bytes)
    _, request_file_ids, request_prompt = _edge_extract_chat_context(request_payload)

    upstream = await _edge_passthrough_http_request(
        request,
        full_path=f"api/v1/chats/{chat_id}",
        body=body_bytes,
    )
    response_payload = _edge_parse_upstream_json(upstream)
    _, response_file_ids, response_prompt = _edge_extract_chat_context(response_payload)

    resolved_file_ids = list(dict.fromkeys([*request_file_ids, *response_file_ids]))
    resolved_prompt = response_prompt or request_prompt
    _edge_store_chat_files(
        chat_id=chat_id,
        user_id=user_id,
        file_ids=resolved_file_ids,
        source="chats_update",
        prompt_text=resolved_prompt,
    )
    _edge_log_chat_sync(
        source="EDGE chats.update sync",
        chat_id=chat_id,
        user_id=user_id,
        file_ids=resolved_file_ids,
        prompt_text=resolved_prompt,
    )

    return _edge_response_from_upstream(upstream)


@app.post("/v1/uploads/bridge")
async def uploads_bridge_v1(request: Request):
    content_type = request.headers.get("content-type", "")
    if not content_type.startswith("multipart/form-data"):
        raise HTTPException(
            status_code=400, detail="Upload bridge requires multipart/form-data"
        )

    body = await request.body()
    if not body:
        raise HTTPException(status_code=400, detail="Empty multipart body")

    bridge_upload_id = uuid.uuid4().hex
    headers = _bridge_headers_from_request(request)

    try:
        be_response = await backend_client.upload_multipart_raw(
            body=body, content_type=content_type, headers=headers
        )
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502,
            detail={
                "message": "Failed forwarding upload to BE",
                "bridge_upload_id": bridge_upload_id,
                "error": str(exc),
            },
        ) from exc

    try:
        be_payload = be_response.json()
    except Exception:
        be_payload = {"raw_response": be_response.text}

    log.info(
        "UPLOAD bridge -> BE status=%s bridge_upload_id=%s be_json=%s",
        be_response.status_code,
        bridge_upload_id,
        _json_for_log(be_payload),
    )
    print(
        f"UPLOAD bridge -> BE status={be_response.status_code} bridge_upload_id={bridge_upload_id} be_json={_json_for_log(be_payload)}",
        flush=True,
    )

    if isinstance(be_payload, dict):
        response_payload: Dict[str, Any] = {
            "bridge_upload_id": bridge_upload_id,
            **be_payload,
        }
    else:
        response_payload = {
            "bridge_upload_id": bridge_upload_id,
            "be_payload": be_payload,
        }

    log.info("UPLOAD bridge -> response json=%s", _json_for_log(response_payload))
    print(
        f"UPLOAD bridge -> response json={_json_for_log(response_payload)}",
        flush=True,
    )

    return JSONResponse(status_code=be_response.status_code, content=response_payload)


@app.post("/uploads/bridge")
async def uploads_bridge(request: Request):
    """Compatibility alias without /v1 prefix."""
    return await uploads_bridge_v1(request)


# -------------------------
# Open WebUI Pipelines endpoints (NO /v1)
# -------------------------
@app.get("/pipelines")
async def pipelines() -> Dict[str, Any]:
    return {"data": [_serialize_pipeline()]}


@app.post("/pipelines/add")
async def pipelines_add():
    raise HTTPException(
        status_code=405, detail="Remote pipeline download not supported")


@app.post("/pipelines/upload")
async def pipelines_upload(request: Request):
    if not request.headers.get("content-type", "").startswith("multipart/form-data"):
        raise HTTPException(
            status_code=400, detail="Upload requires multipart/form-data")

    upload_dir = Path(config.pipeline.__dict__.get(
        "upload_dir", "pipelines-uploaded"))
    upload_dir.mkdir(parents=True, exist_ok=True)

    form = await request.form()
    file = form.get("file")
    if file is None:
        raise HTTPException(
            status_code=400, detail="Missing file in form data")

    filename = Path(file.filename or "pipeline.py").name
    target_path = (upload_dir / filename).resolve()

    if not target_path.suffix.endswith(".py"):
        raise HTTPException(
            status_code=400, detail="Only .py files are allowed")

    with target_path.open("wb") as dest:
        dest.write(await file.read())

    return {"data": {"id": config.pipeline.id, "filename": filename, "path": str(target_path)}}


def _ensure_pipeline(pipeline_id: str) -> None:
    if pipeline_id != config.pipeline.id:
        raise HTTPException(status_code=404, detail="Pipeline not found")


@app.post("/{pipeline_id}/filter/inlet")
async def pipeline_inlet(pipeline_id: str, request: Request):
    _ensure_pipeline(pipeline_id)

    payload = await request.json()
    body = _get_body(payload)

    chat_id = _get_chat_id(payload, body)
    if config.pipeline.enforce_user and chat_id:
        body["user"] = _session_key(_get_user_id(payload), chat_id)

    enforce_prefix = config.pipeline.enforce_prefix
    if enforce_prefix:
        model_id = body.get("model")
        if isinstance(model_id, str) and not model_id.startswith(enforce_prefix):
            body["model"] = f"{enforce_prefix}{model_id}"

    return body


@app.post("/{pipeline_id}/filter/outlet")
async def pipeline_outlet(pipeline_id: str, request: Request):
    _ensure_pipeline(pipeline_id)
    payload = await request.json()
    return payload.get("body", payload)


@app.get("/{pipeline_id}/valves")
async def pipeline_valves(pipeline_id: str):
    _ensure_pipeline(pipeline_id)
    cfg = _load_valves_config()
    values = cfg.get("values")
    if not isinstance(values, dict):
        values = {}
    return values


@app.get("/{pipeline_id}/valves/spec")
async def pipeline_valves_spec(pipeline_id: str):
    _ensure_pipeline(pipeline_id)
    cfg = _load_valves_config()
    spec = cfg.get("schema")
    if not isinstance(spec, dict) or not spec:
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
    return spec


@app.post("/{pipeline_id}/valves/update")
async def pipeline_valves_update(pipeline_id: str, request: Request):
    _ensure_pipeline(pipeline_id)

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


# -------------------------
# /v1 aliases for Pipelines (Open WebUI expects these when Base URL ends with /v1)
# -------------------------
@app.get("/v1/pipelines")
async def pipelines_v1() -> Dict[str, Any]:
    return await pipelines()


@app.post("/v1/pipelines/add")
async def pipelines_add_v1():
    return await pipelines_add()


@app.post("/v1/pipelines/upload")
async def pipelines_upload_v1(request: Request):
    return await pipelines_upload(request)


@app.post("/v1/{pipeline_id}/filter/inlet")
async def pipeline_inlet_v1(pipeline_id: str, request: Request):
    return await pipeline_inlet(pipeline_id, request)


@app.post("/v1/{pipeline_id}/filter/outlet")
async def pipeline_outlet_v1(pipeline_id: str, request: Request):
    return await pipeline_outlet(pipeline_id, request)


@app.get("/v1/{pipeline_id}/valves")
async def pipeline_valves_v1(pipeline_id: str):
    return await pipeline_valves(pipeline_id)


@app.get("/v1/{pipeline_id}/valves/spec")
async def pipeline_valves_spec_v1(pipeline_id: str):
    return await pipeline_valves_spec(pipeline_id)


@app.post("/v1/{pipeline_id}/valves/update")
async def pipeline_valves_update_v1(pipeline_id: str, request: Request):
    return await pipeline_valves_update(pipeline_id, request)


# -------------------------
# Edge websocket passthrough for Open WebUI socket.io
# -------------------------
@app.websocket("/ws/socket.io/")
@app.websocket("/ws/socket.io/{full_path:path}")
async def edge_socketio_ws(websocket: WebSocket, full_path: str = ""):
    if not config.edge.enabled:
        await websocket.close(code=1008, reason="Edge passthrough disabled")
        return

    target_url = _edge_ws_target_url(websocket, full_path)
    upstream_headers = _edge_ws_passthrough_headers(websocket)
    log.info("Edge WS connect %s -> %s", websocket.url.path, target_url)

    await websocket.accept()
    upstream = None

    try:
        upstream = await _edge_open_upstream_ws(
            target_url=target_url,
            headers=upstream_headers,
            timeout_seconds=config.edge.timeout_seconds,
        )

        async def client_to_upstream() -> None:
            while True:
                message = await websocket.receive()
                msg_type = message.get("type")
                if msg_type == "websocket.disconnect":
                    break
                text = message.get("text")
                if text is not None:
                    await upstream.send(text)
                    continue
                data = message.get("bytes")
                if data is not None:
                    await upstream.send(data)

        async def upstream_to_client() -> None:
            while True:
                data = await upstream.recv()
                if isinstance(data, bytes):
                    await websocket.send_bytes(data)
                else:
                    await websocket.send_text(data)

        client_task = asyncio.create_task(client_to_upstream())
        upstream_task = asyncio.create_task(upstream_to_client())

        done, pending = await asyncio.wait(
            {client_task, upstream_task},
            return_when=asyncio.FIRST_COMPLETED,
        )

        for task in pending:
            task.cancel()
        for task in done:
            task.result()

    except WebSocketDisconnect:
        return
    except Exception as exc:
        log.warning("Edge WS passthrough failed: %s", exc)
        try:
            await websocket.close(code=1011, reason="Edge WS upstream error")
        except Exception:
            pass
    finally:
        if upstream is not None:
            try:
                await upstream.close()
            except Exception:
                pass


# -------------------------
# Edge catch-all passthrough (unmatched routes -> Open WebUI backend)
# -------------------------
@app.api_route(
    "/",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
)
@app.api_route(
    "/{full_path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
)
async def edge_passthrough(request: Request, full_path: str = ""):
    if not config.edge.enabled:
        raise HTTPException(status_code=404, detail="Route not found")

    body = await request.body()
    upstream = await _edge_passthrough_http_request(
        request,
        full_path=full_path,
        body=body,
    )
    return _edge_response_from_upstream(upstream)
