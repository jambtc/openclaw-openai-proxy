from __future__ import annotations

import asyncio
from typing import AsyncIterator, Dict, Optional

import httpx

from .config import AgentConfig, AppConfig


class GatewayClient:
    """HTTP client that forwards OpenAI Chat Completions traffic to OpenClaw."""

    def __init__(self, config: AppConfig):
        self._config = config
        self._client = httpx.AsyncClient(timeout=60)

    @property
    def base_url(self) -> str:
        return str(self._config.gateway.base_url).rstrip("/")

    @property
    def token(self) -> str:
        return self._config.gateway.token

    def resolve_agent(self, model_id: str) -> AgentConfig:
        for agent in self._config.agents:
            if agent.id == model_id:
                return agent
        raise httpx.HTTPError(f"Unknown model id '{model_id}'")

    async def chat_completions(self, payload: Dict, stream: bool) -> httpx.Response | AsyncIterator[bytes]:
        headers = {"Authorization": f"Bearer {self.token}"}
        response = await self._client.post(
            f"{self.base_url}/v1/chat/completions",
            headers=headers,
            json=payload,
            timeout=None if stream else 60,
        )
        response.raise_for_status()

        if stream:
            async def iter_sse() -> AsyncIterator[bytes]:
                async for chunk in response.aiter_raw():
                    yield chunk

            return iter_sse()
        return response

    async def close(self) -> None:
        await self._client.aclose()
