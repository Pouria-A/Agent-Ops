from agents.llm_settings import get_llm_settings

from .hermes import HermesGateway
from .mock import MockAgentGateway


def get_agent_gateway(gateway_type=None):
    selected_gateway = (gateway_type or get_llm_settings().gateway or 'mock').strip().lower()
    if selected_gateway == 'mock':
        return MockAgentGateway()
    if selected_gateway == 'hermes':
        return HermesGateway()
    raise ValueError(f'Unsupported AI gateway: {selected_gateway}')
