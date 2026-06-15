from django.conf import settings
from django.utils import timezone

from actions.models import ActionItem
from core.versioning import AGENT_CONTEXT_SCHEMA_VERSION
from deadlines.models import Deadline
from documents.models import Document
from reference_data.models import PolicySnapshot, PublicationStatus


class AgentContextBuilder:
    def build_case_analysis_context(self, *, case):
        return self._with_version('case_analysis', {
            'case': self._case_payload(case),
            'documents': self._case_documents_payload(case),
            'action_items': self._case_action_items_payload(case),
            'deadlines': self._case_deadlines_payload(case),
            'workflow': {
                'roadmap_state': case.roadmap_state,
                'workflow_config_snapshot': case.workflow_config_snapshot,
            },
        })

    def build_document_analysis_context(self, *, document, max_text_chars=30000):
        case = document.case
        return self._with_version('document_analysis', {
            'document': {
                'id': document.id,
                'document_type': document.document_type.name,
                'original_filename': document.original_filename,
                'status': document.status,
                'extraction_status': document.extraction_status,
                'extracted_at': document.extracted_at.isoformat() if document.extracted_at else None,
                'extraction_metadata': document.extraction_metadata,
            },
            'case': {
                'id': case.id,
                'title': case.title,
                'current_stage': case.current_stage,
                'status': case.status,
                'origin_country': case.origin_country.name,
                'destination_country': case.destination_country.name,
                'visa_type': case.visa_type.name,
            },
            'extracted_text': document.extracted_text[:max_text_chars],
        })

    def build_supervisor_planning_context(self, *, case):
        documents = list(
            Document.objects.select_related('document_type')
            .filter(case=case)
            .order_by('document_type__name', 'id')
        )
        action_items = list(ActionItem.objects.filter(case=case).order_by('status', 'due_date', 'id'))
        deadlines = list(Deadline.objects.filter(case=case).order_by('is_resolved', 'due_date', 'id'))
        source_snapshots = list(
            PolicySnapshot.objects.filter(
                requirements__origin_country=case.origin_country,
                requirements__destination_country=case.destination_country,
                requirements__visa_type=case.visa_type,
                requirements__status=PublicationStatus.ACTIVE,
            )
            .select_related('source', 'source__country')
            .distinct()
            .order_by('-fetched_at')[:3]
        )
        return self._with_version('supervisor_planning', {
            'document_ids': [document.id for document in documents],
            'analyzable_document_ids': [
                document.id
                for document in documents
                if document.extraction_status == Document.ExtractionStatus.COMPLETED and document.extracted_text.strip()
            ],
            'action_item_ids': [item.id for item in action_items],
            'deadline_ids': [deadline.id for deadline in deadlines],
            'policy_snapshot_ids': [snapshot.id for snapshot in source_snapshots],
        })

    def build_client_case_chat_context(self, *, case, actor):
        documents = (
            Document.objects.select_related('document_type')
            .filter(case=case)
            .order_by('document_type__name', 'id')[:20]
        )
        action_items = (
            ActionItem.objects.filter(case=case, target_user=actor)
            .order_by('status', 'due_date', 'id')[:20]
        )
        deadlines = (
            Deadline.objects.filter(case=case, target_user=actor)
            .order_by('is_resolved', 'due_date', 'id')[:20]
        )
        return self._with_version('client_case_chat', {
            'case': {
                'id': case.id,
                'title': case.title,
                'status': case.status,
                'current_stage': case.current_stage,
                'origin_country': case.origin_country.name,
                'destination_country': case.destination_country.name,
                'visa_type': case.visa_type.name,
                'assigned_lawyer_email': case.assigned_lawyer.email if case.assigned_lawyer_id else '',
            },
            'documents': [
                {
                    'id': document.id,
                    'document_type': document.document_type.name,
                    'status': document.status,
                    'original_filename': document.original_filename,
                    'extraction_status': document.extraction_status,
                }
                for document in documents
            ],
            'action_items': [
                {
                    'id': item.id,
                    'title': item.title,
                    'description': item.description,
                    'status': item.status,
                    'due_date': item.due_date.isoformat() if item.due_date else None,
                }
                for item in action_items
            ],
            'deadlines': [
                {
                    'id': deadline.id,
                    'title': deadline.title,
                    'due_date': deadline.due_date.isoformat(),
                    'is_resolved': deadline.is_resolved,
                }
                for deadline in deadlines
            ],
            'limits': {
                'max_input_chars': getattr(settings, 'CLIENT_CHAT_MAX_INPUT_CHARS', 800),
                'context_policy': 'client_visible_case_context_only',
            },
        })

    def _with_version(self, context_kind, payload):
        return {
            'schema_version': AGENT_CONTEXT_SCHEMA_VERSION,
            'context_kind': context_kind,
            **payload,
        }

    def _case_payload(self, case):
        return {
            'id': case.id,
            'title': case.title,
            'status': case.status,
            'current_stage': case.current_stage,
            'origin_country': case.origin_country.name,
            'destination_country': case.destination_country.name,
            'visa_type': case.visa_type.name,
            'assigned_lawyer_id': case.assigned_lawyer_id,
            'intake_summary': case.intake_summary,
            'created_at': case.created_at.isoformat(),
            'updated_at': case.updated_at.isoformat(),
        }

    def _case_documents_payload(self, case):
        return [
            {
                'id': document.id,
                'document_type': document.document_type.name,
                'status': document.status,
                'extraction_status': document.extraction_status,
                'last_reviewed_at': document.last_reviewed_at.isoformat() if document.last_reviewed_at else None,
            }
            for document in Document.objects.select_related('document_type').filter(case=case).order_by('document_type__name', 'id')
        ]

    def _case_action_items_payload(self, case):
        return [
            {
                'id': item.id,
                'title': item.title,
                'status': item.status,
                'origin': item.origin,
                'target_user_id': item.target_user_id,
                'due_date': item.due_date.isoformat() if item.due_date else None,
                'completed_at': item.completed_at.isoformat() if item.completed_at else None,
            }
            for item in ActionItem.objects.filter(case=case).order_by('status', 'due_date', 'id')
        ]

    def _case_deadlines_payload(self, case):
        return [
            {
                'id': deadline.id,
                'title': deadline.title,
                'target_user_id': deadline.target_user_id,
                'due_date': deadline.due_date.isoformat(),
                'is_resolved': deadline.is_resolved,
                'resolved_at': deadline.resolved_at.isoformat() if deadline.resolved_at else None,
                'is_overdue': deadline.due_date < timezone.now() and not deadline.is_resolved,
            }
            for deadline in Deadline.objects.filter(case=case).order_by('is_resolved', 'due_date', 'id')
        ]
