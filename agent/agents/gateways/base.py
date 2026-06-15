from dataclasses import dataclass, field
from typing import Protocol

from core.versioning import (
    AGENT_CONTEXT_SCHEMA_VERSION,
    AGENT_INPUT_SCHEMA_VERSION,
    AGENT_OUTPUT_SCHEMA_VERSION,
    AGENT_PROTOCOL_VERSION,
)


@dataclass(frozen=True)
class AgentRequest:
    task_id: str
    task_type: str
    prompt: str
    input_payload: dict
    context_payload: dict = field(default_factory=dict)
    expected_schema: dict = field(default_factory=dict)
    safety_policy: dict = field(default_factory=dict)
    agent_protocol_version: str = AGENT_PROTOCOL_VERSION
    input_schema_version: str = AGENT_INPUT_SCHEMA_VERSION
    context_schema_version: str = AGENT_CONTEXT_SCHEMA_VERSION
    output_schema_version: str = AGENT_OUTPUT_SCHEMA_VERSION
    idempotency_key: str = ''


@dataclass(frozen=True)
class AgentResponse:
    status: str
    output_payload: dict
    summary: str = ''
    provider: str = ''
    model: str = ''
    usage: dict = field(default_factory=dict)
    events: list[dict] = field(default_factory=list)
    error_message: str = ''


@dataclass(frozen=True)
class AgentEnvironmentCheck:
    code: str
    level: str
    message: str
    hint: str = ''


@dataclass(frozen=True)
class AgentEnvironmentReport:
    gateway_type: str
    status: str
    checks: list[AgentEnvironmentCheck]


class AgentGatewayError(ValueError):
    pass


class AgentGateway(Protocol):
    gateway_type: str

    def execute(self, request: AgentRequest) -> AgentResponse:
        ...

    def test_environment(self) -> AgentEnvironmentReport:
        ...
