from django.contrib import admin

from .models import AgentEvent, AgentResult, AgentTask


class AgentResultInline(admin.StackedInline):
    model = AgentResult
    extra = 0
    can_delete = False


class AgentEventInline(admin.TabularInline):
    model = AgentEvent
    extra = 0
    readonly_fields = ['level', 'message', 'metadata', 'created_at', 'updated_at']


@admin.register(AgentTask)
class AgentTaskAdmin(admin.ModelAdmin):
    list_display = ['id', 'task_type', 'status', 'gateway_type', 'agency', 'created_by', 'created_at']
    list_filter = ['task_type', 'status', 'gateway_type', 'created_at']
    search_fields = ['title', 'error_message', 'created_by__email']
    readonly_fields = ['created_at', 'updated_at', 'started_at', 'finished_at']
    inlines = [AgentResultInline, AgentEventInline]


@admin.register(AgentResult)
class AgentResultAdmin(admin.ModelAdmin):
    list_display = ['id', 'task', 'provider', 'model', 'requires_human_review', 'created_at']
    search_fields = ['summary', 'provider', 'model']


@admin.register(AgentEvent)
class AgentEventAdmin(admin.ModelAdmin):
    list_display = ['id', 'task', 'level', 'created_at']
    list_filter = ['level', 'created_at']
    search_fields = ['message']
