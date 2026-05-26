from django.urls import re_path
from .consumers import ChatConsumer, PageChatConsumer

websocket_urlpatterns = [
    re_path(r"ws/chat/(?P<conversation_id>\d+)/$", ChatConsumer.as_asgi()),
    # Page group chat (FE-2): per-page broadcast group, receive-only on the
    # client — sends still go through the REST send_page_chat_message view.
    re_path(r"ws/page-chat/(?P<page_id>\d+)/$", PageChatConsumer.as_asgi()),
]
