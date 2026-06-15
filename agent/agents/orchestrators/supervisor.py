from agents.gateways.base import AgentRequest


SUPERVISOR_CASE_REVIEW_SCHEMA = {
    'type': 'object',
    'required': [
        'summary',
        'execution_order',
        'subagent_task_ids',
        'risk_notes',
        'next_actions',
        'human_review_required',
    ],
    'properties': {
        'summary': {'type': 'string'},
        'execution_order': {'type': 'array', 'items': {'type': 'string'}},
        'subagent_task_ids': {'type': 'array', 'items': {'type': 'integer'}},
        'risk_notes': {'type': 'array', 'items': {'type': 'string'}},
        'next_actions': {'type': 'array', 'items': {'type': 'string'}},
        'human_review_required': {'type': 'boolean'},
    },
}


class SupervisorCaseReviewOrchestrator:
    key = 'supervisor_case_review_v1'
    task_type = 'SUPERVISOR_CASE_REVIEW'

    def build_request(self, *, task, case, plan, subagent_results):
        prompt = (
            'Aggregate specialist agent outputs for agency staff. Use the case context, execution plan, '
            'and subagent results provided. Identify cross-cutting risks and next actions. Do not mutate records, '
            'approve documents, reject documents, change case status, resolve deadlines, complete action items, '
            'or give final legal advice. Return JSON matching the expected schema.'
        )
        input_payload = {
            'case': {
                'id': case.id,
                'title': case.title,
                'status': case.status,
                'current_stage': case.current_stage,
                'origin_country': case.origin_country.name,
                'destination_country': case.destination_country.name,
                'visa_type': case.visa_type.name,
            },
            'plan': plan,
            'subagent_results': subagent_results,
        }
        context_payload = {
            'domain': 'immigration_case_supervision',
            'human_review_required': True,
            'case_id': case.id,
        }
        safety_policy = {
            'allowed_actions': ['coordinate_agents', 'aggregate_results', 'identify_risks', 'suggest_next_steps'],
            'forbidden_actions': [
                'approve_documents',
                'reject_documents',
                'change_document_status',
                'change_case_status',
                'resolve_deadlines',
                'complete_actions',
                'publish_requirements',
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
            expected_schema=SUPERVISOR_CASE_REVIEW_SCHEMA,
            safety_policy=safety_policy,
            idempotency_key=f'agent-task:{task.id}',
        )

    def validate_output(self, output_payload):
        errors = []
        required_keys = [
            'summary',
            'execution_order',
            'subagent_task_ids',
            'risk_notes',
            'next_actions',
            'human_review_required',
        ]
        for key in required_keys:
            if key not in output_payload:
                errors.append(f'Missing required key: {key}')
        if 'summary' in output_payload and not isinstance(output_payload['summary'], str):
            errors.append('summary must be a string')
        if 'human_review_required' in output_payload and not isinstance(output_payload['human_review_required'], bool):
            errors.append('human_review_required must be a boolean')
        for key in ['execution_order', 'subagent_task_ids', 'risk_notes', 'next_actions']:
            if key in output_payload and not isinstance(output_payload[key], list):
                errors.append(f'{key} must be a list')
        return errors
