import asyncio
import json
import time
from urllib.parse import parse_qs

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer
from django.conf import settings

from cases.models import StudentCase
from core.versioning import WEBSOCKET_PROTOCOL_VERSION
from users.models import UserRole

from .services import AgentServiceError, chat_with_client_case
from .socket_tickets import ClientChatSocketTicketError, consume_client_chat_socket_ticket


class ClientCaseChatConsumer(AsyncJsonWebsocketConsumer):
    async def connect(self):
        self.case_id = self.scope['url_route']['kwargs']['case_id']
        self.user = None
        self.message_timestamps = []
        self.idle_task = None

        if self._is_v1_socket():
            try:
                self.user = await self._consume_socket_ticket(self._query_value('ticket'), self.case_id)
            except ClientChatSocketTicketError:
                await self.close(code=4401)
                return
        else:
            self.user = self.scope.get('user')
            if not self.user or not self.user.is_authenticated:
                await self.close(code=4401)
                return
            if self.user.role != UserRole.CLIENT:
                await self.close(code=4403)
                return

        has_access = await self._client_can_access_case(self.case_id, self.user.id)
        if not has_access:
            await self.close(code=4403)
            return

        await self.accept()
        await self.send_json(
            {
                'type': 'connection.accepted',
                'case_id': int(self.case_id),
                'protocol_version': WEBSOCKET_PROTOCOL_VERSION,
                'max_input_chars': getattr(settings, 'CLIENT_CHAT_MAX_INPUT_CHARS', 800),
                'max_messages_per_minute': getattr(settings, 'CLIENT_CHAT_WS_MAX_MESSAGES_PER_MINUTE', 12),
                'idle_timeout_seconds': getattr(settings, 'CLIENT_CHAT_WS_IDLE_TIMEOUT_SECONDS', 300),
            }
        )
        self._reset_idle_timer()

    async def disconnect(self, close_code):
        if self.idle_task:
            self.idle_task.cancel()

    async def receive_json(self, content, **kwargs):
        self._reset_idle_timer()
        if not isinstance(content, dict):
            await self._send_error('invalid_payload', 'Message payload must be a JSON object.')
            return
        if not self._message_size_allowed(content):
            await self._send_error('message_too_large', 'Message payload is too large.')
            await self.close(code=4408)
            return
        if not self._rate_allowed():
            await self._send_error('rate_limited', 'Too many chat messages. Please wait before sending another message.')
            await self.close(code=4429)
            return

        message = content.get('message')
        if not isinstance(message, str):
            await self._send_error('invalid_message', 'Message must be a string.')
            return

        await self.send_json({'type': 'chat.started', 'protocol_version': WEBSOCKET_PROTOCOL_VERSION})
        try:
            response_payload = await self._run_chat(message)
        except AgentServiceError as exc:
            await self._send_error('chat_rejected', str(exc))
            return

        await self.send_json({'type': 'chat.completed', **response_payload})

    def _is_v1_socket(self):
        return self.scope.get('path', '').startswith('/ws/v1/')

    def _query_value(self, key):
        query_string = self.scope.get('query_string', b'').decode('utf-8')
        values = parse_qs(query_string).get(key) or []
        return values[0] if values else ''

    def _message_size_allowed(self, content):
        max_bytes = getattr(settings, 'CLIENT_CHAT_WS_MAX_MESSAGE_BYTES', 2048)
        return len(json.dumps(content).encode('utf-8')) <= max_bytes

    def _rate_allowed(self):
        max_messages = getattr(settings, 'CLIENT_CHAT_WS_MAX_MESSAGES_PER_MINUTE', 12)
        now = time.monotonic()
        self.message_timestamps = [entry for entry in self.message_timestamps if now - entry < 60]
        if len(self.message_timestamps) >= max_messages:
            return False
        self.message_timestamps.append(now)
        return True

    def _reset_idle_timer(self):
        timeout_seconds = getattr(settings, 'CLIENT_CHAT_WS_IDLE_TIMEOUT_SECONDS', 300)
        if timeout_seconds <= 0:
            return
        if self.idle_task:
            self.idle_task.cancel()
        self.idle_task = asyncio.create_task(self._close_when_idle(timeout_seconds))

    async def _close_when_idle(self, timeout_seconds):
        try:
            await asyncio.sleep(timeout_seconds)
            await self.close(code=4408)
        except asyncio.CancelledError:
            return

    async def _send_error(self, code, detail):
        await self.send_json({'type': 'error', 'code': code, 'detail': detail})

    @database_sync_to_async
    def _consume_socket_ticket(self, raw_ticket, case_id):
        return consume_client_chat_socket_ticket(raw_ticket=raw_ticket, case_id=case_id)

    @database_sync_to_async
    def _client_can_access_case(self, case_id, user_id):
        return StudentCase.objects.filter(id=case_id, client_id=user_id).exists()

    @database_sync_to_async
    def _run_chat(self, message):
        case = StudentCase.objects.select_related(
            'agency',
            'client',
            'assigned_lawyer',
            'origin_country',
            'destination_country',
            'visa_type',
        ).get(id=self.case_id)
        task, result = chat_with_client_case(case=case, actor=self.user, message=message)
        return {
            'task_id': task.id,
            'result_id': result.id,
            'protocol_version': WEBSOCKET_PROTOCOL_VERSION,
            'answer': result.output_payload.get('answer', result.summary),
            'suggestions': result.output_payload.get('suggestions', []),
            'visible_case_context': result.output_payload.get('visible_case_context', {}),
            'safety_notes': result.output_payload.get('safety_notes', []),
            'escalation_recommended': result.output_payload.get('escalation_recommended', False),
        }
