from dataclasses import dataclass

from django.conf import settings


@dataclass(frozen=True)
class LlmSettings:
    gateway: str
    hermes_gateway_url: str
    hermes_api_key: str
    hermes_model: str
    request_timeout_seconds: int

    @property
    def hermes_configured(self):
        return bool(self.hermes_gateway_url and self.hermes_api_key)


def get_llm_settings():
    return LlmSettings(
        gateway=getattr(settings, 'AI_GATEWAY', 'mock'),
        hermes_gateway_url=getattr(settings, 'HERMES_GATEWAY_URL', ''),
        hermes_api_key=getattr(settings, 'HERMES_API_KEY', ''),
        hermes_model=getattr(settings, 'HERMES_MODEL', ''),
        request_timeout_seconds=getattr(settings, 'AI_REQUEST_TIMEOUT_SECONDS', 30),
    )
