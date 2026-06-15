API_VERSION = 'v1'
API_VERSION_STATUS = 'stable'
APPLICATION_VERSION = '0.1.0'
WEBSOCKET_PROTOCOL_VERSION = 'client-chat.v1'
AGENT_PROTOCOL_VERSION = 'agent-runtime.v1'
AGENT_CONTEXT_SCHEMA_VERSION = 'agent-context.v1'
AGENT_INPUT_SCHEMA_VERSION = 'agent-input.v1'
AGENT_OUTPUT_SCHEMA_VERSION = 'agent-output.v1'


def build_version_payload():
    return {
        'application_version': APPLICATION_VERSION,
        'api': {
            'current_version': API_VERSION,
            'status': API_VERSION_STATUS,
            'base_path': f'/api/{API_VERSION}/',
            'legacy_base_path': '/api/',
        },
        'websocket': {
            'client_chat_protocol_version': WEBSOCKET_PROTOCOL_VERSION,
            'client_chat_path': f'/ws/{API_VERSION}/client/cases/<case_id>/chat/',
            'legacy_client_chat_path': '/ws/client/cases/<case_id>/chat/',
        },
        'agents': {
            'protocol_version': AGENT_PROTOCOL_VERSION,
            'context_schema_version': AGENT_CONTEXT_SCHEMA_VERSION,
            'input_schema_version': AGENT_INPUT_SCHEMA_VERSION,
            'output_schema_version': AGENT_OUTPUT_SCHEMA_VERSION,
        },
    }
