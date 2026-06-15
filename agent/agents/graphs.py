from typing import Any, TypedDict

from django.db import IntegrityError
from django.utils import timezone
from langgraph.graph import END, START, StateGraph

from .gateways.base import AgentGatewayError
from .models import AgentEvent, AgentResult, AgentTask


class AgentGraphError(ValueError):
    pass


class AgentGraphState(TypedDict, total=False):
    actor: Any
    target: Any
    orchestrator: Any
    gateway: Any
    task: AgentTask
    request: Any
    response: Any
    validation_errors: list[str]
    result: AgentResult


def run_agent_graph(*, orchestrator, target, actor, gateway):
    graph = StateGraph(AgentGraphState)
    graph.add_node('create_task', _create_task)
    graph.add_node('build_request', _build_request)
    graph.add_node('call_gateway', _call_gateway)
    graph.add_node('validate_output', _validate_output)
    graph.add_node('persist_result', _persist_result)
    graph.add_edge(START, 'create_task')
    graph.add_edge('create_task', 'build_request')
    graph.add_edge('build_request', 'call_gateway')
    graph.add_edge('call_gateway', 'validate_output')
    graph.add_edge('validate_output', 'persist_result')
    graph.add_edge('persist_result', END)

    compiled = graph.compile()
    try:
        state = compiled.invoke(
            {
                'actor': actor,
                'target': target,
                'orchestrator': orchestrator,
                'gateway': gateway,
            }
        )
    except AgentGraphError:
        raise
    except AgentGatewayError as exc:
        raise AgentGraphError(str(exc)) from exc
    except Exception as exc:
        raise AgentGraphError(f'Agent graph failed: {exc}') from exc

    return state['task'], state['result']


def _create_task(state):
    orchestrator = state['orchestrator']
    try:
        task = orchestrator.create_task(
            target=state['target'],
            actor=state['actor'],
            gateway=state['gateway'],
        )
    except IntegrityError as exc:
        raise AgentGraphError('An equivalent agent task is already running for this record.') from exc
    return {'task': task}


def _build_request(state):
    orchestrator = state['orchestrator']
    request = orchestrator.build_request(
        task=state['task'],
        **{orchestrator.target_kwarg: state['target']},
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
        message = response.error_message or 'Agent gateway did not complete successfully.'
        _mark_task_failed(task=state['task'], message=message)
        raise AgentGraphError(message)
    return {'response': response}


def _validate_output(state):
    validation_errors = state['orchestrator'].validate_output(state['response'].output_payload)
    return {'validation_errors': validation_errors}


def _persist_result(state):
    task = state['task']
    response = state['response']

    task.status = AgentTask.Status.SUCCEEDED
    task.finished_at = timezone.now()
    task.save(update_fields=['status', 'finished_at', 'updated_at'])

    result = AgentResult.objects.create(
        task=task,
        provider=response.provider,
        model=response.model,
        summary=response.summary or response.output_payload.get('summary', ''),
        output_schema_version=state['request'].output_schema_version,
        output_payload=response.output_payload,
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
            message='Agent output did not fully match expected schema.',
            metadata={'validation_errors': state['validation_errors']},
        )
    return {'result': result}


def _mark_task_failed(*, task, message):
    task.status = AgentTask.Status.FAILED
    task.error_message = message
    task.finished_at = timezone.now()
    task.save(update_fields=['status', 'error_message', 'finished_at', 'updated_at'])
    AgentEvent.objects.create(task=task, level='error', message=message)
