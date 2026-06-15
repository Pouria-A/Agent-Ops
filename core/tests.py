from datetime import timedelta
from unittest.mock import patch

from asgiref.sync import async_to_sync
from channels.testing import WebsocketCommunicator
from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.test import TestCase, override_settings
from django.utils import timezone
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework.test import APITestCase

from actions.models import ActionItem
from agencies.models import AgencyWorkflowTemplate
from agencies.models import Agency
from agents.context import AgentContextBuilder
from agents.models import AgentTask, ClientChatSocketTicket
from agents.services import AgentServiceError, analyze_case, chat_with_client_case, summarize_policy_snapshot
from audit.models import AuditEvent
from cases.models import StudentCase
from cases.services import generate_checklist_for_case
from core.versioning import (
    AGENT_CONTEXT_SCHEMA_VERSION,
    AGENT_OUTPUT_SCHEMA_VERSION,
    AGENT_PROTOCOL_VERSION,
    WEBSOCKET_PROTOCOL_VERSION,
)
from deadlines.models import Deadline
from documents.models import Document
from documents.scanners import DocumentScannerGateway, ScanResult
from documents.services import DocumentExtractionError, queue_document_scan, scan_document
from notifications.models import Notification
from notifications.tasks import (
    create_overdue_action_notifications,
    create_stalled_case_notifications,
    create_upcoming_deadline_notifications,
)
from reference_data.crawlers import CrawlResult
from reference_data.models import Country, DocumentType, PolicySnapshot, PolicySource, VisaRequirement, VisaType
from reference_data.services import crawl_policy_source
from users.models import ClientProfile, LawyerProfile, User, UserRole
from migrationops.asgi import application


class MvpModelTests(TestCase):
    def setUp(self):
        self.agency = Agency.objects.create(
            name='MigrationOps Test Agency',
            email='ops@example.com',
        )
        self.client = User.objects.create_user(
            email='client@example.com',
            password='testpass123',
            role=UserRole.CLIENT,
            agency=self.agency,
        )
        self.lawyer = User.objects.create_user(
            email='lawyer@example.com',
            password='testpass123',
            role=UserRole.LAWYER,
            agency=self.agency,
        )
        self.iran, _ = Country.objects.get_or_create(
            iso_code='IR',
            defaults={'name': 'Iran'},
        )
        self.italy, _ = Country.objects.get_or_create(
            iso_code='IT',
            defaults={'name': 'Italy'},
        )
        self.visa_type, _ = VisaType.objects.get_or_create(
            slug='italy-student-university-enrollment',
            defaults={'name': 'Italy Student Visa - University Enrollment'},
        )
        self.document_type, _ = DocumentType.objects.get_or_create(
            slug='passport',
            defaults={'name': 'Passport'},
        )

    def test_email_user_and_agency_case_graph(self):
        case = StudentCase.objects.create(
            agency=self.agency,
            client=self.client,
            assigned_lawyer=self.lawyer,
            origin_country=self.iran,
            destination_country=self.italy,
            visa_type=self.visa_type,
            title='Client Italy student visa case',
        )

        self.assertEqual(str(self.client), 'client@example.com')
        self.assertEqual(case.agency, self.agency)
        self.assertEqual(case.status, StudentCase.Status.ACTIVE)
        self.assertEqual(case.current_stage, 'intake')

    def test_requirement_document_action_deadline_and_audit_records(self):
        requirement, _ = VisaRequirement.objects.get_or_create(
            origin_country=self.iran,
            destination_country=self.italy,
            visa_type=self.visa_type,
            document_type=self.document_type,
            status='ACTIVE',
            defaults={'title': 'Valid passport'},
        )
        case = StudentCase.objects.create(
            agency=self.agency,
            client=self.client,
            assigned_lawyer=self.lawyer,
            origin_country=self.iran,
            destination_country=self.italy,
            visa_type=self.visa_type,
            title='Client Italy student visa case',
        )
        document = Document.objects.create(
            case=case,
            document_type=self.document_type,
            uploaded_by=self.client,
            file='case_documents/case_1/passport.pdf',
            original_filename='passport.pdf',
        )
        action = ActionItem.objects.create(
            case=case,
            related_document=document,
            title='Review passport',
            target_user=self.lawyer,
            created_by=self.lawyer,
            origin=ActionItem.Origin.LAWYER,
        )
        deadline = Deadline.objects.create(
            case=case,
            action_item=action,
            title='Passport review due',
            target_user=self.lawyer,
            due_date=timezone.now(),
        )
        audit_event = AuditEvent.objects.create(
            agency=self.agency,
            actor=self.lawyer,
            event_type=AuditEvent.EventType.CREATED,
            object_app_label='cases',
            object_model='StudentCase',
            object_id=str(case.id),
            after={'status': case.status},
        )

        self.assertEqual(requirement.document_type, self.document_type)
        self.assertEqual(document.status, Document.Status.UPLOADED)
        self.assertEqual(action.status, ActionItem.Status.PENDING)
        self.assertFalse(deadline.is_resolved)
        self.assertEqual(audit_event.agency, self.agency)

    def test_scan_document_stores_extracted_text_and_metadata(self):
        case = StudentCase.objects.create(
            agency=self.agency,
            client=self.client,
            assigned_lawyer=self.lawyer,
            origin_country=self.iran,
            destination_country=self.italy,
            visa_type=self.visa_type,
            title='Client Italy student visa case',
        )
        document = Document.objects.create(
            case=case,
            document_type=self.document_type,
            uploaded_by=self.client,
            file='case_documents/case_1/passport.pdf',
            original_filename='passport.pdf',
        )

        class FakeScanner:
            def scan(self, document):
                return ScanResult(
                    text='Passport number AB123456',
                    metadata={'provider': 'fake', 'requires_ocr': False},
                )

        scanned_document = scan_document(document=document, actor=self.lawyer, scanner=FakeScanner())

        self.assertEqual(scanned_document.extraction_status, Document.ExtractionStatus.COMPLETED)
        self.assertEqual(scanned_document.extracted_text, 'Passport number AB123456')
        self.assertEqual(scanned_document.extraction_metadata['provider'], 'fake')
        self.assertIn('visibility_score', scanned_document.extraction_metadata)
        self.assertIsNotNone(scanned_document.extracted_at)

    def test_scan_document_marks_low_visibility_for_human_review(self):
        case = StudentCase.objects.create(
            agency=self.agency,
            client=self.client,
            assigned_lawyer=self.lawyer,
            origin_country=self.iran,
            destination_country=self.italy,
            visa_type=self.visa_type,
            title='Low visibility document case',
        )
        document = Document.objects.create(
            case=case,
            document_type=self.document_type,
            uploaded_by=self.client,
            file='case_documents/case_1/handwritten.jpg',
            original_filename='handwritten.jpg',
        )

        class LowVisibilityScanner:
            def scan(self, document):
                return ScanResult(
                    text='',
                    metadata={'provider': 'fake_low_visibility', 'requires_ocr': True},
                    confidence_score=25,
                    warnings=['Unreadable handwriting.'],
                    handwriting_detected=True,
                    human_review_required=True,
                )

        scanned_document = scan_document(document=document, actor=self.lawyer, scanner=LowVisibilityScanner())

        self.assertEqual(scanned_document.extraction_status, Document.ExtractionStatus.COMPLETED)
        self.assertEqual(scanned_document.confidence_score, 25)
        self.assertTrue(scanned_document.extraction_metadata['human_review_required'])
        self.assertTrue(scanned_document.extraction_metadata['handwriting_detected'])
        self.assertIn('Unreadable handwriting.', scanned_document.extraction_metadata['warnings'])

    def test_document_scanner_gateway_routes_ocr_candidates_to_ocr_provider(self):
        case = StudentCase.objects.create(
            agency=self.agency,
            client=self.client,
            assigned_lawyer=self.lawyer,
            origin_country=self.iran,
            destination_country=self.italy,
            visa_type=self.visa_type,
            title='OCR fallback case',
        )
        document = Document.objects.create(
            case=case,
            document_type=self.document_type,
            uploaded_by=self.client,
            file='case_documents/case_1/scan.png',
            original_filename='scan.png',
        )

        class FakePrimaryScanner:
            def scan(self, document):
                return ScanResult(
                    text='',
                    metadata={'provider': 'fake_primary', 'requires_ocr': True, 'scanner_pipeline': ['fake_primary']},
                    confidence_score=0,
                )

        class FakeOcrProvider:
            def scan(self, document, prior_result=None):
                return ScanResult(
                    text='Handwritten passport number AB123456',
                    metadata={
                        **prior_result.metadata,
                        'provider': 'fake_ocr',
                        'requires_ocr': False,
                        'scanner_pipeline': ['fake_primary', 'fake_ocr'],
                    },
                    confidence_score=68,
                    pages=[{'page_number': 1, 'confidence': 68, 'text': 'Handwritten passport number AB123456'}],
                    detected_languages=['en'],
                    handwriting_detected=True,
                )

        result = DocumentScannerGateway(provider=FakePrimaryScanner(), ocr_provider=FakeOcrProvider()).scan(document)

        self.assertEqual(result.text, 'Handwritten passport number AB123456')
        self.assertEqual(result.metadata['provider'], 'fake_ocr')
        self.assertEqual(result.metadata['visibility_score'], 68)
        self.assertEqual(result.metadata['scanner_pipeline'], ['fake_primary', 'fake_ocr'])

    def test_queue_document_scan_dispatches_after_transaction_commit(self):
        case = StudentCase.objects.create(
            agency=self.agency,
            client=self.client,
            assigned_lawyer=self.lawyer,
            origin_country=self.iran,
            destination_country=self.italy,
            visa_type=self.visa_type,
            title='Queued scan case',
        )
        document = Document.objects.create(
            case=case,
            document_type=self.document_type,
            uploaded_by=self.client,
            file='case_documents/case_1/passport.pdf',
            original_filename='passport.pdf',
        )

        with patch('documents.tasks.scan_document_task.delay') as delay:
            with self.captureOnCommitCallbacks(execute=False) as callbacks:
                with transaction.atomic():
                    queue_document_scan(document)
                    delay.assert_not_called()

            delay.assert_not_called()
            self.assertEqual(len(callbacks), 1)
            callbacks[0]()
            delay.assert_called_once_with(document.id)

    def test_queue_document_scan_does_not_duplicate_pending_scan(self):
        case = StudentCase.objects.create(
            agency=self.agency,
            client=self.client,
            assigned_lawyer=self.lawyer,
            origin_country=self.iran,
            destination_country=self.italy,
            visa_type=self.visa_type,
            title='Pending scan case',
        )
        document = Document.objects.create(
            case=case,
            document_type=self.document_type,
            uploaded_by=self.client,
            file='case_documents/case_1/passport.pdf',
            original_filename='passport.pdf',
            extraction_status=Document.ExtractionStatus.PENDING,
        )

        with patch('documents.tasks.scan_document_task.delay') as delay:
            queue_document_scan(document)

        delay.assert_not_called()
        document.refresh_from_db()
        self.assertEqual(document.extraction_status, Document.ExtractionStatus.PENDING)

    def test_scan_document_rejects_already_processing_document(self):
        case = StudentCase.objects.create(
            agency=self.agency,
            client=self.client,
            assigned_lawyer=self.lawyer,
            origin_country=self.iran,
            destination_country=self.italy,
            visa_type=self.visa_type,
            title='Processing scan case',
        )
        document = Document.objects.create(
            case=case,
            document_type=self.document_type,
            uploaded_by=self.client,
            file='case_documents/case_1/passport.pdf',
            original_filename='passport.pdf',
            extraction_status=Document.ExtractionStatus.PROCESSING,
        )

        with self.assertRaises(DocumentExtractionError):
            scan_document(document=document, actor=self.lawyer)

    def test_generate_checklist_is_idempotent(self):
        case = StudentCase.objects.create(
            agency=self.agency,
            client=self.client,
            assigned_lawyer=self.lawyer,
            origin_country=self.iran,
            destination_country=self.italy,
            visa_type=self.visa_type,
            title='Checklist idempotency case',
        )

        first_created = generate_checklist_for_case(case, self.lawyer)
        second_created = generate_checklist_for_case(case, self.lawyer)

        self.assertGreaterEqual(len(first_created), 1)
        self.assertEqual(second_created, [])
        self.assertEqual(
            ActionItem.objects.filter(case=case, origin=ActionItem.Origin.WORKFLOW).count(),
            len(first_created),
        )

    def test_agent_analysis_rejects_duplicate_running_target_task(self):
        case = StudentCase.objects.create(
            agency=self.agency,
            client=self.client,
            assigned_lawyer=self.lawyer,
            origin_country=self.iran,
            destination_country=self.italy,
            visa_type=self.visa_type,
            title='Duplicate agent case',
        )
        AgentTask.objects.create(
            agency=self.agency,
            created_by=self.lawyer,
            task_type=AgentTask.TaskType.CASE_ANALYSIS,
            status=AgentTask.Status.RUNNING,
            title='Running analysis',
            content_type=ContentType.objects.get_for_model(case),
            object_id=case.id,
        )

        with self.assertRaises(AgentServiceError):
            analyze_case(case=case, actor=self.lawyer)

    def test_crawl_policy_source_stores_snapshot_and_reuses_unchanged_content(self):
        source = PolicySource.objects.create(
            country=self.italy,
            name='Italy student visa source',
            url='https://example.com/italy-student-visa',
        )

        class FakeCrawler:
            def crawl(self, source):
                return CrawlResult(
                    final_url=source.url,
                    status_code=200,
                    title='Italy student visa',
                    text='Passport and university enrollment letter are required.',
                    html='<html><body>Passport and university enrollment letter are required.</body></html>',
                    metadata={'provider': 'fake'},
                )

        first_run, first_snapshot, first_created = crawl_policy_source(source=source, crawler=FakeCrawler())
        second_run, second_snapshot, second_created = crawl_policy_source(source=source, crawler=FakeCrawler())

        self.assertEqual(first_run.status, 'SUCCEEDED')
        self.assertTrue(first_created)
        self.assertEqual(first_snapshot.raw_text, 'Passport and university enrollment letter are required.')
        self.assertEqual(first_snapshot.raw_metadata['provider'], 'fake')
        self.assertEqual(second_run.status, 'SUCCEEDED')
        self.assertEqual(first_snapshot.id, second_snapshot.id)
        self.assertFalse(second_created)

    def test_summarize_policy_snapshot_creates_agent_result(self):
        source = PolicySource.objects.create(
            country=self.italy,
            name='Italy student visa source',
            url='https://example.com/italy-ai-summary',
        )
        snapshot = PolicySnapshot.objects.create(
            source=source,
            content_hash='a' * 64,
            raw_text='Passport and university enrollment letter are required. Proof of funds may be requested.',
            raw_html='<html><body>Passport and enrollment letter are required.</body></html>',
            raw_metadata={'provider': 'fake'},
            fetched_at=timezone.now(),
        )

        task, result = summarize_policy_snapshot(snapshot=snapshot, actor=self.lawyer)

        self.assertEqual(task.status, AgentTask.Status.SUCCEEDED)
        self.assertEqual(task.gateway_type, 'mock')
        self.assertEqual(result.provider, 'mock')
        self.assertTrue(result.requires_human_review)
        self.assertIn('Passport', result.summary)


