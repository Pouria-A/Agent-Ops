from django.contrib.contenttypes.models import ContentType
from django.utils import timezone

from agents.context import AgentContextBuilder
from agents.gateways.base import AgentRequest
from agents.models import AgentTask
from documents.models import Document


DOCUMENT_ANALYSIS_SCHEMA = {
    'type': 'object',
    'required': ['summary', 'detected_fields', 'completeness_notes', 'risk_notes', 'next_steps'],
    'properties': {
        'document_type': {'type': 'string'},
        'original_filename': {'type': 'string'},
        'summary': {'type': 'string'},
        'detected_fields': {'type': 'object'},
        'completeness_notes': {'type': 'array', 'items': {'type': 'string'}},
        'risk_notes': {'type': 'array', 'items': {'type': 'string'}},
        'next_steps': {'type': 'array', 'items': {'type': 'string'}},
    },
}


class DocumentAnalysisOrchestrator:
    key = 'document_analysis_v1'
    task_type = 'DOCUMENT_ANALYSIS'
    target_kwarg = 'document'

    def __init__(self, *, context_builder=None):
        self.context_builder = context_builder or AgentContextBuilder()

    def ensure_ready(self, *, document):
        if document.extraction_status != Document.ExtractionStatus.COMPLETED:
            raise ValueError('Document text extraction must be completed before AI analysis.')
        if not document.extracted_text.strip():
            raise ValueError('Document has no extracted text to analyze.')

    def create_task(self, *, target, actor, gateway):
        self.ensure_ready(document=target)
        content_type = ContentType.objects.get_for_model(target)
        return AgentTask.objects.create(
            agency=target.case.agency,
            created_by=actor,
            task_type=AgentTask.TaskType.DOCUMENT_ANALYSIS,
            status=AgentTask.Status.RUNNING,
            title=f'Analyze document {target.id}',
            gateway_type=gateway.gateway_type,
            orchestrator_key=self.key,
            input_payload={
                'document_id': target.id,
                'case_id': target.case_id,
                'document_type_id': target.document_type_id,
                'extraction_status': target.extraction_status,
            },
            context_payload={
                'case_title': target.case.title,
                'document_type': target.document_type.name,
            },
            content_type=content_type,
            object_id=target.id,
            started_at=timezone.now(),
        )

    def build_request(self, *, task, document):
        self.ensure_ready(document=document)
        case = document.case
        prompt = (
            'Analyze this scanned immigration document for agency staff. '
            'Use only the extracted document text and case context provided. '
            'Summarize the document, identify visible fields, note completeness concerns, '
            'and suggest next human-review steps. Do not approve, reject, or change the document status. '
            'Return JSON matching the expected schema.'
        )
        input_payload = self.context_builder.build_document_analysis_context(document=document)
        context_payload = {
            'domain': 'immigration_document_review',
            'human_review_required': True,
            'document_id': document.id,
            'case_id': case.id,
        }
        safety_policy = {
            'allowed_actions': ['summarize', 'extract_visible_fields', 'identify_uncertainty', 'suggest_next_steps'],
            'forbidden_actions': [
                'approve_documents',
                'reject_documents',
                'change_document_status',
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
            expected_schema=DOCUMENT_ANALYSIS_SCHEMA,
            safety_policy=safety_policy,
            idempotency_key=f'agent-task:{task.id}',
        )

    def validate_output(self, output_payload):
        errors = []
        required_keys = ['summary', 'detected_fields', 'completeness_notes', 'risk_notes', 'next_steps']
        for key in required_keys:
            if key not in output_payload:
                errors.append(f'Missing required key: {key}')
        if 'summary' in output_payload and not isinstance(output_payload['summary'], str):
            errors.append('summary must be a string')
        if 'detected_fields' in output_payload and not isinstance(output_payload['detected_fields'], dict):
            errors.append('detected_fields must be an object')
        for key in ['completeness_notes', 'risk_notes', 'next_steps']:
            if key in output_payload and not isinstance(output_payload[key], list):
                errors.append(f'{key} must be a list')
        return errors
