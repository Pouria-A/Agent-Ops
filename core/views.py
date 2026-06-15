from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.dashboard import build_client_dashboard, build_staff_dashboard
from core.permissions import IsAgencyStaff
from core.serializers import ClientDashboardSerializer, StaffDashboardSerializer
from core.versioning import build_version_payload
from users.models import UserRole


class VersionView(APIView):
    permission_classes = []
    authentication_classes = []

    @extend_schema(responses=OpenApiTypes.OBJECT)
    def get(self, request):
        return Response(build_version_payload())


class StaffDashboardView(APIView):
    permission_classes = [IsAgencyStaff]

    @extend_schema(responses=StaffDashboardSerializer)
    def get(self, request):
        return Response(build_staff_dashboard(request.user))


class ClientDashboardView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(responses=ClientDashboardSerializer)
    def get(self, request):
        if request.user.role != UserRole.CLIENT:
            return Response({'detail': 'Only clients can access the client dashboard.'}, status=403)
        return Response(build_client_dashboard(request.user))
