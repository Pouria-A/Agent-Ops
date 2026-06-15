from typing import Any, TypedDict

from django.contrib.contenttypes.models import ContentType
from django.db import IntegrityError
from django.utils import timezone
from langgraph.graph import END, START, StateGraph

from documents.models import Document
from reference_data.models import PolicySnapshot

from .context import AgentContextBuilder
from .gateways.base import AgentGatewayError
from .graphs import AgentGraphError, run_agent_graph
from .models import AgentEvent, AgentResult, AgentTask
from .orchestrators.case import CaseAnalysisOrchestrator
from .orchestrators.document import DocumentAnalysisOrchestrator
from .orchestrators.policy_snapshot import PolicySnapshotSummaryOrchestrator
from .orchestrators.supervisor import SupervisorCaseReviewOrchestrator


class SupervisorGraphError(ValueError):
    pass


class SupervisorGraphState(TypedDict, total=False):
    actor: Any
    case: Any
    gateway: Any
    task: AgentTask
    context: dict
    plan: dict
    subagent_results: dict
    request: Any
    response: Any
    validation_errors: list[str]
    result: AgentResult


def run_supervisor_case_graph(*, case, actor, gateway):
    graph = StateGraph(SupervisorGraphState)
    graph.add_node('create_supervisor_task', _create_supervisor_task)
    graph.add_node('load_case_context', _load_case_context)
    graph.add_node('plan_agent_tasks', _plan_agent_tasks)
    graph.add_node('run_case_agent', _run_case_agent)
    graph.add_node('run_document_agents', _run_document_agents)
    graph.add_node('run_policy_agents', _run_policy_agents)
    graph.add_node('build_supervisor_request', _build_supervisor_request)
    graph.add_node('call_gateway', _call_gateway)
    graph.add_node('validate_output', _validate_output)
    graph.add_node('persist_result', _persist_result)
    graph.add_edge(START, 'create_supervisor_task')
    graph.add_edge('create_supervisor_task', 'load_case_context')
    graph.add_edge('load_case_context', 'plan_agent_tasks')
    graph.add_edge('plan_agent_tasks', 'run_case_agent')
    graph.add_edge('run_case_agent', 'run_document_agents')
    graph.add_edge('run_document_agents', 'run_policy_agents')
    graph.add_edge('run_policy_agents', 'build_supervisor_request')
    graph.add_edge('build_supervisor_request', 'call_gateway')
    graph.add_edge('call_gateway', 'validate_output')
    graph.add_edge('validate_output', 'persist_result')
    graph.add_edge('persist_result', END)

    compiled = graph.compile()
    try:
        state = compiled.invoke({'case': case, 'actor': actor, 'gateway': gateway})
    except SupervisorGraphError:
        raise
    except (AgentGatewayError, AgentGraphError) as exc:
        raise SupervisorGraphError(str(exc)) from exc
    except Exception as exc:
        raise SupervisorGraphError(f'Supervisor graph failed: {exc}') from exc
    return state['task'], state['result']


def _create_supervisor_task(state):
    case = state['case']
    gateway = state['gateway']
    content_type = ContentType.objects.get_for_model(case)
    try:
        task = AgentTask.objects.create(
            agency=case.agency,
            created_by=state['actor'],
            task_type=AgentTask.TaskType.SUPERVISOR_CASE_REVIEW,
            status=AgentTask.Status.RUNNING,
            title=f'Supervise case {case.id}',
            gateway_type=gateway.gateway_type,
            orchestrator_key=SupervisorCaseReviewOrchestrator.key,
            input_payload={
                'case_id': case.id,
                'status': case.status,
                'current_stage': case.current_stage,
            },
            context_payload={
                'case_title': case.title,
                'visa_type': case.visa_type.name,
                'destination_country': case.destination_country.name,
            },
            content_type=content_type,
            object_id=case.id,
            started_at=timezone.now(),
        )
    except IntegrityError as exc:
        raise SupervisorGraphError('An equivalent supervisor task is already running for this case.') from exc
    return {'task': task}


def _load_case_context(state):
    context = AgentContextBuilder().build_supervisor_planning_context(case=state['case'])
    return {'context': context}


def _plan_agent_tasks(state):
    context = state['context']
    execution_order = ['CASE_ANALYSIS']
    if context['analyzable_document_ids']:
        execution_order.append('DOCUMENT_ANALYSIS')
    if context['policy_snapshot_ids']:
        execution_order.append('POLICY_SNAPSHOT_SUMMARY')
    plan = {
        'execution_order': execution_order,
        'case_agent': {'enabled': True},
        'document_agents': {
            'enabled': bool(context['analyzable_document_ids']),
            'document_ids': context['analyzable_document_ids'],
            'skipped_document_ids': [
                document_id
                for document_id in context['document_ids']
                if document_id not in context['analyzable_document_ids']
            ],
        },
        'policy_agents': {
            'enabled': bool(context['policy_snapshot_ids']),
            'policy_snapshot_ids': context['policy_snapshot_ids'],
        },
        'human_review_required': True,
    }
    AgentEvent.objects.create(
        task=state['task'],
        level='info',
        message='Supervisor planned specialist agent execution.',
        metadata=plan,
    )
    return {'plan': plan, 'subagent_results': {'case': None, 'documents': [], 'policy': []}}


