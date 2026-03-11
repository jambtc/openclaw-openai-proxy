from __future__ import annotations

import os
from pathlib import Path
from typing import List, Optional

import yaml
from pydantic import AnyHttpUrl, BaseModel, Field, validator


class AgentConfig(BaseModel):
    """Static mapping between exposed model IDs and OpenClaw agents."""

    id: str = Field(..., description="Public model id returned to Open WebUI")
    name: Optional[str] = Field(
        default=None, description="Human readable name surfaced in Open WebUI"
    )
    agent_id: str = Field(..., description="Underlying OpenClaw agent id")
    description: Optional[str] = None
    tags: List[str] = Field(default_factory=list)
    profile_image_url: Optional[str] = Field(default=None)
    is_default: bool = Field(
        default=False,
        description=(
            "If true, this model will be suggested as the default selection in docs"
        ),
    )

    @property
    def openai_model_id(self) -> str:
        return self.id


class PipelineConfig(BaseModel):
    id: str = Field(default="openclaw-session-filter")
    name: str = Field(default="OpenClaw session filter")
    description: str = Field(
        default=(
            "Injects Open WebUI chat_id into the OpenAI 'user' field to stabilize"
            " OpenClaw sessions and normalises model identifiers."
        )
    )
    pipelines: List[str] = Field(
        default_factory=lambda: ["*"],
        description="Target models for the filter. `*` matches every model.",
    )
    priority: int = 500
    enforce_user: bool = True
    enforce_prefix: Optional[str] = None
    valves_config: Optional[str] = Field(
        default=None,
        description="Path to JSON file that defines valves/spec metadata.",
    )


class GatewayConfig(BaseModel):
    base_url: AnyHttpUrl = Field(
        default="http://127.0.0.1:18789", description="OpenClaw Gateway URL"
    )
    token: str = Field(..., description="Gateway bearer token")


class BackendConfig(BaseModel):
    base_url: AnyHttpUrl = Field(
        default="http://127.0.0.1:8000", description="OpenClaw BFF URL"
    )
    timeout_seconds: float = Field(
        default=120, gt=0, le=600, description="Timeout for proxy->BFF calls"
    )


class AppConfig(BaseModel):
    gateway: GatewayConfig
    backend: BackendConfig = Field(default_factory=BackendConfig)
    agents: List[AgentConfig]
    pipeline: PipelineConfig = Field(default_factory=PipelineConfig)

    @validator("agents")
    def ensure_unique_ids(cls, value: List[AgentConfig]) -> List[AgentConfig]:
        ids = {agent.id for agent in value}
        if len(ids) != len(value):
            raise ValueError("Agent ids must be unique")
        return value


def _expand_env(value: str) -> str:
    if value.startswith("${") and value.endswith("}"):
        env_key = value[2:-1]
        resolved = os.environ.get(env_key)
        if not resolved:
            raise ValueError(
                f"Environment variable '{env_key}' referenced in config but not set"
            )
        return resolved
    return value


def load_config(config_path: Path) -> AppConfig:
    """Load configuration from a YAML file."""

    data = yaml.safe_load(config_path.read_text())
    if data is None:
        raise ValueError(f"Config file {config_path} is empty")

    gateway = data.get("gateway", {})
    if "token" in gateway and isinstance(gateway["token"], str):
        gateway["token"] = _expand_env(gateway["token"])
    data["gateway"] = gateway

    return AppConfig.model_validate(data)
