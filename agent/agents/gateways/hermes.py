import json
import urllib.error
import urllib.request

from agents.llm_settings import get_llm_settings

from .base import (
    AgentEnvironmentCheck,
    AgentEnvironmentReport,
    AgentGatewayError,
    AgentRequest,
    AgentResponse,
)


class HermesGateway:
    gateway_type = 'hermes'

    def __init__(self, llm_settings=None):
        self.llm_settings = llm_settings or get_llm_settings()

    def execute(self, request: AgentRequest):
        if not self.llm_settings.hermes_configured:
            raise AgentGatewayError('Hermes gateway is not configured. Set HERMES_GATEWAY_URL and HERMES_API_KEY.')

        payload = {
            'taskId': request.task_id,
            'taskType': request.task_type,
            'prompt': request.prompt,
            'input': request.input_payload,
            'context': request.context_payload,
            'expectedSchema': request.expected_schema,
            'safetyPolicy': request.safety_policy,
            'versions': {
                'agentProtocol': request.agent_protocol_version,
                'inputSchema': request.input_schema_version,
                'contextSchema': request.context_schema_version,
                'outputSchema': request.output_schema_version,
            },
            'idempotencyKey': request.idempotency_key,
            'model': self.llm_settings.hermes_model,
        }
        data = json.dumps(payload).encode('utf-8')
        http_request = urllib.request.Request(
            self.llm_settings.hermes_gateway_url,
            data=data,
            method='POST',
            headers={
                'Authorization': f'Bearer {self.llm_settings.hermes_api_key}',
                'Content-Type': 'application/json',
                'X-MigrationOps-Task-Id': request.task_id,
                'X-MigrationOps-Agent-Protocol': request.agent_protocol_version,
            },
        )

        try:
            with urllib.request.urlopen(http_request, timeout=self.llm_settings.request_timeout_seconds) as response:
                raw_body = response.read().decode('utf-8')
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode('utf-8', errors='replace')
            raise AgentGatewayError(f'Hermes gateway HTTP {exc.code}: {error_body}') from exc
        except urllib.error.URLError as exc:
            raise AgentGatewayError(f'Hermes gateway request failed: {exc.reason}') from exc

        try:
            parsed = json.loads(raw_body)
        except json.JSONDecodeError as exc:
            raise AgentGatewayError('Hermes gateway returned invalid JSON.') from exc

        output_payload = parsed.get('output') or parsed.get('output_payload') or parsed.get('result') or {}
        if not isinstance(output_payload, dict):
            raise AgentGatewayError('Hermes gateway output must be a JSON object.')

        return AgentResponse(
            status=parsed.get('status', 'succeeded'),
            output_payload=output_payload,
            summary=parsed.get('summary', output_payload.get('summary', '')),
            provider=parsed.get('provider', 'hermes'),
            model=parsed.get('model', self.llm_settings.hermes_model),
            usage=parsed.get('usage') if isinstance(parsed.get('usage'), dict) else {},
            events=parsed.get('events') if isinstance(parsed.get('events'), list) else [],
            error_message=parsed.get('error') or parsed.get('error_message') or '',
        )

    def test_environment(self):
        checks = []
        if self.llm_settings.hermes_gateway_url:
            checks.append(
                AgentEnvironmentCheck(
                    code='hermes_gateway_url_configured',
                    level='info',
                    message='Hermes gateway URL is configured.',
                )
            )
        else:
            checks.append(
                AgentEnvironmentCheck(
                    code='hermes_gateway_url_missing',
                    level='error',
                    message='Hermes gateway URL is missing.',
                    hint='Set HERMES_GATEWAY_URL in the backend .env file.',
                )
            )

        if self.llm_settings.hermes_api_key:
            checks.append(
                AgentEnvironmentCheck(
                    code='hermes_api_key_configured',
                    level='info',
                    message='Hermes API key is configured.',
                )
            )
        else:
            checks.append(
                AgentEnvironmentCheck(
                    code='hermes_api_key_missing',
                    level='error',
                    message='Hermes API key is missing.',
                    hint='Set HERMES_API_KEY in the backend .env file.',
                )
            )

        status = 'fail' if any(check.level == 'error' for check in checks) else 'pass'
        return AgentEnvironmentReport(gateway_type=self.gateway_type, status=status, checks=checks)
