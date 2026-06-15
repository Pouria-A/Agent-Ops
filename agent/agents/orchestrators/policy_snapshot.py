from django.contrib.contenttypes.models import ContentType
from django.utils import timezone

from agents.models import AgentTask
from agents.gateways.base import AgentRequest


POLICY_SNAPSHOT_SUMMARY_SCHEMA = {
    'type': 'object',
    'required': ['summary', 'key_points', 'risk_notes', 'next_steps'],
    'properties': {
        'source_name': {'type': 'string'},
        'source_url': {'type': 'string'},
        'summary': {'type': 'string'},
        'key_points': {'type': 'array', 'items': {'type': 'string'}},
        'possible_requirements': {'type': 'array', 'items': {'type': 'object'}},
        'risk_notes': {'type': 'array', 'items': {'type': 'string'}},
        'next_steps': {'type': 'array', 'items': {'type': 'string'}},
    },
}


class PolicySnapshotSummaryOrchestrator:
    key = 'policy_snapshot_summary_v1'
    task_type = 'POLICY_SNAPSHOT_SUMMARY'
    target_kwarg = 'snapshot'

    def create_task(self, *, target, actor, gateway):
        content_type = ContentType.objects.get_for_model(target)
        return AgentTask.objects.create(
            agency=getattr(actor, 'agency', None),
            created_by=actor,
            task_type=AgentTask.TaskType.POLICY_SNAPSHOT_SUMMARY,
            status=AgentTask.Status.RUNNING,
            title=f'Summarize policy snapshot {target.id}',
            gateway_type=gateway.gateway_type,
            orchestrator_key=self.key,
            input_payload={
                'snapshot_id': target.id,
                'source_id': target.source_id,
                'content_hash': target.content_hash,
            },
            context_payload={
                'source_name': target.source.name,
                'source_url': target.source.url,
            },
            content_type=content_type,
            object_id=target.id,
            started_at=timezone.now(),
        )

    def build_request(self, *, task, snapshot):
        raw_text = snapshot.raw_text or snapshot.raw_html or ''
        source = snapshot.source
        metadata = snapshot.raw_metadata or {}

        prompt = (
            'Summarize this immigration policy snapshot for agency staff. '
            'Extract only what is supported by the source text. '
            'Do not approve, publish, or create legal requirements. '
            'Return JSON matching the expected schema.'
        )
        input_payload = {
            'source': {
                'id': source.id,
                'name': source.name,
                'url': source.url,
                'country': source.country.name,
                'source_type': source.source_type,
            },
            'snapshot': {
                'id': snapshot.id,
                'content_hash': snapshot.content_hash,
                'fetched_at': snapshot.fetched_at.isoformat(),
                'status': snapshot.status,
            },
            'raw_text': raw_text[:30000],
            'raw_metadata': metadata,
        }
        context_payload = {
            'domain': 'immigration_policy',
            'human_review_required': True,
            'source_snapshot_id': snapshot.id,
        }
        safety_policy = {
            'allowed_actions': ['summarize', 'extract_draft_points', 'identify_uncertainty'],
            'forbidden_actions': [
                'approve_documents',
                'reject_documents',
                'publish_requirements',
                'change_case_status',
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
            expected_schema=POLICY_SNAPSHOT_SUMMARY_SCHEMA,
            safety_policy=safety_policy,
            idempotency_key=f'agent-task:{task.id}',
        )

    def validate_output(self, output_payload):
        errors = []
        required_keys = ['summary', 'key_points', 'risk_notes', 'next_steps']
        for key in required_keys:
            if key not in output_payload:
                errors.append(f'Missing required key: {key}')
        if 'summary' in output_payload and not isinstance(output_payload['summary'], str):
            errors.append('summary must be a string')
        for key in ['key_points', 'risk_notes', 'next_steps']:
            if key in output_payload and not isinstance(output_payload[key], list):
                errors.append(f'{key} must be a list')
        return errors
