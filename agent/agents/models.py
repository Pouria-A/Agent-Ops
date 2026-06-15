from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models
from django.utils import timezone

from core.models import TimeStampedModel
from core.versioning import (
    AGENT_CONTEXT_SCHEMA_VERSION,
    AGENT_INPUT_SCHEMA_VERSION,
    AGENT_OUTPUT_SCHEMA_VERSION,
    AGENT_PROTOCOL_VERSION,
)


class AgentTask(TimeStampedModel):
    class TaskType(models.TextChoices):
        POLICY_SNAPSHOT_SUMMARY = 'POLICY_SNAPSHOT_SUMMARY', 'Policy snapshot summary'
        DOCUMENT_ANALYSIS = 'DOCUMENT_ANALYSIS', 'Document analysis'
        CASE_ANALYSIS = 'CASE_ANALYSIS', 'Case analysis'
        SUPERVISOR_CASE_REVIEW = 'SUPERVISOR_CASE_REVIEW', 'Supervisor case review'
        CLIENT_CASE_CHAT = 'CLIENT_CASE_CHAT', 'Client case chat'

    class Status(models.TextChoices):
        PENDING = 'PENDING', 'Pending'
        RUNNING = 'RUNNING', 'Running'
        SUCCEEDED = 'SUCCEEDED', 'Succeeded'
        FAILED = 'FAILED', 'Failed'

    agency = models.ForeignKey(
        'agencies.Agency',
        on_delete=models.CASCADE,
        related_name='agent_tasks',
        null=True,
        blank=True,
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name='created_agent_tasks',
        null=True,
        blank=True,
    )
    task_type = models.CharField(max_length=80, choices=TaskType.choices)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    title = models.CharField(max_length=255)
    gateway_type = models.CharField(max_length=80, default='mock')
    orchestrator_key = models.CharField(max_length=120, blank=True)
    agent_protocol_version = models.CharField(max_length=80, default=AGENT_PROTOCOL_VERSION)
    input_schema_version = models.CharField(max_length=80, default=AGENT_INPUT_SCHEMA_VERSION)
    context_schema_version = models.CharField(max_length=80, default=AGENT_CONTEXT_SCHEMA_VERSION)
    input_payload = models.JSONField(default=dict, blank=True)
    context_payload = models.JSONField(default=dict, blank=True)
    content_type = models.ForeignKey(ContentType, on_delete=models.SET_NULL, null=True, blank=True)
    object_id = models.PositiveBigIntegerField(null=True, blank=True)
    target_object = GenericForeignKey('content_type', 'object_id')
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    error_message = models.TextField(blank=True)

    class Meta:
        indexes = [
            models.Index(fields=['agency', 'task_type', 'status']),
            models.Index(fields=['created_at']),
            models.Index(fields=['content_type', 'object_id']),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['task_type', 'content_type', 'object_id'],
                condition=(
                    models.Q(status='RUNNING')
                    & models.Q(content_type__isnull=False)
                    & models.Q(object_id__isnull=False)
                    & ~models.Q(task_type='CLIENT_CASE_CHAT')
                ),
                name='unique_running_agent_task_per_target',
            ),
        ]
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.task_type} #{self.id}'


class AgentResult(TimeStampedModel):
    task = models.OneToOneField(AgentTask, on_delete=models.CASCADE, related_name='result')
    provider = models.CharField(max_length=80, blank=True)
    model = models.CharField(max_length=120, blank=True)
    summary = models.TextField(blank=True)
    output_schema_version = models.CharField(max_length=80, default=AGENT_OUTPUT_SCHEMA_VERSION)
    output_payload = models.JSONField(default=dict, blank=True)
    usage = models.JSONField(default=dict, blank=True)
    requires_human_review = models.BooleanField(default=True)
    validation_errors = models.JSONField(default=list, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'Result for task #{self.task_id}'


class AgentEvent(TimeStampedModel):
    task = models.ForeignKey(AgentTask, on_delete=models.CASCADE, related_name='events')
    level = models.CharField(max_length=20, default='info')
    message = models.TextField()
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=['task', 'created_at']),
        ]
        ordering = ['created_at']

    def __str__(self):
        return f'{self.level}: task #{self.task_id}'


class ClientChatSocketTicket(TimeStampedModel):
    class Status(models.TextChoices):
        ACTIVE = 'ACTIVE', 'Active'
        USED = 'USED', 'Used'
        EXPIRED = 'EXPIRED', 'Expired'
        REVOKED = 'REVOKED', 'Revoked'

    ticket_hash = models.CharField(max_length=128, unique=True)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='client_chat_socket_tickets')
    case = models.ForeignKey('cases.StudentCase', on_delete=models.CASCADE, related_name='client_chat_socket_tickets')
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=['user', 'case', 'status']),
            models.Index(fields=['expires_at']),
        ]
        ordering = ['-created_at']

    def __str__(self):
        return f'Client chat socket ticket #{self.id}'

    @property
    def is_expired(self):
        return self.expires_at <= timezone.now()
