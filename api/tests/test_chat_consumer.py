"""WebSocket auth-gate tests for ChatConsumer (R3).

Exercises the JWT middleware + consumer connect path against the in-memory
channel layer. We drive the async communicator from sync test methods via
async_to_sync, and use TransactionTestCase so the consumer's
database_sync_to_async queries (which run on a separate connection) see the
data created in setUp.
"""
from asgiref.sync import async_to_sync
from channels.routing import URLRouter
from channels.testing import WebsocketCommunicator
from django.contrib.auth.models import User
from django.test import TransactionTestCase
from rest_framework_simplejwt.tokens import RefreshToken

from api.jwt_middleware import JWTAuthMiddleware
from api.models import Conversation
from api.routing import websocket_urlpatterns

# The auth + routing stack, minus the AllowedHostsOriginValidator (which would
# need an Origin header the test client doesn't send). This is exactly what
# asgi.py wraps in production.
application = JWTAuthMiddleware(URLRouter(websocket_urlpatterns))


def _token_for(user):
    return str(RefreshToken.for_user(user).access_token)


class ChatConsumerAuthTests(TransactionTestCase):
    def setUp(self):
        self.alice = User.objects.create(username="ws_alice", password="x")
        self.bob = User.objects.create(username="ws_bob", password="x")
        self.outsider = User.objects.create(username="ws_outsider", password="x")
        self.convo = Conversation.objects.create()
        self.convo.participants.add(self.alice, self.bob)

    async def _try_connect(self, conversation_id, subprotocols=None):
        communicator = WebsocketCommunicator(
            application, f"/ws/chat/{conversation_id}/", subprotocols=subprotocols
        )
        connected, _ = await communicator.connect()
        await communicator.disconnect()
        return connected

    def test_anonymous_connection_rejected(self):
        connected = async_to_sync(self._try_connect)(self.convo.id)
        self.assertFalse(connected)

    def test_invalid_token_rejected(self):
        connected = async_to_sync(self._try_connect)(
            self.convo.id, subprotocols=["auth.token", "not-a-valid-jwt"]
        )
        self.assertFalse(connected)

    def test_participant_can_connect(self):
        token = _token_for(self.alice)
        connected = async_to_sync(self._try_connect)(
            self.convo.id, subprotocols=["auth.token", token]
        )
        self.assertTrue(connected)

    def test_non_participant_rejected(self):
        token = _token_for(self.outsider)
        connected = async_to_sync(self._try_connect)(
            self.convo.id, subprotocols=["auth.token", token]
        )
        self.assertFalse(connected)
