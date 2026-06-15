from django.contrib.contenttypes.models import ContentType
from django.utils import timezone

from agents.context import AgentContextBuilder
from agents.gateways.base import AgentRequest
from agents.models import AgentTask


CASE_ANALYSIS_SCHEMA = {
    'type': 'object',
    'required': ['summary', 'status_notes', 'risk_notes', 'next_actions', 'overdue_items', 'client_message_draft'],
    'properties': {
        'summary': {'type': 'string'},
        'status_notes': {'type': 'array', 'items': {'type': 'string'}},
        'risk_notes': {'type': 'array', 'items': {'type': 'string'}},
        'next_actions': {'type': 'array', 'items': {'type': 'string'}},
        'overdue_items': {'type': 'array', 'items': {'type': 'object'}},
        'client_message_draft': {'type': 'string'},
    },
}


class CaseAnalysisOrchestrator:
    key = 'case_analysis_v1'
    task_type = 'CASE_ANALYSIS'
    target_kwarg = 'case'

    def __init__(self, *, context_builder=None):
        self.context_builder = context_builder or AgentContextBuilder()

    def create_task(self, *, target, actor, gateway):
        content_type = ContentType.objects.get_for_model(target)
        return AgentTask.objects.create(
            agency=target.agency,
            created_by=actor,
            task_type=AgentTask.TaskType.CASE_ANALYSIS,
            status=AgentTask.Status.RUNNING,
            title=f'Analyze case {target.id}',
            gateway_type=gateway.gateway_type,
            orchestrator_key=self.key,
            input_payload={
                'case_id': target.id,
                'client_id': target.client_id,
                'assigned_lawyer_id': target.assigned_lawyer_id,
                'status': target.status,
                'current_stage': target.current_stage,
            },
            context_payload={
                'case_title': target.title,
                'visa_type': target.visa_type.name,
                'destination_country': target.destination_country.name,
            },
            content_type=content_type,
            object_id=target.id,
            started_at=timezone.now(),
        )

    def build_request(self, *, task, case):
        prompt = (
            'Analyze this immigration case for agency staff. Use only the case context, documents, '
            'action items, and deadlines provided. Summarize current progress, identify risks, '
            'and suggest next staff actions. Do not change case status, approve documents, reject documents, '
            'resolve deadlines, complete actions, or give final legal advice. Return JSON matching the expected schema.'
        )
        input_payload = self.context_builder.build_case_analysis_context(case=case)
        context_payload = {
            'domain': 'immigration_case_operations',
            'human_review_required': True,
            'case_id': case.id,
        }
        safety_policy = {
            'allowed_actions': ['summarize', 'identify_risks', 'suggest_next_steps', 'draft_client_message'],
            'forbidden_actions': [
                'approve_documents',
                'reject_documents',
                'change_document_status',
                'change_case_status',
                'resolve_deadlines',
                'complete_actions',
                'submit_applications',
                'give_final_legal_advice',
            ],
            'human_review_required': True,
        }
        return AgentRequest(
            task_id=str(task.id),
            task_type=self.task_type,
            prompt=prompt,
            input_payload=input_payload,
            context_payload=context_payload,
            expected_schema=CASE_ANALYSIS_SCHEMA,
            safety_policy=safety_policy,
            idempotency_key=f'agent-task:{task.id}',
        )

    def validate_output(self, output_payload):
        errors = []
        required_keys = ['summary', 'status_notes', 'risk_notes', 'next_actions', 'overdue_items', 'client_message_draft']
        for key in required_keys:
            if key not in output_payload:
                errors.append(f'Missing required key: {key}')
        for key in ['summary', 'client_message_draft']:
            if key in output_payload and not isinstance(output_payload[key], str):
                errors.append(f'{key} must be a string')
        for key in ['status_notes', 'risk_notes', 'next_actions', 'overdue_items']:
            if key in output_payload and not isinstance(output_payload[key], list):
                errors.append(f'{key} must be a list')
        return errors