def _run_case_agent(state):
    task, result = run_agent_graph(
        orchestrator=CaseAnalysisOrchestrator(),
        target=state['case'],
        actor=state['actor'],
        gateway=state['gateway'],
    )
    subagent_results = dict(state['subagent_results'])
    subagent_results['case'] = _result_payload(task, result)
    return {'subagent_results': subagent_results}


def _run_document_agents(state):
    subagent_results = dict(state['subagent_results'])
    documents = []
    for document in Document.objects.filter(id__in=state['plan']['document_agents']['document_ids']).select_related(
        'case',
        'case__origin_country',
        'case__destination_country',
        'case__visa_type',
        'document_type',
    ):
        task, result = run_agent_graph(
            orchestrator=DocumentAnalysisOrchestrator(),
            target=document,
            actor=state['actor'],
            gateway=state['gateway'],
        )
        documents.append(_result_payload(task, result))
    subagent_results['documents'] = documents
    return {'subagent_results': subagent_results}


def _run_policy_agents(state):
    subagent_results = dict(state['subagent_results'])
    policy_results = []
    for snapshot in PolicySnapshot.objects.filter(id__in=state['plan']['policy_agents']['policy_snapshot_ids']).select_related(
        'source',
        'source__country',
    ):
        task, result = run_agent_graph(
            orchestrator=PolicySnapshotSummaryOrchestrator(),
            target=snapshot,
            actor=state['actor'],
            gateway=state['gateway'],
        )
        policy_results.append(_result_payload(task, result))
    subagent_results['policy'] = policy_results
    return {'subagent_results': subagent_results}


def _build_supervisor_request(state):
    request = SupervisorCaseReviewOrchestrator().build_request(
        task=state['task'],
        case=state['case'],
        plan=state['plan'],
        subagent_results=state['subagent_results'],
    )
    task = state['task']
    task.agent_protocol_version = request.agent_protocol_version
    task.input_schema_version = request.input_schema_version
    task.context_schema_version = request.context_schema_version
    task.save(
        update_fields=[
            'agent_protocol_version',
            'input_schema_version',
            'context_schema_version',
            'updated_at',
        ]
    )
    return {'request': request}


def _call_gateway(state):
    try:
        response = state['gateway'].execute(state['request'])
    except Exception as exc:
        _mark_task_failed(task=state['task'], message=str(exc))
        raise
    if response.status.lower() not in {'succeeded', 'success', 'completed'}:
        message = response.error_message or 'Supervisor gateway did not complete successfully.'
        _mark_task_failed(task=state['task'], message=message)
        raise SupervisorGraphError(message)
    return {'response': response}


def _validate_output(state):
    validation_errors = SupervisorCaseReviewOrchestrator().validate_output(state['response'].output_payload)
    return {'validation_errors': validation_errors}


def _persist_result(state):
    task = state['task']
    response = state['response']
    task.status = AgentTask.Status.SUCCEEDED
    task.finished_at = timezone.now()
    task.save(update_fields=['status', 'finished_at', 'updated_at'])

    output_payload = {
        **response.output_payload,
        'plan': state['plan'],
        'subagent_results': state['subagent_results'],
    }
    result = AgentResult.objects.create(
        task=task,
        provider=response.provider,
        model=response.model,
        summary=response.summary or response.output_payload.get('summary', ''),
        output_schema_version=state['request'].output_schema_version,
        output_payload=output_payload,
        usage=response.usage,
        requires_human_review=True,
        validation_errors=state['validation_errors'],
    )
    for event in response.events:
        AgentEvent.objects.create(
            task=task,
            level=event.get('level', 'info'),
            message=event.get('message', ''),
            metadata=event.get('metadata', {}),
        )
    if state['validation_errors']:
        AgentEvent.objects.create(
            task=task,
            level='warning',
            message='Supervisor output did not fully match expected schema.',
            metadata={'validation_errors': state['validation_errors']},
        )
    return {'result': result}


def _result_payload(task, result):
    return {
        'task_id': task.id,
        'task_type': task.task_type,
        'result_id': result.id,
        'summary': result.summary,
        'agent_protocol_version': task.agent_protocol_version,
        'context_schema_version': task.context_schema_version,
        'output_schema_version': result.output_schema_version,
        'output_payload': result.output_payload,
    }


def _mark_task_failed(*, task, message):
    task.status = AgentTask.Status.FAILED
    task.error_message = message
    task.finished_at = timezone.now()
    task.save(update_fields=['status', 'error_message', 'finished_at', 'updated_at'])
    AgentEvent.objects.create(task=task, level='error', message=message)
