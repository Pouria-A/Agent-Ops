import hashlib
import secrets
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from cases.models import StudentCase
from users.models import UserRole

from .models import ClientChatSocketTicket


class ClientChatSocketTicketError(ValueError):
    pass


def create_client_chat_socket_ticket(*, case, actor, metadata=None):
    if actor.role != UserRole.CLIENT:
        raise ClientChatSocketTicketError('Only clients can create chat socket tickets.')
    if case.client_id != actor.id:
        raise ClientChatSocketTicketError('You can only create chat socket tickets for your own case.')

    raw_ticket = secrets.token_urlsafe(32)
    ttl_seconds = getattr(settings, 'CLIENT_CHAT_SOCKET_TICKET_TTL_SECONDS', 60)
    ticket = ClientChatSocketTicket.objects.create(
        ticket_hash=_hash_ticket(raw_ticket),
        user=actor,
        case=case,
        expires_at=timezone.now() + timedelta(seconds=ttl_seconds),
        metadata=metadata or {},
    )
    return raw_ticket, ticket


def consume_client_chat_socket_ticket(*, raw_ticket, case_id):
    if not raw_ticket:
        raise ClientChatSocketTicketError('Socket ticket is required.')

    now = timezone.now()
    ticket_hash = _hash_ticket(raw_ticket)
    with transaction.atomic():
        try:
            ticket = (
                ClientChatSocketTicket.objects.select_for_update()
                .select_related('user', 'case')
                .get(ticket_hash=ticket_hash)
            )
        except ClientChatSocketTicket.DoesNotExist as exc:
            raise ClientChatSocketTicketError('Socket ticket is invalid.') from exc

        if ticket.status != ClientChatSocketTicket.Status.ACTIVE:
            raise ClientChatSocketTicketError('Socket ticket has already been used or revoked.')
        if ticket.expires_at <= now:
            ticket.status = ClientChatSocketTicket.Status.EXPIRED
            ticket.save(update_fields=['status', 'updated_at'])
            raise ClientChatSocketTicketError('Socket ticket has expired.')
        if ticket.case_id != int(case_id):
            raise ClientChatSocketTicketError('Socket ticket does not match this case.')
        if ticket.user.role != UserRole.CLIENT or ticket.case.client_id != ticket.user_id:
            raise ClientChatSocketTicketError('Socket ticket is not valid for this client case.')

        ticket.status = ClientChatSocketTicket.Status.USED
        ticket.used_at = now
        ticket.save(update_fields=['status', 'used_at', 'updated_at'])
        return ticket.user


def _hash_ticket(raw_ticket):
    return hashlib.sha256(raw_ticket.encode('utf-8')).hexdigest()
