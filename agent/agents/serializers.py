from django.conf import settings
from rest_framework import serializers

from cases.models import StudentCase
from core.versioning import WEBSOCKET_PROTOCOL_VERSION
from users.models import UserRole

from .models import AgentEvent, AgentResult, AgentTask
from .socket_tickets import create_client_chat_socket_ticket


class AgentEventSerializer(serializers.ModelSerializer):
    class Meta:
        model = AgentEvent
        fields = ['id', 'task', 'level', 'message', 'metadata', 'created_at', 'updated_at']
        read_only_fields = fields


class AgentResultSerializer(serializers.ModelSerializer):
    class Meta:
        model = AgentResult
        fields = [
            'id',
            'task',
            'provider',
            'model',
            'summary',
            'output_schema_version',
            'output_payload',
            'usage',
            'requires_human_review',
            'validation_errors',
            'created_at',
            'updated_at',
        ]
        read_only_fields = fields


class AgentTaskSerializer(serializers.ModelSerializer):
    result = AgentResultSerializer(read_only=True)
    events = AgentEventSerializer(many=True, read_only=True)
    created_by_email = serializers.CharField(source='created_by.email', read_only=True)
    agency_name = serializers.CharField(source='agency.name', read_only=True)

    class Meta:
        model = AgentTask
        fields = [
            'id',
            'agency',
            'agency_name',
            'created_by',
            'created_by_email',
            'task_type',
            'status',
            'title',
            'gateway_type',
            'orchestrator_key',
            'agent_protocol_version',
            'input_schema_version',
            'context_schema_version',
            'input_payload',
            'context_payload',
            'content_type',
            'object_id',
            'started_at',
            'finished_at',
            'error_message',
            'result',
            'events',
            'created_at',
            'updated_at',
        ]
        read_only_fields = fields


class ClientChatSocketTicketCreateSerializer(serializers.Serializer):
    case = serializers.PrimaryKeyRelatedField(queryset=StudentCase.objects.all())

    def validate_case(self, case):
        user = self.context['request'].user
        if user.role != UserRole.CLIENT:
            raise serializers.ValidationError('Only clients can create chat socket tickets.')
        if case.client_id != user.id:
            raise serializers.ValidationError('You can only create chat socket tickets for your own case.')
        return case

    def create(self, validated_data):
        request = self.context['request']
        raw_ticket, ticket = create_client_chat_socket_ticket(
            case=validated_data['case'],
            actor=request.user,
            metadata={
                'user_agent': request.META.get('HTTP_USER_AGENT', ''),
                'remote_addr': request.META.get('REMOTE_ADDR', ''),
            },
        )
        return {
            'ticket': raw_ticket,
            'expires_at': ticket.expires_at,
            'case_id': ticket.case_id,
            'protocol_version': WEBSOCKET_PROTOCOL_VERSION,
            'max_input_chars': getattr(settings, 'CLIENT_CHAT_MAX_INPUT_CHARS', 800),
            'ws_path': f'/ws/v1/client/cases/{ticket.case_id}/chat/?ticket={raw_ticket}',
        }


class ClientChatSocketTicketSerializer(serializers.Serializer):
    ticket = serializers.CharField(read_only=True)
    expires_at = serializers.DateTimeField(read_only=True)
    case_id = serializers.IntegerField(read_only=True)
    protocol_version = serializers.CharField(read_only=True)
    max_input_chars = serializers.IntegerField(read_only=True)
    ws_path = serializers.CharField(read_only=True)