@override_settings(MEDIA_ROOT='/tmp/migrationops-test-media')
class MvpApiPermissionTests(APITestCase):
    def setUp(self):
        self.agency = Agency.objects.create(name='Agency A', email='a@example.com')
        self.other_agency = Agency.objects.create(name='Agency B', email='b@example.com')
        self.client_user = User.objects.create_user(
            email='client-a@example.com',
            password='testpass123',
            role=UserRole.CLIENT,
            agency=self.agency,
        )
        self.lawyer = User.objects.create_user(
            email='lawyer-a@example.com',
            password='testpass123',
            role=UserRole.LAWYER,
            agency=self.agency,
        )
        self.admin_user = User.objects.create_user(
            email='admin-a@example.com',
            password='testpass123',
            role=UserRole.ADMIN,
            agency=self.agency,
        )
        self.paralegal = User.objects.create_user(
            email='paralegal-a@example.com',
            password='testpass123',
            role=UserRole.PARALEGAL,
            agency=self.agency,
        )
        self.other_client = User.objects.create_user(
            email='client-b@example.com',
            password='testpass123',
            role=UserRole.CLIENT,
            agency=self.other_agency,
        )
        self.iran = Country.objects.get(iso_code='IR')
        self.italy = Country.objects.get(iso_code='IT')
        self.visa_type = VisaType.objects.get(slug='italy-student-university-enrollment')
        self.document_type = DocumentType.objects.get(slug='passport')
        self.case = StudentCase.objects.create(
            agency=self.agency,
            client=self.client_user,
            assigned_lawyer=self.lawyer,
            origin_country=self.iran,
            destination_country=self.italy,
            visa_type=self.visa_type,
            title='Agency A case',
        )
        self.other_case = StudentCase.objects.create(
            agency=self.other_agency,
            client=self.other_client,
            origin_country=self.iran,
            destination_country=self.italy,
            visa_type=self.visa_type,
            title='Agency B case',
        )

    def _role_users(self):
        return {
            'admin': self.admin_user,
            'lawyer': self.lawyer,
            'paralegal': self.paralegal,
            'client': self.client_user,
        }

    def _assert_role_matrix(self, *, checks):
        for label, user in self._role_users().items():
            for check in checks:
                self.client.force_authenticate(user=user)
                response = check['request']()
                expected_status = check['expected'][label]
                with self.subTest(role=label, endpoint=check['name']):
                    self.assertEqual(response.status_code, expected_status)

    def _case_payload(self, title):
        return {
            'agency': self.agency.id,
            'client': self.client_user.id,
            'assigned_lawyer': self.lawyer.id,
            'origin_country': self.iran.id,
            'destination_country': self.italy.id,
            'visa_type': self.visa_type.id,
            'title': title,
        }

    def test_role_permission_matrix_for_core_boundaries(self):
        checks = [
            {
                'name': 'agency settings update',
                'request': lambda: self.client.patch(
                    f'/api/agencies/{self.agency.id}/',
                    {'phone_number': '+3906123456'},
                    format='json',
                ),
                'expected': {'admin': 200, 'lawyer': 403, 'paralegal': 403, 'client': 403},
            },
            {
                'name': 'workflow template create',
                'request': lambda: self.client.post(
                    '/api/workflow-templates/',
                    {
                        'agency': self.agency.id,
                        'visa_type': self.visa_type.id,
                        'name': 'Matrix workflow',
                        'stages': [{'key': 'intake', 'label': 'Intake'}],
                    },
                    format='json',
                ),
                'expected': {'admin': 201, 'lawyer': 403, 'paralegal': 403, 'client': 403},
            },
            {
                'name': 'user invite create',
                'request': lambda: self.client.post(
                    '/api/users/',
                    {
                        'email': f'matrix-{timezone.now().timestamp()}@example.com',
                        'role': UserRole.CLIENT,
                        'agency': self.agency.id,
                        'password': 'strongpass123',
                    },
                    format='json',
                ),
                'expected': {'admin': 201, 'lawyer': 403, 'paralegal': 403, 'client': 403},
            },
            {
                'name': 'reference data write',
                'request': lambda: self.client.post(
                    '/api/reference/policy-sources/',
                    {
                        'country': self.italy.id,
                        'name': f'Matrix source {timezone.now().timestamp()}',
                        'url': f'https://example.com/matrix-{timezone.now().timestamp()}',
                    },
                    format='json',
                ),
                'expected': {'admin': 201, 'lawyer': 403, 'paralegal': 403, 'client': 403},
            },
            {
                'name': 'case create',
                'request': lambda: self.client.post(
                    '/api/cases/',
                    self._case_payload(f'Matrix case {timezone.now().timestamp()}'),
                    format='json',
                ),
                'expected': {'admin': 201, 'lawyer': 201, 'paralegal': 201, 'client': 403},
            },
            {
                'name': 'audit list',
                'request': lambda: self.client.get('/api/audit-events/'),
                'expected': {'admin': 200, 'lawyer': 200, 'paralegal': 200, 'client': 403},
            },
            {
                'name': 'agent task list',
                'request': lambda: self.client.get('/api/agent-tasks/'),
                'expected': {'admin': 200, 'lawyer': 200, 'paralegal': 200, 'client': 403},
            },
            {
                'name': 'client profile list',
                'request': lambda: self.client.get('/api/client-profiles/'),
                'expected': {'admin': 200, 'lawyer': 200, 'paralegal': 200, 'client': 403},
            },
            {
                'name': 'client chat socket ticket',
                'request': lambda: self.client.post(
                    '/api/v1/client-chat/socket-tickets/',
                    {'case': self.case.id},
                    format='json',
                ),
                'expected': {'admin': 403, 'lawyer': 403, 'paralegal': 403, 'client': 201},
            },
        ]

        self._assert_role_matrix(checks=checks)

    def test_role_permission_matrix_for_case_document_action_deadline_controls(self):
        def review_document_request():
            document = Document.objects.create(
                case=self.case,
                document_type=self.document_type,
                uploaded_by=self.client_user,
                file='case_documents/case_1/passport.pdf',
                original_filename='passport.pdf',
            )
            return self.client.post(
                f'/api/documents/{document.id}/review/',
                {'status': Document.Status.FLAGGED_FOR_HUMAN},
                format='json',
            )

        def cancel_action_request():
            action_item = ActionItem.objects.create(
                case=self.case,
                title='Matrix action',
                target_user=self.client_user,
                created_by=self.lawyer,
            )
            return self.client.post(f'/api/action-items/{action_item.id}/cancel/')

        def complete_own_action_request():
            action_item = ActionItem.objects.create(
                case=self.case,
                title='Matrix client action',
                target_user=self.client_user,
                created_by=self.lawyer,
            )
            return self.client.post(f'/api/action-items/{action_item.id}/complete/')

        def resolve_deadline_request():
            deadline = Deadline.objects.create(
                case=self.case,
                title='Matrix deadline',
                target_user=self.client_user,
                due_date=timezone.now() + timedelta(days=1),
            )
            return self.client.post(f'/api/deadlines/{deadline.id}/resolve/')

        checks = [
            {
                'name': 'assign lawyer',
                'request': lambda: self.client.post(
                    f'/api/cases/{self.case.id}/assign-lawyer/',
                    {'lawyer_id': self.lawyer.id},
                    format='json',
                ),
                'expected': {'admin': 200, 'lawyer': 200, 'paralegal': 403, 'client': 403},
            },
            {
                'name': 'generate checklist',
                'request': lambda: self.client.post(f'/api/cases/{self.case.id}/generate-checklist/'),
                'expected': {'admin': 200, 'lawyer': 200, 'paralegal': 200, 'client': 403},
            },
            {
                'name': 'document review',
                'request': review_document_request,
                'expected': {'admin': 200, 'lawyer': 200, 'paralegal': 403, 'client': 403},
            },
            {
                'name': 'action cancel',
                'request': cancel_action_request,
                'expected': {'admin': 200, 'lawyer': 200, 'paralegal': 200, 'client': 403},
            },
            {
                'name': 'client own action complete',
                'request': complete_own_action_request,
                'expected': {'admin': 200, 'lawyer': 200, 'paralegal': 200, 'client': 200},
            },
            {
                'name': 'deadline resolve',
                'request': resolve_deadline_request,
                'expected': {'admin': 200, 'lawyer': 200, 'paralegal': 200, 'client': 403},
            },
            {
                'name': 'case analyze',
                'request': lambda: self.client.post(f'/api/cases/{self.case.id}/analyze/'),
                'expected': {'admin': 200, 'lawyer': 200, 'paralegal': 200, 'client': 403},
            },
            {
                'name': 'case supervise',
                'request': lambda: self.client.post(f'/api/cases/{self.case.id}/supervise/'),
                'expected': {'admin': 200, 'lawyer': 200, 'paralegal': 200, 'client': 403},
            },
        ]

        self._assert_role_matrix(checks=checks)

    def test_client_only_sees_own_cases(self):
        self.client.force_authenticate(user=self.client_user)

        response = self.client.get('/api/cases/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['count'], 1)
        self.assertEqual(response.data['results'][0]['id'], self.case.id)

    def test_agency_staff_only_sees_own_agency_cases(self):
        self.client.force_authenticate(user=self.lawyer)

        response = self.client.get('/api/cases/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['count'], 1)
        self.assertEqual(response.data['results'][0]['id'], self.case.id)

    def test_staff_can_summarize_policy_snapshot(self):
        source = PolicySource.objects.create(
            country=self.italy,
            name='Italy student visa source',
            url='https://example.com/italy-summary-api',
        )
        snapshot = PolicySnapshot.objects.create(
            source=source,
            content_hash='b' * 64,
            raw_text='Students must provide a valid passport and enrollment confirmation.',
            raw_metadata={'provider': 'fake'},
            fetched_at=timezone.now(),
        )
        self.client.force_authenticate(user=self.lawyer)

        response = self.client.post(f'/api/reference/policy-snapshots/{snapshot.id}/summarize/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['task']['status'], AgentTask.Status.SUCCEEDED)
        self.assertEqual(response.data['result']['provider'], 'mock')

    def test_client_cannot_read_reference_data(self):
        self.client.force_authenticate(user=self.client_user)

        response = self.client.get('/api/reference/visa-requirements/')

        self.assertEqual(response.status_code, 403)

    def test_agency_staff_can_read_reference_data(self):
        self.client.force_authenticate(user=self.lawyer)

        response = self.client.get('/api/reference/visa-requirements/')

        self.assertEqual(response.status_code, 200)
        self.assertGreaterEqual(response.data['count'], 1)

    def test_reference_data_writes_are_admin_only(self):
        self.client.force_authenticate(user=self.lawyer)

        lawyer_response = self.client.post(
            '/api/reference/policy-sources/',
            {
                'country': self.italy.id,
                'name': 'Lawyer source attempt',
                'url': 'https://example.com/lawyer-source-attempt',
            },
            format='json',
        )

        self.client.force_authenticate(user=self.admin_user)
        admin_response = self.client.post(
            '/api/reference/policy-sources/',
            {
                'country': self.italy.id,
                'name': 'Admin source',
                'url': 'https://example.com/admin-source',
            },
            format='json',
        )

        self.assertEqual(lawyer_response.status_code, 403)
        self.assertEqual(admin_response.status_code, 201)

    def test_case_filter_search_and_ordering(self):
        self.client.force_authenticate(user=self.lawyer)

        filtered = self.client.get(f'/api/cases/?status=ACTIVE&search=Agency A&ordering=title')

        self.assertEqual(filtered.status_code, 200)
        self.assertEqual(filtered.data['count'], 1)
        self.assertEqual(filtered.data['results'][0]['id'], self.case.id)

    def test_openapi_schema_and_swagger_docs_are_available(self):
        self.client.force_authenticate(user=self.lawyer)

        schema_response = self.client.get('/api/schema/')
        docs_response = self.client.get('/api/docs/')
        versioned_schema_response = self.client.get('/api/v1/schema/')

        self.assertEqual(schema_response.status_code, 200)
        self.assertEqual(docs_response.status_code, 200)
        self.assertEqual(versioned_schema_response.status_code, 200)

    def test_api_version_metadata_and_v1_alias_are_available(self):
        version_response = self.client.get('/api/version/')
        versioned_version_response = self.client.get('/api/v1/version/')
        self.client.force_authenticate(user=self.lawyer)
        versioned_cases_response = self.client.get('/api/v1/cases/')

        self.assertEqual(version_response.status_code, 200)
        self.assertEqual(versioned_version_response.status_code, 200)
        self.assertEqual(version_response.data['api']['current_version'], 'v1')
        self.assertEqual(version_response.data['api']['base_path'], '/api/v1/')
        self.assertEqual(versioned_version_response.data['agents']['protocol_version'], AGENT_PROTOCOL_VERSION)
        self.assertEqual(versioned_cases_response.status_code, 200)
        self.assertEqual(versioned_cases_response.data['count'], 1)

    def test_jwt_login_uses_email_credentials(self):
        response = self.client.post(
            '/api/token/',
            {'email': 'client-a@example.com', 'password': 'testpass123'},
            format='json',
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn('access', response.data)
        self.assertIn('refresh', response.data)

    def test_jwt_refresh_rotates_and_blacklists_old_refresh_token(self):
        login_response = self.client.post(
            '/api/token/',
            {'email': 'client-a@example.com', 'password': 'testpass123'},
            format='json',
        )
        old_refresh = login_response.data['refresh']

        refresh_response = self.client.post('/api/token/refresh/', {'refresh': old_refresh}, format='json')
        old_refresh_reuse_response = self.client.post('/api/token/refresh/', {'refresh': old_refresh}, format='json')

        self.assertEqual(refresh_response.status_code, 200)
        self.assertIn('access', refresh_response.data)
        self.assertIn('refresh', refresh_response.data)
        self.assertNotEqual(refresh_response.data['refresh'], old_refresh)
        self.assertEqual(old_refresh_reuse_response.status_code, 401)

    def test_logout_blacklists_refresh_token(self):
        login_response = self.client.post(
            '/api/token/',
            {'email': 'client-a@example.com', 'password': 'testpass123'},
            format='json',
        )
        refresh = login_response.data['refresh']

        logout_response = self.client.post('/api/token/logout/', {'refresh': refresh}, format='json')
        refresh_response = self.client.post('/api/token/refresh/', {'refresh': refresh}, format='json')

        self.assertEqual(logout_response.status_code, 200)
        self.assertEqual(refresh_response.status_code, 401)

    def test_inactive_user_access_token_is_rejected(self):
        login_response = self.client.post(
            '/api/token/',
            {'email': 'client-a@example.com', 'password': 'testpass123'},
            format='json',
        )
        access = login_response.data['access']
        self.client_user.is_active = False
        self.client_user.save(update_fields=['is_active'])

        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {access}')
        response = self.client.get('/api/users/me/')

        self.assertEqual(response.status_code, 401)

    def test_password_change_invalidates_existing_access_token(self):
        login_response = self.client.post(
            '/api/token/',
            {'email': 'client-a@example.com', 'password': 'testpass123'},
            format='json',
        )
        access = login_response.data['access']
        self.client_user.set_password('new-testpass123')
        self.client_user.save(update_fields=['password'])

        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {access}')
        response = self.client.get('/api/users/me/')

        self.assertEqual(response.status_code, 401)

    def test_staff_creates_case_with_requirement_checklist(self):
        new_client = User.objects.create_user(
            email='new-client@example.com',
            password='testpass123',
            role=UserRole.CLIENT,
            agency=self.agency,
        )
        self.client.force_authenticate(user=self.lawyer)

        response = self.client.post(
            '/api/cases/',
            {
                'agency': self.agency.id,
                'client': new_client.id,
                'assigned_lawyer': self.lawyer.id,
                'origin_country': self.iran.id,
                'destination_country': self.italy.id,
                'visa_type': self.visa_type.id,
                'title': 'New Italy student visa case',
                'intake_summary': 'Initial case intake.',
            },
            format='json',
        )

        self.assertEqual(response.status_code, 201)
        case = StudentCase.objects.get(id=response.data['id'])
        self.assertEqual(case.workflow_config_snapshot['source'], 'active_visa_requirements')
        self.assertEqual(len(case.workflow_config_snapshot['requirements']), 10)
        self.assertEqual(ActionItem.objects.filter(case=case, origin=ActionItem.Origin.WORKFLOW).count(), 10)
        self.assertEqual(Notification.objects.filter(recipient_user=new_client).count(), 10)
        self.assertTrue(AuditEvent.objects.filter(object_model='StudentCase', object_id=str(case.id)).exists())

    def test_client_cannot_create_case(self):
        self.client.force_authenticate(user=self.client_user)

        response = self.client.post(
            '/api/cases/',
            {
                'agency': self.agency.id,
                'client': self.client_user.id,
                'origin_country': self.iran.id,
                'destination_country': self.italy.id,
                'visa_type': self.visa_type.id,
                'title': 'Forbidden case',
            },
            format='json',
        )

        self.assertEqual(response.status_code, 403)

    def test_checklist_generation_is_idempotent(self):
        self.client.force_authenticate(user=self.lawyer)

        first = self.client.post(f'/api/cases/{self.case.id}/generate-checklist/')
        second = self.client.post(f'/api/cases/{self.case.id}/generate-checklist/')

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(len(first.data['created_action_item_ids']), 10)
        self.assertEqual(len(second.data['created_action_item_ids']), 0)

    def test_assign_lawyer_endpoint_updates_case_and_audit(self):
        replacement_lawyer = User.objects.create_user(
            email='replacement-lawyer@example.com',
            password='testpass123',
            role=UserRole.LAWYER,
            agency=self.agency,
        )
        self.client.force_authenticate(user=self.lawyer)

        response = self.client.post(
            f'/api/cases/{self.case.id}/assign-lawyer/',
            {'lawyer_id': replacement_lawyer.id},
            format='json',
        )

        self.assertEqual(response.status_code, 200)
        self.case.refresh_from_db()
        self.assertEqual(self.case.assigned_lawyer, replacement_lawyer)
        self.assertTrue(
            AuditEvent.objects.filter(
                object_model='StudentCase',
                object_id=str(self.case.id),
                after__assigned_lawyer_id=replacement_lawyer.id,
            ).exists()
        )

    def test_document_review_updates_status_and_creates_followup(self):
        document = Document.objects.create(
            case=self.case,
            document_type=self.document_type,
            uploaded_by=self.client_user,
            file='case_documents/case_1/passport.pdf',
            original_filename='passport.pdf',
        )
        self.client.force_authenticate(user=self.lawyer)

        response = self.client.post(
            f'/api/documents/{document.id}/review/',
            {
                'status': Document.Status.REJECTED,
                'review_metadata': {'reason': 'Unreadable scan'},
                'follow_up_title': 'Upload a clearer passport scan',
                'follow_up_description': 'The current scan is not readable.',
            },
            format='json',
        )

        self.assertEqual(response.status_code, 200)
        document.refresh_from_db()
        self.assertEqual(document.status, Document.Status.REJECTED)
        self.assertEqual(document.reviewed_by, self.lawyer)
        self.assertEqual(document.review_metadata['reason'], 'Unreadable scan')
        self.assertIsNotNone(response.data['created_action_item_id'])
        self.assertTrue(
            Notification.objects.filter(
                recipient_user=self.client_user,
                notification_type=Notification.Type.DOCUMENT_REJECTED,
                related_document=document,
            ).exists()
        )
        self.assertTrue(
            Notification.objects.filter(
                recipient_user=self.client_user,
                notification_type=Notification.Type.ACTION_CREATED,
                related_document=document,
            ).exists()
        )
        self.assertTrue(AuditEvent.objects.filter(object_model='Document', object_id=str(document.id)).exists())

    def test_paralegal_cannot_review_documents(self):
        document = Document.objects.create(
            case=self.case,
            document_type=self.document_type,
            uploaded_by=self.client_user,
            file='case_documents/case_1/passport.pdf',
            original_filename='passport.pdf',
        )
        self.client.force_authenticate(user=self.paralegal)

        response = self.client.post(
            f'/api/documents/{document.id}/review/',
            {'status': Document.Status.VERIFIED_BY_HUMAN},
            format='json',
        )

        self.assertEqual(response.status_code, 403)
        document.refresh_from_db()
        self.assertEqual(document.status, Document.Status.UPLOADED)

    def test_dashboards_are_role_specific(self):
        ActionItem.objects.create(
            case=self.case,
            title='Client pending task',
            target_user=self.client_user,
            created_by=self.lawyer,
        )
        self.client.force_authenticate(user=self.lawyer)
        staff_response = self.client.get('/api/dashboard/staff/')

        self.client.force_authenticate(user=self.client_user)
        client_response = self.client.get('/api/dashboard/client/')

        self.assertEqual(staff_response.status_code, 200)
        self.assertEqual(staff_response.data['active_cases'], 1)
        self.assertEqual(client_response.status_code, 200)
        self.assertEqual(client_response.data['active_cases'], 1)
        self.assertEqual(client_response.data['pending_actions'], 1)

    def test_me_endpoint_returns_current_user(self):
        self.client.force_authenticate(user=self.client_user)

        response = self.client.get('/api/users/me/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['email'], self.client_user.email)

    def test_admin_creates_client_user_with_profile_and_default_agency(self):
        self.client.force_authenticate(user=self.admin_user)

        response = self.client.post(
            '/api/users/',
            {
                'email': 'created-client@example.com',
                'first_name': 'Created',
                'last_name': 'Client',
                'role': UserRole.CLIENT,
                'password': 'strongpass123',
            },
            format='json',
        )

        self.assertEqual(response.status_code, 201)
        user = User.objects.get(email='created-client@example.com')
        self.assertEqual(user.agency, self.agency)
        self.assertTrue(user.check_password('strongpass123'))
        self.assertTrue(ClientProfile.objects.filter(user=user).exists())

    def test_admin_creates_lawyer_user_with_profile(self):
        self.client.force_authenticate(user=self.admin_user)

        response = self.client.post(
            '/api/users/',
            {
                'email': 'created-lawyer@example.com',
                'role': UserRole.LAWYER,
                'agency': self.agency.id,
                'password': 'strongpass123',
            },
            format='json',
        )

        self.assertEqual(response.status_code, 201)
        user = User.objects.get(email='created-lawyer@example.com')
        self.assertTrue(LawyerProfile.objects.filter(user=user).exists())

    def test_lawyer_cannot_create_users(self):
        self.client.force_authenticate(user=self.lawyer)

        response = self.client.post(
            '/api/users/',
            {
                'email': 'lawyer-created-client@example.com',
                'role': UserRole.CLIENT,
                'agency': self.agency.id,
                'password': 'strongpass123',
            },
            format='json',
        )

        self.assertEqual(response.status_code, 403)

    def test_agency_admin_cannot_create_admin_user(self):
        self.client.force_authenticate(user=self.admin_user)

        response = self.client.post(
            '/api/users/',
            {
                'email': 'admin-attempt@example.com',
                'role': UserRole.ADMIN,
                'agency': self.agency.id,
                'password': 'strongpass123',
            },
            format='json',
        )

        self.assertEqual(response.status_code, 400)

    def test_lawyer_cannot_change_own_role(self):
        self.client.force_authenticate(user=self.lawyer)

        response = self.client.patch(
            f'/api/users/{self.lawyer.id}/',
            {'role': UserRole.CLIENT},
            format='json',
        )

        self.assertEqual(response.status_code, 403)
        self.lawyer.refresh_from_db()
        self.assertEqual(self.lawyer.role, UserRole.LAWYER)

    def test_document_upload_rejects_unsupported_extension(self):
        self.client.force_authenticate(user=self.client_user)
        upload = SimpleUploadedFile('passport.txt', b'not a valid document', content_type='text/plain')

        response = self.client.post(
            '/api/documents/',
            {
                'case': self.case.id,
                'document_type': self.document_type.id,
                'file': upload,
            },
            format='multipart',
        )

        self.assertEqual(response.status_code, 400)
        self.assertTrue(
            AuditEvent.objects.filter(
                event_type=AuditEvent.EventType.DOCUMENT_SECURITY_REJECTED,
                object_model='DocumentUpload',
                object_id=str(self.case.id),
            ).exists()
        )

    def test_document_upload_rejects_mismatched_content_signature(self):
        self.client.force_authenticate(user=self.client_user)
        upload = SimpleUploadedFile('passport.pdf', b'not really a pdf', content_type='application/pdf')

        response = self.client.post(
            '/api/documents/',
            {
                'case': self.case.id,
                'document_type': self.document_type.id,
                'file': upload,
            },
            format='multipart',
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn('file', response.data)

    @override_settings(DOCUMENT_MAX_UPLOAD_SIZE_BYTES=8)
    def test_document_upload_rejects_oversized_file(self):
        self.client.force_authenticate(user=self.client_user)
        upload = SimpleUploadedFile('passport.pdf', b'%PDF-1.4 too large', content_type='application/pdf')

        response = self.client.post(
            '/api/documents/',
            {
                'case': self.case.id,
                'document_type': self.document_type.id,
                'file': upload,
            },
            format='multipart',
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn('file', response.data)

    def test_document_upload_rejects_mock_malware_detection(self):
        self.client.force_authenticate(user=self.client_user)
        upload = SimpleUploadedFile(
            'passport.pdf',
            b'%PDF-1.4 X5O!P%@AP EICAR-STANDARD-ANTIVIRUS-TEST-FILE',
            content_type='application/pdf',
        )

        response = self.client.post(
            '/api/documents/',
            {
                'case': self.case.id,
                'document_type': self.document_type.id,
                'file': upload,
            },
            format='multipart',
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn('file', response.data)

    def test_document_upload_sets_metadata_and_ignores_review_fields(self):
        self.client.force_authenticate(user=self.client_user)
        upload = SimpleUploadedFile('my passport.pdf', b'%PDF-1.4 fake', content_type='application/pdf')

        with patch('documents.views.queue_document_scan') as queue_document_scan:
            response = self.client.post(
                '/api/documents/',
                {
                    'case': self.case.id,
                    'document_type': self.document_type.id,
                    'file': upload,
                    'status': Document.Status.VERIFIED_BY_HUMAN,
                    'confidence_score': '99.00',
                    'review_metadata': '{"bad": true}',
                },
                format='multipart',
            )

        self.assertEqual(response.status_code, 201)
        queue_document_scan.assert_called_once()
        document = Document.objects.get(id=response.data['id'])
        self.assertEqual(document.status, Document.Status.UPLOADED)
        self.assertEqual(document.source, 'client_upload')
        self.assertEqual(document.original_filename, 'my_passport.pdf')
        self.assertNotEqual(document.file.name, f'case_documents/case_{self.case.id}/my_passport.pdf')
        self.assertTrue(document.file.name.startswith(f'case_documents/case_{self.case.id}/'))
        self.assertEqual(document.file_metadata['upload_security']['content_signature'], 'pdf')
        self.assertEqual(document.file_metadata['upload_security']['malware_scan']['status'], 'clean')
        self.assertIsNone(document.confidence_score)
        self.assertEqual(document.review_metadata, {})
        self.assertTrue(
            AuditEvent.objects.filter(
                event_type=AuditEvent.EventType.DOCUMENT_UPLOADED,
                object_model='Document',
                object_id=str(document.id),
            ).exists()
        )

    def test_client_notification_list_unread_count_and_mark_read(self):
        notification = Notification.objects.create(
            agency=self.agency,
            recipient_user=self.client_user,
            title='Upload passport',
            message='Please upload your passport.',
            notification_type=Notification.Type.ACTION_CREATED,
            related_case=self.case,
        )
        Notification.objects.create(
            agency=self.other_agency,
            recipient_user=self.other_client,
            title='Other agency notification',
            notification_type=Notification.Type.SYSTEM,
            related_case=self.other_case,
        )
        self.client.force_authenticate(user=self.client_user)

        list_response = self.client.get('/api/notifications/')
        count_response = self.client.get('/api/notifications/unread-count/')
        mark_response = self.client.post(f'/api/notifications/{notification.id}/mark-read/')
        mark_all_response = self.client.post('/api/notifications/mark-all-read/')

        self.assertEqual(list_response.status_code, 200)
        self.assertEqual(list_response.data['count'], 1)
        self.assertEqual(count_response.status_code, 200)
        self.assertEqual(count_response.data['unread_count'], 1)
        self.assertEqual(mark_response.status_code, 200)
        notification.refresh_from_db()
        self.assertTrue(notification.is_read)
        self.assertIsNotNone(notification.read_at)
        self.assertEqual(mark_all_response.status_code, 200)

    def test_staff_sees_agency_notifications_but_cannot_mark_client_notification_read(self):
        notification = Notification.objects.create(
            agency=self.agency,
            recipient_user=self.client_user,
            title='Client notification',
            notification_type=Notification.Type.SYSTEM,
            related_case=self.case,
        )
        self.client.force_authenticate(user=self.lawyer)

        list_response = self.client.get('/api/notifications/')
        mark_response = self.client.post(f'/api/notifications/{notification.id}/mark-read/')

        self.assertEqual(list_response.status_code, 200)
        self.assertEqual(list_response.data['count'], 1)
        self.assertEqual(mark_response.status_code, 403)

    def test_client_can_complete_own_action_item_but_cannot_cancel_it(self):
        action_item = ActionItem.objects.create(
            case=self.case,
            title='Upload passport',
            target_user=self.client_user,
            created_by=self.lawyer,
        )
        self.client.force_authenticate(user=self.client_user)

        complete_response = self.client.post(f'/api/action-items/{action_item.id}/complete/')
        cancel_response = self.client.post(f'/api/action-items/{action_item.id}/cancel/')

        self.assertEqual(complete_response.status_code, 200)
        action_item.refresh_from_db()
        self.assertEqual(action_item.status, ActionItem.Status.COMPLETED)
        self.assertIsNotNone(action_item.completed_at)
        self.assertTrue(
            Notification.objects.filter(
                recipient_user=self.lawyer,
                notification_type=Notification.Type.ACTION_COMPLETED,
                related_action_item=action_item,
            ).exists()
        )
        self.assertEqual(cancel_response.status_code, 403)

    def test_staff_can_cancel_action_item(self):
        action_item = ActionItem.objects.create(
            case=self.case,
            title='Upload passport',
            target_user=self.client_user,
            created_by=self.lawyer,
        )
        self.client.force_authenticate(user=self.lawyer)

        response = self.client.post(f'/api/action-items/{action_item.id}/cancel/')

        self.assertEqual(response.status_code, 200)
        action_item.refresh_from_db()
        self.assertEqual(action_item.status, ActionItem.Status.CANCELLED)
        self.assertTrue(
            Notification.objects.filter(
                recipient_user=self.client_user,
                notification_type=Notification.Type.ACTION_CANCELLED,
                related_action_item=action_item,
            ).exists()
        )

    def test_client_cannot_read_audit_events(self):
        AuditEvent.objects.create(
            agency=self.agency,
            actor=self.client_user,
            event_type=AuditEvent.EventType.CREATED,
            object_app_label='cases',
            object_model='StudentCase',
            object_id=str(self.case.id),
        )
        self.client.force_authenticate(user=self.client_user)

        response = self.client.get('/api/audit-events/')

        self.assertEqual(response.status_code, 403)

    def test_staff_can_resolve_deadline(self):
        deadline = Deadline.objects.create(
            case=self.case,
            title='Submit passport',
            target_user=self.client_user,
            due_date=timezone.now(),
        )
        self.client.force_authenticate(user=self.lawyer)

        response = self.client.post(f'/api/deadlines/{deadline.id}/resolve/')

        self.assertEqual(response.status_code, 200)
        deadline.refresh_from_db()
        self.assertTrue(deadline.is_resolved)
        self.assertIsNotNone(deadline.resolved_at)
        self.assertTrue(
            Notification.objects.filter(
                recipient_user=self.client_user,
                notification_type=Notification.Type.DEADLINE_RESOLVED,
                related_deadline=deadline,
            ).exists()
        )

    def test_paralegal_can_create_case_but_cannot_change_case_stage(self):
        new_client = User.objects.create_user(
            email='paralegal-client@example.com',
            password='testpass123',
            role=UserRole.CLIENT,
            agency=self.agency,
        )
        self.client.force_authenticate(user=self.paralegal)

        create_response = self.client.post(
            '/api/cases/',
            {
                'agency': self.agency.id,
                'client': new_client.id,
                'origin_country': self.iran.id,
                'destination_country': self.italy.id,
                'visa_type': self.visa_type.id,
                'title': 'Paralegal intake case',
            },
            format='json',
        )

        self.assertEqual(create_response.status_code, 201)
        stage_response = self.client.patch(
            f'/api/cases/{create_response.data["id"]}/',
            {'current_stage': 'review'},
            format='json',
        )

        self.assertEqual(stage_response.status_code, 400)
        created_case = StudentCase.objects.get(id=create_response.data['id'])
        self.assertEqual(created_case.current_stage, 'intake')

    def test_staff_can_analyze_case(self):
        ActionItem.objects.create(
            case=self.case,
            title='Upload passport',
            target_user=self.client_user,
            created_by=self.lawyer,
            due_date=timezone.now() - timedelta(days=1),
        )
        Deadline.objects.create(
            case=self.case,
            title='Passport upload deadline',
            target_user=self.client_user,
            due_date=timezone.now() - timedelta(days=1),
        )
        self.client.force_authenticate(user=self.lawyer)

        response = self.client.post(f'/api/cases/{self.case.id}/analyze/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['task']['task_type'], AgentTask.TaskType.CASE_ANALYSIS)
        self.assertEqual(response.data['task']['status'], AgentTask.Status.SUCCEEDED)
        self.assertEqual(response.data['result']['provider'], 'mock')
        self.assertTrue(response.data['result']['requires_human_review'])
        self.assertIn('overdue deadline', response.data['result']['summary'])
        self.case.refresh_from_db()
        self.assertEqual(self.case.current_stage, 'intake')

    def test_agent_context_builder_builds_case_analysis_context(self):
        ActionItem.objects.create(
            case=self.case,
            title='Upload passport',
            target_user=self.client_user,
            created_by=self.lawyer,
        )
        Deadline.objects.create(
            case=self.case,
            title='Passport upload deadline',
            target_user=self.client_user,
            due_date=timezone.now() - timedelta(days=1),
        )

        context = AgentContextBuilder().build_case_analysis_context(case=self.case)

        self.assertEqual(context['schema_version'], AGENT_CONTEXT_SCHEMA_VERSION)
        self.assertEqual(context['context_kind'], 'case_analysis')
        self.assertEqual(context['case']['id'], self.case.id)
        self.assertEqual(context['case']['current_stage'], 'intake')
        self.assertEqual(len(context['action_items']), 1)
        self.assertEqual(len(context['deadlines']), 1)
        self.assertTrue(context['deadlines'][0]['is_overdue'])
        self.assertIn('workflow', context)

    def test_agent_context_builder_caps_document_text(self):
        document = Document.objects.create(
            case=self.case,
            document_type=self.document_type,
            uploaded_by=self.client_user,
            file='case_documents/case_1/passport.pdf',
            original_filename='passport.pdf',
            extraction_status=Document.ExtractionStatus.COMPLETED,
            extracted_text='A' * 100,
        )

        context = AgentContextBuilder().build_document_analysis_context(document=document, max_text_chars=12)

        self.assertEqual(context['schema_version'], AGENT_CONTEXT_SCHEMA_VERSION)
        self.assertEqual(context['context_kind'], 'document_analysis')
        self.assertEqual(context['document']['id'], document.id)
        self.assertEqual(context['case']['id'], self.case.id)
        self.assertEqual(context['extracted_text'], 'A' * 12)

    def test_client_cannot_analyze_case(self):
        self.client.force_authenticate(user=self.client_user)

        response = self.client.post(f'/api/cases/{self.case.id}/analyze/')

        self.assertEqual(response.status_code, 403)

    def test_staff_can_supervise_case(self):
        Document.objects.create(
            case=self.case,
            document_type=self.document_type,
            uploaded_by=self.client_user,
            file='case_documents/case_1/passport.pdf',
            original_filename='passport.pdf',
            extraction_status=Document.ExtractionStatus.COMPLETED,
            extracted_text='Passport number AB123456',
        )
        self.client.force_authenticate(user=self.lawyer)

        response = self.client.post(f'/api/cases/{self.case.id}/supervise/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['task']['task_type'], AgentTask.TaskType.SUPERVISOR_CASE_REVIEW)
        self.assertEqual(response.data['task']['status'], AgentTask.Status.SUCCEEDED)
        self.assertEqual(response.data['task']['agent_protocol_version'], AGENT_PROTOCOL_VERSION)
        self.assertEqual(response.data['task']['context_schema_version'], AGENT_CONTEXT_SCHEMA_VERSION)
        self.assertEqual(response.data['result']['provider'], 'mock')
        self.assertEqual(response.data['result']['output_schema_version'], AGENT_OUTPUT_SCHEMA_VERSION)
        self.assertTrue(response.data['result']['requires_human_review'])
        self.assertIn('CASE_ANALYSIS', response.data['result']['output_payload']['execution_order'])
        self.assertIn('DOCUMENT_ANALYSIS', response.data['result']['output_payload']['execution_order'])
        self.assertEqual(AgentTask.objects.filter(task_type=AgentTask.TaskType.SUPERVISOR_CASE_REVIEW).count(), 1)
        self.assertEqual(AgentTask.objects.filter(task_type=AgentTask.TaskType.CASE_ANALYSIS).count(), 1)
        self.assertEqual(AgentTask.objects.filter(task_type=AgentTask.TaskType.DOCUMENT_ANALYSIS).count(), 1)

    def test_client_cannot_supervise_case(self):
        self.client.force_authenticate(user=self.client_user)

        response = self.client.post(f'/api/cases/{self.case.id}/supervise/')

        self.assertEqual(response.status_code, 403)

    def test_client_case_chat_service_is_client_only(self):
        with self.assertRaises(AgentServiceError):
            chat_with_client_case(case=self.case, actor=self.lawyer, message='What is my next step?')

        task, result = chat_with_client_case(case=self.case, actor=self.client_user, message='What is my next step?')

        self.assertEqual(task.task_type, AgentTask.TaskType.CLIENT_CASE_CHAT)
        self.assertEqual(task.status, AgentTask.Status.SUCCEEDED)
        self.assertEqual(task.agent_protocol_version, AGENT_PROTOCOL_VERSION)
        self.assertEqual(task.context_schema_version, AGENT_CONTEXT_SCHEMA_VERSION)
        self.assertEqual(result.provider, 'mock')
        self.assertEqual(result.output_schema_version, AGENT_OUTPUT_SCHEMA_VERSION)
        self.assertIn('answer', result.output_payload)
        self.assertIn('visible_case_context', result.output_payload)

    def test_agent_context_builder_client_chat_context_is_client_visible_only(self):
        Document.objects.create(
            case=self.case,
            document_type=self.document_type,
            uploaded_by=self.client_user,
            file='case_documents/case_1/passport.pdf',
            original_filename='passport.pdf',
            extraction_status=Document.ExtractionStatus.COMPLETED,
            extracted_text='Sensitive extracted passport text',
        )
        ActionItem.objects.create(
            case=self.case,
            title='Client visible task',
            target_user=self.client_user,
            created_by=self.lawyer,
        )
        ActionItem.objects.create(
            case=self.case,
            title='Staff-only task',
            target_user=self.lawyer,
            created_by=self.lawyer,
        )

        context = AgentContextBuilder().build_client_case_chat_context(case=self.case, actor=self.client_user)

        self.assertEqual(context['schema_version'], AGENT_CONTEXT_SCHEMA_VERSION)
        self.assertEqual(context['context_kind'], 'client_case_chat')
        self.assertEqual(len(context['documents']), 1)
        self.assertNotIn('extracted_text', context['documents'][0])
        self.assertEqual([item['title'] for item in context['action_items']], ['Client visible task'])
        self.assertEqual(context['limits']['context_policy'], 'client_visible_case_context_only')

    def test_client_can_create_socket_ticket_for_own_case(self):
        self.client.force_authenticate(user=self.client_user)

        response = self.client.post('/api/v1/client-chat/socket-tickets/', {'case': self.case.id}, format='json')

        self.assertEqual(response.status_code, 201)
        self.assertIn('ticket', response.data)
        self.assertEqual(response.data['case_id'], self.case.id)
        self.assertEqual(response.data['protocol_version'], WEBSOCKET_PROTOCOL_VERSION)
        self.assertIn(f'/ws/v1/client/cases/{self.case.id}/chat/?ticket=', response.data['ws_path'])
        stored_ticket = ClientChatSocketTicket.objects.get(case=self.case, user=self.client_user)
        self.assertEqual(stored_ticket.status, ClientChatSocketTicket.Status.ACTIVE)
        self.assertNotEqual(stored_ticket.ticket_hash, response.data['ticket'])

    def test_client_can_chat_over_websocket_with_jwt(self):
        ActionItem.objects.create(
            case=self.case,
            title='Upload passport',
            target_user=self.client_user,
            created_by=self.lawyer,
        )
        token = str(RefreshToken.for_user(self.client_user).access_token)
        communicator = WebsocketCommunicator(
            application,
            f'/ws/client/cases/{self.case.id}/chat/?token={token}',
            headers=[(b'host', b'localhost'), (b'origin', b'http://localhost')],
        )

        async def run_socket_chat():
            connected, close_code = await communicator.connect()
            self.assertTrue(connected, close_code)
            accepted_payload = await communicator.receive_json_from()
            self.assertEqual(accepted_payload['type'], 'connection.accepted')
            self.assertEqual(accepted_payload['protocol_version'], WEBSOCKET_PROTOCOL_VERSION)

            await communicator.send_json_to({'message': 'What should I do next?'})

            started_payload = await communicator.receive_json_from()
            completed_payload = await communicator.receive_json_from()
            await communicator.disconnect()
            return started_payload, completed_payload

        started_payload, completed_payload = async_to_sync(run_socket_chat)()
        self.assertEqual(started_payload['type'], 'chat.started')
        self.assertEqual(started_payload['protocol_version'], WEBSOCKET_PROTOCOL_VERSION)
        self.assertEqual(completed_payload['type'], 'chat.completed')
        self.assertEqual(completed_payload['protocol_version'], WEBSOCKET_PROTOCOL_VERSION)
        self.assertIn('answer', completed_payload)
        self.assertIn('task_id', completed_payload)
        self.assertEqual(
            AgentTask.objects.get(id=completed_payload['task_id']).task_type,
            AgentTask.TaskType.CLIENT_CASE_CHAT,
        )

    def test_client_can_chat_over_v1_websocket_with_socket_ticket(self):
        self.client.force_authenticate(user=self.client_user)
        ticket_response = self.client.post('/api/v1/client-chat/socket-tickets/', {'case': self.case.id}, format='json')
        raw_ticket = ticket_response.data['ticket']
        communicator = WebsocketCommunicator(
            application,
            f'/ws/v1/client/cases/{self.case.id}/chat/?ticket={raw_ticket}',
            headers=[(b'host', b'localhost'), (b'origin', b'http://localhost')],
        )

        async def run_socket_connect():
            connected, close_code = await communicator.connect()
            self.assertTrue(connected, close_code)
            accepted_payload = await communicator.receive_json_from()
            await communicator.disconnect()
            return accepted_payload

        accepted_payload = async_to_sync(run_socket_connect)()
        self.assertEqual(accepted_payload['type'], 'connection.accepted')
        self.assertEqual(accepted_payload['protocol_version'], WEBSOCKET_PROTOCOL_VERSION)
        stored_ticket = ClientChatSocketTicket.objects.get(case=self.case, user=self.client_user)
        self.assertEqual(stored_ticket.status, ClientChatSocketTicket.Status.USED)

    def test_v1_websocket_rejects_missing_or_reused_socket_ticket(self):
        missing_ticket_communicator = WebsocketCommunicator(
            application,
            f'/ws/v1/client/cases/{self.case.id}/chat/',
            headers=[(b'host', b'localhost'), (b'origin', b'http://localhost')],
        )

        async def run_missing_ticket_connect():
            return await missing_ticket_communicator.connect()

        connected, _subprotocol = async_to_sync(run_missing_ticket_connect)()
        self.assertFalse(connected)

        self.client.force_authenticate(user=self.client_user)
        ticket_response = self.client.post('/api/v1/client-chat/socket-tickets/', {'case': self.case.id}, format='json')
        raw_ticket = ticket_response.data['ticket']

        async def connect_once():
            communicator = WebsocketCommunicator(
                application,
                f'/ws/v1/client/cases/{self.case.id}/chat/?ticket={raw_ticket}',
                headers=[(b'host', b'localhost'), (b'origin', b'http://localhost')],
            )
            connected, close_code = await communicator.connect()
            self.assertTrue(connected, close_code)
            await communicator.receive_json_from()
            await communicator.disconnect()

        async_to_sync(connect_once)()

        reused_ticket_communicator = WebsocketCommunicator(
            application,
            f'/ws/v1/client/cases/{self.case.id}/chat/?ticket={raw_ticket}',
            headers=[(b'host', b'localhost'), (b'origin', b'http://localhost')],
        )

        async def run_reused_ticket_connect():
            return await reused_ticket_communicator.connect()

        connected, _subprotocol = async_to_sync(run_reused_ticket_connect)()
        self.assertFalse(connected)

    @override_settings(CLIENT_CHAT_WS_MAX_MESSAGES_PER_MINUTE=1)
    def test_client_chat_websocket_rate_limits_messages(self):
        token = str(RefreshToken.for_user(self.client_user).access_token)
        communicator = WebsocketCommunicator(
            application,
            f'/ws/client/cases/{self.case.id}/chat/?token={token}',
            headers=[(b'host', b'localhost'), (b'origin', b'http://localhost')],
        )

        async def run_socket_chat():
            connected, close_code = await communicator.connect()
            self.assertTrue(connected, close_code)
            await communicator.receive_json_from()
            await communicator.send_json_to({'message': 'First message'})
            await communicator.receive_json_from()
            await communicator.receive_json_from()
            await communicator.send_json_to({'message': 'Second message'})
            error_payload = await communicator.receive_json_from()
            await communicator.disconnect()
            return error_payload

        error_payload = async_to_sync(run_socket_chat)()
        self.assertEqual(error_payload['type'], 'error')
        self.assertEqual(error_payload['code'], 'rate_limited')

    def test_staff_cannot_connect_to_client_chat_websocket(self):
        token = str(RefreshToken.for_user(self.lawyer).access_token)
        communicator = WebsocketCommunicator(
            application,
            f'/ws/client/cases/{self.case.id}/chat/?token={token}',
            headers=[(b'host', b'localhost'), (b'origin', b'http://localhost')],
        )

        async def run_socket_connect():
            return await communicator.connect()

        connected, _subprotocol = async_to_sync(run_socket_connect)()

        self.assertFalse(connected)

    def test_document_download_uses_authenticated_document_permissions(self):
        self.client.force_authenticate(user=self.client_user)
        upload = SimpleUploadedFile('passport.pdf', b'%PDF-1.4 fake', content_type='application/pdf')
        with patch('documents.views.queue_document_scan'):
            create_response = self.client.post(
                '/api/documents/',
                {
                    'case': self.case.id,
                    'document_type': self.document_type.id,
                    'file': upload,
                },
                format='multipart',
            )
        self.assertEqual(create_response.status_code, 201)

        download_response = self.client.get(f'/api/documents/{create_response.data["id"]}/download/')

        self.assertEqual(download_response.status_code, 200)
        self.assertEqual(download_response['Content-Disposition'], 'attachment; filename="passport.pdf"')
        self.assertTrue(
            AuditEvent.objects.filter(
                event_type=AuditEvent.EventType.DOCUMENT_DOWNLOADED,
                object_model='Document',
                object_id=str(create_response.data['id']),
                actor=self.client_user,
            ).exists()
        )

        self.client.force_authenticate(user=self.other_client)
        other_client_response = self.client.get(f'/api/documents/{create_response.data["id"]}/download/')

        self.assertEqual(other_client_response.status_code, 404)

    def test_staff_can_scan_document_and_read_extraction(self):
        document = Document.objects.create(
            case=self.case,
            document_type=self.document_type,
            uploaded_by=self.client_user,
            file='case_documents/case_1/passport.pdf',
            original_filename='passport.pdf',
        )

        class FakeGateway:
            def scan(self, document):
                return ScanResult(
                    text='Student visa support letter text',
                    metadata={'provider': 'fake', 'page_count': 1},
                )

        self.client.force_authenticate(user=self.lawyer)
        with patch('documents.services.DocumentScannerGateway', return_value=FakeGateway()):
            scan_response = self.client.post(f'/api/documents/{document.id}/scan/')
        extraction_response = self.client.get(f'/api/documents/{document.id}/extraction/')

        self.assertEqual(scan_response.status_code, 200)
        self.assertEqual(scan_response.data['extraction_status'], Document.ExtractionStatus.COMPLETED)
        self.assertEqual(extraction_response.status_code, 200)
        self.assertEqual(extraction_response.data['extracted_text'], 'Student visa support letter text')
        self.assertEqual(extraction_response.data['extraction_metadata']['provider'], 'fake')

    def test_staff_can_analyze_scanned_document(self):
        document = Document.objects.create(
            case=self.case,
            document_type=self.document_type,
            uploaded_by=self.client_user,
            file='case_documents/case_1/passport.pdf',
            original_filename='passport.pdf',
            extraction_status=Document.ExtractionStatus.COMPLETED,
            extracted_text='Passport number AB123456. Date of birth 1999-01-01. Expiry 2030-01-01.',
            extraction_metadata={'provider': 'fake'},
            extracted_at=timezone.now(),
        )
        self.client.force_authenticate(user=self.lawyer)

        response = self.client.post(f'/api/documents/{document.id}/analyze/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['task']['task_type'], AgentTask.TaskType.DOCUMENT_ANALYSIS)
        self.assertEqual(response.data['task']['status'], AgentTask.Status.SUCCEEDED)
        self.assertEqual(response.data['result']['provider'], 'mock')
        self.assertTrue(response.data['result']['requires_human_review'])
        self.assertIn('Passport number', response.data['result']['summary'])

    def test_unscanned_document_cannot_be_analyzed(self):
        document = Document.objects.create(
            case=self.case,
            document_type=self.document_type,
            uploaded_by=self.client_user,
            file='case_documents/case_1/passport.pdf',
            original_filename='passport.pdf',
            extraction_status=Document.ExtractionStatus.NOT_STARTED,
        )
        self.client.force_authenticate(user=self.lawyer)

        response = self.client.post(f'/api/documents/{document.id}/analyze/')

        self.assertEqual(response.status_code, 400)
        self.assertIn('extraction must be completed', response.data['detail'])

    def test_client_cannot_analyze_document(self):
        document = Document.objects.create(
            case=self.case,
            document_type=self.document_type,
            uploaded_by=self.client_user,
            file='case_documents/case_1/passport.pdf',
            original_filename='passport.pdf',
            extraction_status=Document.ExtractionStatus.COMPLETED,
            extracted_text='Passport number AB123456.',
            extracted_at=timezone.now(),
        )
        self.client.force_authenticate(user=self.client_user)

        response = self.client.post(f'/api/documents/{document.id}/analyze/')

        self.assertEqual(response.status_code, 403)

    def test_agency_workflow_template_is_used_when_creating_case(self):
        AgencyWorkflowTemplate.objects.create(
            agency=self.agency,
            visa_type=self.visa_type,
            name='Agency Italy student workflow',
            stages=[
                {'key': 'custom_intake', 'label': 'Custom intake', 'status': 'active'},
                {'key': 'custom_review', 'label': 'Custom review', 'status': 'pending'},
            ],
            deadline_rules=[{'action': 'upload_passport', 'days_after_case_creation': 3}],
            escalation_rules=[{'if_overdue_days': 2, 'notify': 'assigned_lawyer'}],
        )
        new_client = User.objects.create_user(
            email='template-client@example.com',
            password='testpass123',
            role=UserRole.CLIENT,
            agency=self.agency,
        )
        self.client.force_authenticate(user=self.lawyer)

        response = self.client.post(
            '/api/cases/',
            {
                'agency': self.agency.id,
                'client': new_client.id,
                'assigned_lawyer': self.lawyer.id,
                'origin_country': self.iran.id,
                'destination_country': self.italy.id,
                'visa_type': self.visa_type.id,
                'title': 'Template workflow case',
            },
            format='json',
        )

        self.assertEqual(response.status_code, 201)
        case = StudentCase.objects.get(id=response.data['id'])
        self.assertEqual(case.workflow_config_snapshot['source'], 'agency_workflow_template')
        self.assertEqual(case.roadmap_state['stages'][0]['key'], 'custom_intake')
        self.assertEqual(case.roadmap_state['deadline_rules'][0]['action'], 'upload_passport')

    def test_agency_admin_creates_workflow_template_only_for_own_agency(self):
        self.client.force_authenticate(user=self.admin_user)

        own_agency_response = self.client.post(
            '/api/workflow-templates/',
            {
                'agency': self.agency.id,
                'visa_type': self.visa_type.id,
                'name': 'Default Italy student workflow',
                'stages': [{'key': 'intake', 'label': 'Intake', 'status': 'active'}],
            },
            format='json',
        )
        other_agency_response = self.client.post(
            '/api/workflow-templates/',
            {
                'agency': self.other_agency.id,
                'visa_type': self.visa_type.id,
                'name': 'Other agency workflow',
                'stages': [{'key': 'intake', 'label': 'Intake', 'status': 'active'}],
            },
            format='json',
        )

        self.assertEqual(own_agency_response.status_code, 201)
        self.assertEqual(own_agency_response.data['agency'], self.agency.id)
        self.assertEqual(other_agency_response.status_code, 400)

    def test_reminder_tasks_create_idempotent_notifications(self):
        approaching_deadline = Deadline.objects.create(
            case=self.case,
            title='Upcoming passport deadline',
            target_user=self.client_user,
            due_date=timezone.now() + timedelta(days=1),
        )
        overdue_action = ActionItem.objects.create(
            case=self.case,
            title='Overdue upload',
            target_user=self.client_user,
            created_by=self.lawyer,
            due_date=timezone.now() - timedelta(days=1),
        )
        StudentCase.objects.filter(id=self.case.id).update(updated_at=timezone.now() - timedelta(days=8))

        self.assertEqual(create_upcoming_deadline_notifications(), 1)
        self.assertEqual(create_upcoming_deadline_notifications(), 0)
        self.assertEqual(create_overdue_action_notifications(), 2)
        self.assertEqual(create_overdue_action_notifications(), 0)
        self.assertEqual(create_stalled_case_notifications(), 1)
        self.assertEqual(create_stalled_case_notifications(), 0)

        self.assertTrue(
            Notification.objects.filter(
                recipient_user=self.client_user,
                notification_type=Notification.Type.DEADLINE_APPROACHING,
                related_deadline=approaching_deadline,
            ).exists()
        )
        self.assertTrue(
            Notification.objects.filter(
                recipient_user=self.client_user,
                notification_type=Notification.Type.ACTION_OVERDUE,
                related_action_item=overdue_action,
            ).exists()
        )
        self.assertTrue(
            Notification.objects.filter(
                recipient_user=self.lawyer,
                notification_type=Notification.Type.CASE_STALLED,
                related_case=self.case,
            ).exists()
        )

# Create your tests here.
