from .base import AgentEnvironmentCheck, AgentEnvironmentReport, AgentRequest, AgentResponse


class MockAgentGateway:
    gateway_type = 'mock'

    def execute(self, request: AgentRequest):
        if request.task_type == 'POLICY_SNAPSHOT_SUMMARY':
            source = request.input_payload.get('source', {})
            text = request.input_payload.get('raw_text', '')
            compact_text = ' '.join(text.split())
            summary = compact_text[:500] if compact_text else 'No policy text was available to summarize.'
            output = {
                'source_name': source.get('name', ''),
                'source_url': source.get('url', ''),
                'summary': summary,
                'key_points': self._key_points(compact_text),
                'possible_requirements': [],
                'risk_notes': [
                    'Mock AI output only. A human must review before publishing requirements.',
                ],
                'next_steps': [
                    'Review the snapshot text.',
                    'Extract draft requirements only after legal review.',
                ],
            }
            return AgentResponse(
                status='succeeded',
                output_payload=output,
                summary=output['summary'],
                provider='mock',
                model='mock-policy-summarizer',
                usage={'input_chars': len(text), 'output_chars': len(output['summary'])},
                events=[
                    {
                        'level': 'info',
                        'message': 'Generated deterministic mock policy summary.',
                    }
                ],
            )

        if request.task_type == 'DOCUMENT_ANALYSIS':
            document = request.input_payload.get('document', {})
            text = request.input_payload.get('extracted_text', '')
            compact_text = ' '.join(text.split())
            summary = compact_text[:500] if compact_text else 'No extracted document text was available to analyze.'
            output = {
                'document_type': document.get('document_type', ''),
                'original_filename': document.get('original_filename', ''),
                'summary': summary,
                'detected_fields': self._detected_document_fields(compact_text),
                'completeness_notes': [
                    'Mock AI output only. Staff must review the scanned text and original document.',
                ],
                'risk_notes': [
                    'This result does not approve or reject the document.',
                ],
                'next_steps': [
                    'Compare extracted text against the original uploaded file.',
                    'Have an agency lawyer or admin make the final review decision.',
                ],
            }
            return AgentResponse(
                status='succeeded',
                output_payload=output,
                summary=output['summary'],
                provider='mock',
                model='mock-document-analyzer',
                usage={'input_chars': len(text), 'output_chars': len(output['summary'])},
                events=[
                    {
                        'level': 'info',
                        'message': 'Generated deterministic mock document analysis.',
                    }
                ],
            )

        if request.task_type == 'CASE_ANALYSIS':
            case = request.input_payload.get('case', {})
            documents = request.input_payload.get('documents', [])
            action_items = request.input_payload.get('action_items', [])
            deadlines = request.input_payload.get('deadlines', [])
            overdue_deadlines = [deadline for deadline in deadlines if deadline.get('is_overdue')]
            pending_actions = [item for item in action_items if item.get('status') in {'PENDING', 'IN_PROGRESS'}]
            summary = (
                f"Case '{case.get('title', '')}' is {case.get('status', '')} "
                f"at stage {case.get('current_stage', '')}. "
                f"{len(documents)} document(s), {len(pending_actions)} pending action(s), "
                f"and {len(overdue_deadlines)} overdue deadline(s) are visible."
            )
            output = {
                'summary': summary,
                'status_notes': [
                    f"Current stage: {case.get('current_stage', '')}",
                    f"Visa route: {case.get('origin_country', '')} to {case.get('destination_country', '')} - {case.get('visa_type', '')}",
                ],
                'risk_notes': self._case_risk_notes(documents, pending_actions, overdue_deadlines),
                'next_actions': self._case_next_actions(documents, pending_actions, overdue_deadlines),
                'overdue_items': overdue_deadlines,
                'client_message_draft': (
                    'Your case is being reviewed by the agency team. '
                    'Please complete any pending upload or information requests shown in your portal.'
                ),
            }
            return AgentResponse(
                status='succeeded',
                output_payload=output,
                summary=output['summary'],
                provider='mock',
                model='mock-case-analyzer',
                usage={'input_items': len(documents) + len(action_items) + len(deadlines), 'output_chars': len(output['summary'])},
                events=[
                    {
                        'level': 'info',
                        'message': 'Generated deterministic mock case analysis.',
                    }
                ],
            )

        if request.task_type == 'SUPERVISOR_CASE_REVIEW':
            plan = request.input_payload.get('plan', {})
            subagent_results = request.input_payload.get('subagent_results', {})
            execution_order = plan.get('execution_order', [])
            subagent_task_ids = self._subagent_task_ids(subagent_results)
            case_result = subagent_results.get('case') or {}
            document_results = subagent_results.get('documents') or []
            policy_results = subagent_results.get('policy') or []
            summary = (
                f"Supervisor ran {len(subagent_task_ids)} specialist task(s): "
                f"{', '.join(execution_order) or 'none'}. "
                f"Case summary: {case_result.get('summary', 'No case summary available')}"
            )
            output = {
                'summary': summary,
                'execution_order': execution_order,
                'subagent_task_ids': subagent_task_ids,
                'risk_notes': self._supervisor_risk_notes(case_result, document_results, policy_results, plan),
                'next_actions': self._supervisor_next_actions(plan),
                'human_review_required': True,
            }
            return AgentResponse(
                status='succeeded',
                output_payload=output,
                summary=output['summary'],
                provider='mock',
                model='mock-supervisor',
                usage={'subagent_tasks': len(subagent_task_ids), 'output_chars': len(output['summary'])},
                events=[
                    {
                        'level': 'info',
                        'message': 'Generated deterministic mock supervisor case review.',
                    }
                ],
            )

        if request.task_type == 'CLIENT_CASE_CHAT':
            context = request.input_payload.get('client_visible_context', {})
            message = request.input_payload.get('message', '')
            case = context.get('case', {})
            pending_actions = [
                item
                for item in context.get('action_items', [])
                if item.get('status') in {'PENDING', 'IN_PROGRESS'}
            ]
            open_deadlines = [
                deadline
                for deadline in context.get('deadlines', [])
                if not deadline.get('is_resolved')
            ]
            answer = (
                f"Your case '{case.get('title', '')}' is currently {case.get('status', '')} "
                f"at stage {case.get('current_stage', '')}. "
                f"You have {len(pending_actions)} open task(s) and {len(open_deadlines)} open deadline(s) visible here."
            )
            if message:
                answer = f"{answer} I can help with case status, visible tasks, document upload steps, and app navigation."
            output = {
                'answer': answer,
                'suggestions': self._client_chat_suggestions(context),
                'visible_case_context': {
                    'case_id': case.get('id'),
                    'current_stage': case.get('current_stage', ''),
                    'document_count': len(context.get('documents', [])),
                    'open_action_count': len(pending_actions),
                    'open_deadline_count': len(open_deadlines),
                },
                'safety_notes': [
                    'This chatbot cannot approve documents, change case status, or provide final legal advice.',
                ],
                'escalation_recommended': self._client_chat_escalation_recommended(message),
            }
            return AgentResponse(
                status='succeeded',
                output_payload=output,
                summary=output['answer'],
                provider='mock',
                model='mock-client-chat',
                usage={'input_chars': len(message), 'output_chars': len(output['answer'])},
                events=[
                    {
                        'level': 'info',
                        'message': 'Generated deterministic mock client chat response.',
                    }
                ],
            )

        return AgentResponse(
            status='failed',
            output_payload={},
            provider='mock',
            model='mock',
            error_message=f'Unsupported mock task type: {request.task_type}',
        )

    def test_environment(self):
        return AgentEnvironmentReport(
            gateway_type=self.gateway_type,
            status='pass',
            checks=[
                AgentEnvironmentCheck(
                    code='mock_gateway_available',
                    level='info',
                    message='Mock AI gateway is available.',
                )
            ],
        )

    def _key_points(self, text):
        if not text:
            return []
        sentences = [entry.strip() for entry in text.replace('\n', ' ').split('.') if entry.strip()]
        return sentences[:5]

    def _detected_document_fields(self, text):
        lowered = text.lower()
        fields = {}
        if 'passport' in lowered:
            fields['document_kind'] = 'passport'
        if 'passport number' in lowered:
            fields['passport_number_present'] = True
        if 'expiry' in lowered or 'expiration' in lowered:
            fields['expiry_reference_present'] = True
        if 'birth' in lowered or 'date of birth' in lowered:
            fields['birth_date_reference_present'] = True
        return fields

    def _case_risk_notes(self, documents, pending_actions, overdue_deadlines):
        notes = []
        if overdue_deadlines:
            notes.append('One or more deadlines appear overdue and need staff review.')
        if pending_actions:
            notes.append('There are pending action items that may block case progress.')
        if any(document.get('status') == 'REJECTED' for document in documents):
            notes.append('At least one document has been rejected.')
        if not documents:
            notes.append('No uploaded documents are visible for this case.')
        if not notes:
            notes.append('No obvious mock risk was detected from the provided case context.')
        return notes

    def _case_next_actions(self, documents, pending_actions, overdue_deadlines):
        actions = []
        if overdue_deadlines:
            actions.append('Review overdue deadlines and update the responsible owner.')
        if pending_actions:
            actions.append('Follow up on pending client or staff action items.')
        if any(document.get('extraction_status') != 'COMPLETED' for document in documents):
            actions.append('Complete document text extraction before relying on AI document analysis.')
        actions.append('Have agency staff review this AI case summary before contacting the client.')
        return actions

    def _subagent_task_ids(self, subagent_results):
        task_ids = []
        case_result = subagent_results.get('case')
        if case_result and case_result.get('task_id'):
            task_ids.append(case_result['task_id'])
        for result in subagent_results.get('documents') or []:
            if result.get('task_id'):
                task_ids.append(result['task_id'])
        for result in subagent_results.get('policy') or []:
            if result.get('task_id'):
                task_ids.append(result['task_id'])
        return task_ids

    def _supervisor_risk_notes(self, case_result, document_results, policy_results, plan):
        notes = []
        case_payload = case_result.get('output_payload') or {}
        notes.extend(case_payload.get('risk_notes') or [])
        if plan.get('document_agents', {}).get('skipped_document_ids'):
            notes.append('Some documents were skipped because extracted text is not ready.')
        if not document_results:
            notes.append('No document specialist results were available for aggregation.')
        if not policy_results:
            notes.append('No policy specialist results were available for aggregation.')
        if not notes:
            notes.append('No obvious mock supervisor risk was detected.')
        return notes

    def _supervisor_next_actions(self, plan):
        actions = ['Human staff must review the supervisor result before taking legal or operational action.']
        if plan.get('document_agents', {}).get('skipped_document_ids'):
            actions.append('Complete document scanning for skipped documents.')
        if not plan.get('policy_agents', {}).get('enabled'):
            actions.append('Review active visa requirements manually if policy context is needed.')
        return actions

    def _client_chat_suggestions(self, context):
        suggestions = []
        if context.get('action_items'):
            suggestions.append('Open your tasks list and complete any pending upload or information requests.')
        if context.get('documents'):
            suggestions.append('Check each document status before uploading another copy.')
        suggestions.append('Contact agency staff for legal advice or urgent case changes.')
        return suggestions

    def _client_chat_escalation_recommended(self, message):
        lowered = (message or '').lower()
        escalation_terms = [
            'legal advice',
            'approve',
            'reject',
            'change status',
            'submit',
            'urgent',
            'deadline missed',
        ]
        return any(term in lowered for term in escalation_terms)
