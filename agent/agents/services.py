from django.conf import settings
from django.contrib.contenttypes.models import ContentType

from users.models import UserRole

from .context import AgentContextBuilder
from .gateways.registry import get_agent_gateway
from .graphs import AgentGraphError, run_agent_graph
from .orchestrators.case import CaseAnalysisOrchestrator
from .orchestrators.client_chat import ClientCaseChatOrchestrator
from .orchestrators.document import DocumentAnalysisOrchestrator
from .orchestrators.policy_snapshot import PolicySnapshotSummaryOrchestrator
from .supervisor_graphs import SupervisorGraphError, run_supervisor_case_graph
from .models import AgentTask


class AgentServiceError(ValueError):
    pass


def summarize_policy_snapshot(*, snapshot, actor, gateway=None):
    _ensure_no_running_target_task(target=snapshot, task_type=AgentTask.TaskType.POLICY_SNAPSHOT_SUMMARY)
    orchestrator = PolicySnapshotSummaryOrchestrator()
    selected_gateway = gateway or get_agent_gateway()
    try:
        return run_agent_graph(orchestrator=orchestrator, target=snapshot, actor=actor, gateway=selected_gateway)
    except AgentGraphError as exc:
        raise AgentServiceError(str(exc)) from exc


def analyze_document(*, document, actor, gateway=None):
    _ensure_no_running_target_task(target=document, task_type=AgentTask.TaskType.DOCUMENT_ANALYSIS)
    orchestrator = DocumentAnalysisOrchestrator()
    try:
        orchestrator.ensure_ready(document=document)
    except ValueError as exc:
        raise AgentServiceError(str(exc)) from exc

    selected_gateway = gateway or get_agent_gateway()
    try:
        return run_agent_graph(orchestrator=orchestrator, target=document, actor=actor, gateway=selected_gateway)
    except AgentGraphError as exc:
        raise AgentServiceError(str(exc)) from exc


def analyze_case(*, case, actor, gateway=None):
    _ensure_no_running_target_task(target=case, task_type=AgentTask.TaskType.CASE_ANALYSIS)
    orchestrator = CaseAnalysisOrchestrator()
    selected_gateway = gateway or get_agent_gateway()
    try:
        return run_agent_graph(orchestrator=orchestrator, target=case, actor=actor, gateway=selected_gateway)
    except AgentGraphError as exc:
        raise AgentServiceError(str(exc)) from exc


def supervise_case(*, case, actor, gateway=None):
    _ensure_no_running_target_task(target=case, task_type=AgentTask.TaskType.SUPERVISOR_CASE_REVIEW)
    selected_gateway = gateway or get_agent_gateway()
    try:
        return run_supervisor_case_graph(case=case, actor=actor, gateway=selected_gateway)
    except SupervisorGraphError as exc:
        raise AgentServiceError(str(exc)) from exc


def chat_with_client_case(*, case, actor, message, gateway=None):
    message = (message or '').strip()
    max_chars = getattr(settings, 'CLIENT_CHAT_MAX_INPUT_CHARS', 800)
    if actor.role != UserRole.CLIENT:
        raise AgentServiceError('Only clients can use the client case chatbot.')
    if case.client_id != actor.id:
        raise AgentServiceError('You can only chat about your own case.')
    if not message:
        raise AgentServiceError('Message is required.')
    if len(message) > max_chars:
        raise AgentServiceError(f'Message is too long. Limit is {max_chars} characters.')

    client_context = build_client_case_chat_context(case=case, actor=actor)
    orchestrator = ClientCaseChatOrchestrator(message=message, client_context=client_context)
    selected_gateway = gateway or get_agent_gateway()
    try:
        return run_agent_graph(orchestrator=orchestrator, target=case, actor=actor, gateway=selected_gateway)
    except AgentGraphError as exc:
        raise AgentServiceError(str(exc)) from exc


def build_client_case_chat_context(*, case, actor):
    return AgentContextBuilder().build_client_case_chat_context(case=case, actor=actor)


def _ensure_no_running_target_task(*, target, task_type):
    content_type = ContentType.objects.get_for_model(target)
    if AgentTask.objects.filter(
        content_type=content_type,
        object_id=target.id,
        task_type=task_type,
        status=AgentTask.Status.RUNNING,
    ).exists():
        raise AgentServiceError(f'{task_type} is already running for this record.')
