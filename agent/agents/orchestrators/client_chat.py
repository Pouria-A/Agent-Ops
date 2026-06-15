from agents.gateways.base import AgentRequest
from agents.models import AgentTask


CLIENT_CASE_CHAT_SCHEMA = {
    'type': 'object',
    'required': [
        'answer',
        'suggestions',
        'visible_case_context',
        'safety_notes',
        'escalation_recommended',
    ],
    'properties': {
        'answer': {'type': 'string'},
        'suggestions': {'type': 'array', 'items': {'type': 'string'}},
        'visible_case_context': {'type': 'object'},
        'safety_notes': {'type': 'array', 'items': {'type': 'string'}},
        'escalation_recommended': {'type': 'boolean'},
    },
}


class ClientCaseChatOrchestrator:
    key = 'client_case_chat_v1'
    task_type = 'CLIENT_CASE_CHAT'
    target_kwarg = 'case'

    def __init__(self, *, message, client_context):
        self.message = message
        self.client_context = client_context

    def create_task(self, *, target, actor, gateway):
        return AgentTask.objects.create(
            agency=target.agency,
            created_by=actor,
            task_type=AgentTask.TaskType.CLIENT_CASE_CHAT,
            status=AgentTask.Status.RUNNING,
            title=f'Client chat for case {target.id}',
            gateway_type=gateway.gateway_type,
            orchestrator_key=self.key,
            input_payload={
                'case_id': target.id,
                'message': self.message,
            },
            context_payload=self.client_context,
        )

    def build_request(self, *, task, case):
        prompt = (
            'You are a restricted MigrationOps client support chatbot. Answer only using the client-visible '
            'case context and general app workflow help. Do not provide final legal advice, do not reveal staff '
            'notes, extracted document text, audit logs, policy internals, other users, or other cases. '
            'Do not mutate records. If the client asks for legal judgment, document approval, status changes, '
            'or anything outside visible context, recommend contacting agency staff. Return JSON matching the schema.'
        )
        input_payload = {
            'message': self.message,
            'client_visible_context': self.client_context,
        }
        safety_policy = {
            'allowed_actions': [
                'answer_app_usage_questions',
                'summarize_client_visible_case_status',
                'explain_pending_client_tasks',
                'suggest_safe_next_steps',
            ],
            'forbidden_actions': [
                'give_final_legal_advice',
                'approve_documents',
                'reject_documents',
                'change_case_status',
                'complete_action_items',
                'resolve_deadlines',
                'submit_applications',
                'reveal_staff_notes',
                'reveal_audit_logs',
                'reveal_other_cases',
            ],
            'client_only': True,
            'human_review_required_for_legal_or_status_questions': True,
        }
        return AgentRequest(
            task_id=str(task.id),
            task_type=self.task_type,
            prompt=prompt,
            input_payload=input_payload,
            context_payload={
                'domain': 'client_case_chat',
                'case_id': case.id,
                'client_id': case.client_id,
            },
            expected_schema=CLIENT_CASE_CHAT_SCHEMA,
            safety_policy=safety_policy,
            idempotency_key=f'agent-task:{task.id}',
        )

    def validate_output(self, output_payload):
        errors = []
        required_keys = [
            'answer',
            'suggestions',
            'visible_case_context',
            'safety_notes',
            'escalation_recommended',
        ]
        for key in required_keys:
            if key not in output_payload:
                errors.append(f'Missing required key: {key}')
        if 'answer' in output_payload and not isinstance(output_payload['answer'], str):
            errors.append('answer must be a string')
        if 'visible_case_context' in output_payload and not isinstance(output_payload['visible_case_context'], dict):
            errors.append('visible_case_context must be an object')
        if 'escalation_recommended' in output_payload and not isinstance(output_payload['escalation_recommended'], bool):
            errors.append('escalation_recommended must be a boolean')
        for key in ['suggestions', 'safety_notes']:
            if key in output_payload and not isinstance(output_payload[key], list):
                errors.append(f'{key} must be a list')
        return errors
