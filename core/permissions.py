from rest_framework import permissions

from users.models import UserRole


AGENCY_STAFF_ROLES = {UserRole.ADMIN, UserRole.LAWYER, UserRole.PARALEGAL}
LEGAL_CONTROL_ROLES = {UserRole.ADMIN, UserRole.LAWYER}
OPERATIONAL_ROLES = {UserRole.ADMIN, UserRole.LAWYER, UserRole.PARALEGAL}


class IsAgencyStaff(permissions.BasePermission):
    def has_permission(self, request, view):
        return bool(
            request.user
            and request.user.is_authenticated
            and (request.user.is_superuser or request.user.role in AGENCY_STAFF_ROLES)
        )


class IsAgencyAdmin(permissions.BasePermission):
    def has_permission(self, request, view):
        return bool(
            request.user
            and request.user.is_authenticated
            and (request.user.is_superuser or request.user.role == UserRole.ADMIN)
        )


class IsLegalControlStaff(permissions.BasePermission):
    def has_permission(self, request, view):
        return bool(
            request.user
            and request.user.is_authenticated
            and (request.user.is_superuser or request.user.role in LEGAL_CONTROL_ROLES)
        )


class IsOperationalStaff(permissions.BasePermission):
    def has_permission(self, request, view):
        return bool(
            request.user
            and request.user.is_authenticated
            and (request.user.is_superuser or request.user.role in OPERATIONAL_ROLES)
        )


class IsClient(permissions.BasePermission):
    def has_permission(self, request, view):
        return bool(
            request.user
            and request.user.is_authenticated
            and request.user.role == UserRole.CLIENT
        )


class IsSelfOrAgencyStaff(permissions.BasePermission):
    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated)

    def has_object_permission(self, request, view, obj):
        if request.user.is_superuser:
            return True
        if request.user.role in AGENCY_STAFF_ROLES:
            return obj.agency_id == request.user.agency_id
        return obj.id == request.user.id


def restrict_queryset_to_user(queryset, user, agency_field='agency'):
    if not user.is_authenticated:
        return queryset.none()
    if user.is_superuser:
        return queryset
    if user.role in AGENCY_STAFF_ROLES:
        return queryset.filter(**{f'{agency_field}_id': user.agency_id})
    return queryset.none()
