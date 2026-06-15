from rest_framework import generics, mixins, viewsets
from rest_framework.response import Response

from core.permissions import IsAgencyStaff, IsClient

from .models import AgentTask
from .serializers import AgentTaskSerializer, ClientChatSocketTicketCreateSerializer, ClientChatSocketTicketSerializer


class AgentTaskViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    queryset = AgentTask.objects.none()
    serializer_class = AgentTaskSerializer
    permission_classes = [IsAgencyStaff]
    filterset_fields = ['task_type', 'status', 'gateway_type', 'created_by']
    search_fields = ['title', 'error_message']
    ordering_fields = ['created_at', 'started_at', 'finished_at']
    ordering = ['-created_at']

    def get_queryset(self):
        if getattr(self, 'swagger_fake_view', False):
            return AgentTask.objects.none()
        queryset = AgentTask.objects.select_related('agency', 'created_by', 'content_type').prefetch_related('events')
        user = self.request.user
        if user.is_superuser:
            return queryset
        return queryset.filter(agency_id=user.agency_id)


class ClientChatSocketTicketCreateView(generics.CreateAPIView):
    serializer_class = ClientChatSocketTicketCreateSerializer
    permission_classes = [IsClient]
    throttle_scope = 'client_chat_socket_ticket'

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        payload = serializer.save()
        return Response(ClientChatSocketTicketSerializer(payload).data, status=201)
