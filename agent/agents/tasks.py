from celery import shared_task

from cases.models import StudentCase
from documents.models import Document
from reference_data.models import PolicySnapshot

from .services import AgentServiceError, analyze_case, analyze_document, summarize_policy_snapshot, supervise_case


@shared_task(autoretry_for=(Exception,), retry_backoff=True, retry_jitter=True, max_retries=2)
def summarize_policy_snapshot_task(snapshot_id, actor_id):
    from users.models import User

    snapshot = PolicySnapshot.objects.select_related('source', 'source__country').get(id=snapshot_id)
    actor = User.objects.get(id=actor_id)
    try:
        task, result = summarize_policy_snapshot(snapshot=snapshot, actor=actor)
    except AgentServiceError as exc:
        return {'status': 'failed', 'snapshot_id': snapshot_id, 'error': str(exc)}
    return {'task_id': task.id, 'result_id': result.id}


@shared_task(autoretry_for=(Exception,), retry_backoff=True, retry_jitter=True, max_retries=2)
def analyze_document_task(document_id, actor_id):
    from users.models import User

    document = Document.objects.select_related(
        'case',
        'case__agency',
        'case__origin_country',
        'case__destination_country',
        'case__visa_type',
        'document_type',
    ).get(id=document_id)
    actor = User.objects.get(id=actor_id)
    try:
        task, result = analyze_document(document=document, actor=actor)
    except AgentServiceError as exc:
        return {'status': 'failed', 'document_id': document_id, 'error': str(exc)}
    return {'task_id': task.id, 'result_id': result.id}


@shared_task(autoretry_for=(Exception,), retry_backoff=True, retry_jitter=True, max_retries=2)
def analyze_case_task(case_id, actor_id):
    from users.models import User

    case = StudentCase.objects.select_related(
        'agency',
        'client',
        'assigned_lawyer',
        'origin_country',
        'destination_country',
        'visa_type',
    ).get(id=case_id)
    actor = User.objects.get(id=actor_id)
    try:
        task, result = analyze_case(case=case, actor=actor)
    except AgentServiceError as exc:
        return {'status': 'failed', 'case_id': case_id, 'error': str(exc)}
    return {'task_id': task.id, 'result_id': result.id}


@shared_task(autoretry_for=(Exception,), retry_backoff=True, retry_jitter=True, max_retries=2)
def supervise_case_task(case_id, actor_id):
    from users.models import User

    case = StudentCase.objects.select_related(
        'agency',
        'client',
        'assigned_lawyer',
        'origin_country',
        'destination_country',
        'visa_type',
    ).get(id=case_id)
    actor = User.objects.get(id=actor_id)
    try:
        task, result = supervise_case(case=case, actor=actor)
    except AgentServiceError as exc:
        return {'status': 'failed', 'case_id': case_id, 'error': str(exc)}
    return {'task_id': task.id, 'result_id': result.id}
