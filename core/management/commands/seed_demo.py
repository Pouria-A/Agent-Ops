from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from actions.models import ActionItem
from agencies.models import Agency, AgencyWorkflowTemplate
from cases.models import StudentCase
from cases.services import create_case
from deadlines.models import Deadline
from reference_data.models import Country, DocumentType, PublicationStatus, VisaRequirement, VisaType
from users.models import ClientProfile, LawyerProfile, User, UserRole


DEMO_PASSWORD = 'DemoPass123!'


class Command(BaseCommand):
    help = 'Seed local demo data for the MigrationOps MVP workflow.'

    def handle(self, *args, **options):
        with transaction.atomic():
            agency = self._seed_agency()
            admin = self._seed_user(
                email='admin@migrationops.local',
                role=UserRole.ADMIN,
                agency=agency,
                first_name='Demo',
                last_name='Admin',
                is_staff=True,
                is_superuser=True,
            )
            lawyer = self._seed_user(
                email='lawyer@migrationops.local',
                role=UserRole.LAWYER,
                agency=agency,
                first_name='Leila',
                last_name='Lawyer',
            )
            paralegal = self._seed_user(
                email='paralegal@migrationops.local',
                role=UserRole.PARALEGAL,
                agency=agency,
                first_name='Peyman',
                last_name='Paralegal',
            )
            client = self._seed_user(
                email='client@migrationops.local',
                role=UserRole.CLIENT,
                agency=agency,
                first_name='Sara',
                last_name='Student',
            )

            self._seed_profiles(client=client, lawyer=lawyer)
            iran, italy, visa_type, document_types = self._seed_reference_data(admin)
            self._seed_workflow_template(agency=agency, visa_type=visa_type)
            case = self._seed_case(
                agency=agency,
                admin=admin,
                client=client,
                lawyer=lawyer,
                iran=iran,
                italy=italy,
                visa_type=visa_type,
            )
            self._seed_deadlines(case=case, lawyer=lawyer, client=client)

        self.stdout.write(self.style.SUCCESS('Demo data is ready.'))
        self.stdout.write('')
        self.stdout.write('Demo credentials:')
        for label, email in [
            ('Admin', 'admin@migrationops.local'),
            ('Lawyer', 'lawyer@migrationops.local'),
            ('Paralegal', 'paralegal@migrationops.local'),
            ('Client', 'client@migrationops.local'),
        ]:
            self.stdout.write(f'  {label}: {email} / {DEMO_PASSWORD}')
        self.stdout.write('')
        self.stdout.write(f'Agency: {agency.name}')
        self.stdout.write(f'Case: {case.title}')
        self.stdout.write('Run the frontend and login with the admin/lawyer/client accounts to test the manual MVP flow.')

    def _seed_agency(self):
        agency, _ = Agency.objects.update_or_create(
            name='MigrationOps Demo Agency',
            defaults={
                'email': 'admin@migrationops.local',
                'phone_number': '+39 02 0000 0000',
                'website': 'https://migrationops.local',
                'address': 'Demo agency workspace',
                'is_active': True,
            },
        )
        return agency

    def _seed_user(self, *, email, role, agency, first_name, last_name, is_staff=False, is_superuser=False):
        user, _ = User.objects.update_or_create(
            email=email,
            defaults={
                'role': role,
                'agency': agency,
                'first_name': first_name,
                'last_name': last_name,
                'phone_number': '',
                'is_active': True,
                'is_staff': is_staff,
                'is_superuser': is_superuser,
            },
        )
        user.set_password(DEMO_PASSWORD)
        user.save(update_fields=['password'])
        return user

    def _seed_profiles(self, *, client, lawyer):
        ClientProfile.objects.update_or_create(
            user=client,
            defaults={
                'nationality': 'Iran',
                'residence_country': 'Iran',
                'passport_number': 'A12345678',
                'preferred_language': 'English',
                'emergency_contact_name': 'Demo Contact',
                'emergency_contact_phone': '+98 21 0000 0000',
                'notes': 'Demo student profile for manual MVP testing.',
            },
        )
        LawyerProfile.objects.update_or_create(
            user=lawyer,
            defaults={
                'title': 'Immigration Lawyer',
                'license_number': 'MI-DEMO-001',
                'department': 'Student visas',
                'office_phone_number': '+39 02 1111 1111',
                'is_accepting_cases': True,
                'review_capacity': 12,
            },
        )

    def _seed_reference_data(self, admin):
        iran, _ = Country.objects.update_or_create(name='Iran', defaults={'iso_code': 'IR'})
        italy, _ = Country.objects.update_or_create(name='Italy', defaults={'iso_code': 'IT'})
        visa_type, _ = VisaType.objects.update_or_create(
            slug='italy-student-university-enrollment',
            defaults={
                'name': 'Italy Student Visa - University Enrollment',
                'description': 'Demo student visa workflow for Iranian applicants applying to study in Italy.',
                'is_active': True,
            },
        )
        document_specs = [
            ('passport', 'Passport', 'Valid passport identity page.'),
            ('university-enrollment-letter', 'University enrollment letter', 'Admission or enrollment confirmation from the Italian institution.'),
            ('proof-of-funds', 'Proof of funds', 'Bank statements or sponsorship proof.'),
            ('travel-insurance', 'Travel insurance', 'Insurance coverage for the expected travel period.'),
            ('accommodation-proof', 'Accommodation proof', 'Lease, dorm confirmation, or host declaration.'),
        ]
        document_types = []
        for index, (slug, name, description) in enumerate(document_specs, start=1):
            document_type, _ = DocumentType.objects.update_or_create(
                slug=slug,
                defaults={'name': name, 'description': description, 'is_active': True},
            )
            VisaRequirement.objects.update_or_create(
                origin_country=iran,
                destination_country=italy,
                visa_type=visa_type,
                document_type=document_type,
                status=PublicationStatus.ACTIVE,
                defaults={
                    'title': name,
                    'instructions': description,
                    'is_required': True,
                    'sort_order': index,
                    'published_by': admin,
                    'published_at': timezone.now(),
                },
            )
            document_types.append(document_type)
        return iran, italy, visa_type, document_types

    def _seed_workflow_template(self, *, agency, visa_type):
        AgencyWorkflowTemplate.objects.update_or_create(
            agency=agency,
            visa_type=visa_type,
            is_active=True,
            defaults={
                'name': 'Italy student visa standard workflow',
                'stages': [
                    {'key': 'intake', 'label': 'Intake', 'status': 'active'},
                    {'key': 'document_collection', 'label': 'Document collection', 'status': 'pending'},
                    {'key': 'legal_review', 'label': 'Legal review', 'status': 'pending'},
                    {'key': 'submission_preparation', 'label': 'Submission preparation', 'status': 'pending'},
                    {'key': 'submitted', 'label': 'Submitted', 'status': 'pending'},
                    {'key': 'decision', 'label': 'Decision', 'status': 'pending'},
                ],
                'deadline_rules': [
                    {'action': 'upload_required_documents', 'days_after_case_creation': 7},
                    {'action': 'lawyer_document_review', 'days_after_upload': 2},
                ],
                'escalation_rules': [
                    {'if_overdue_days': 2, 'notify': 'assigned_lawyer'},
                ],
            },
        )

    def _seed_case(self, *, agency, admin, client, lawyer, iran, italy, visa_type):
        case = StudentCase.objects.filter(
            agency=agency,
            client=client,
            visa_type=visa_type,
            title='Sara Student - Italy student visa',
        ).first()
        if case:
            return case

        case, _created_items = create_case(
            actor=admin,
            agency=agency,
            client=client,
            assigned_lawyer=lawyer,
            origin_country=iran,
            destination_country=italy,
            visa_type=visa_type,
            title='Sara Student - Italy student visa',
            current_stage='intake',
            intake_summary='Demo case for manual client/lawyer workflow testing.',
        )
        return case

    def _seed_deadlines(self, *, case, lawyer, client):
        first_action = ActionItem.objects.filter(case=case, target_user=client).order_by('created_at').first()
        if first_action and not first_action.due_date:
            first_action.due_date = timezone.now() + timedelta(days=7)
            first_action.save(update_fields=['due_date', 'updated_at'])

        Deadline.objects.update_or_create(
            case=case,
            title='Client document upload target',
            defaults={
                'action_item': first_action,
                'target_user': client,
                'due_date': timezone.now() + timedelta(days=7),
                'reminder_policy': {'days_before': [3, 1]},
                'is_resolved': False,
                'escalation_owner': lawyer,
            },
        )
