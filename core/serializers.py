from rest_framework import serializers


class StaffDashboardSerializer(serializers.Serializer):
    active_cases = serializers.IntegerField()
    flagged_documents = serializers.IntegerField()
    overdue_action_items = serializers.IntegerField()
    upcoming_deadlines = serializers.IntegerField()


class ClientDashboardSerializer(serializers.Serializer):
    active_cases = serializers.IntegerField()
    pending_actions = serializers.IntegerField()
    upcoming_deadlines = serializers.IntegerField()
    documents = serializers.IntegerField()
