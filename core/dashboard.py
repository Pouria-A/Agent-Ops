from django.utils import timezone

from actions.models import ActionItem
from cases.models import StudentCase
from deadlines.models import Deadline
from documents.models import Document


def build_staff_dashboard(user):
    now = timezone.now()
    agency_id = user.agency_id

    return {
        'active_cases': StudentCase.objects.filter(
            agency_id=agency_id,
            status=StudentCase.Status.ACTIVE,
        ).count(),
        'flagged_documents': Document.objects.filter(
            case__agency_id=agency_id,
            status=Document.Status.FLAGGED_FOR_HUMAN,
        ).count(),
        'overdue_action_items': ActionItem.objects.filter(
            case__agency_id=agency_id,
            due_date__lt=now,
        ).exclude(status__in=[ActionItem.Status.COMPLETED, ActionItem.Status.CANCELLED]).count(),
        'upcoming_deadlines': Deadline.objects.filter(
            case__agency_id=agency_id,
            due_date__gte=now,
            is_resolved=False,
        ).count(),
    }


def build_client_dashboard(user):
    now = timezone.now()
    return {
        'active_cases': StudentCase.objects.filter(
            client=user,
            status=StudentCase.Status.ACTIVE,
        ).count(),
        'pending_actions': ActionItem.objects.filter(
            target_user=user,
            status__in=[ActionItem.Status.PENDING, ActionItem.Status.IN_PROGRESS],
        ).count(),
        'upcoming_deadlines': Deadline.objects.filter(
            target_user=user,
            due_date__gte=now,
            is_resolved=False,
        ).count(),
        'documents': Document.objects.filter(case__client=user).count(),
    }
