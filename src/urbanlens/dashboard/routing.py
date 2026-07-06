from django.urls import path

from urbanlens.dashboard.consumers import RequestStatusConsumer, SafetyCheckinChatConsumer

websocket_urlpatterns = [
    path("ws/request_status/", RequestStatusConsumer.as_asgi()),
    path("ws/safety/checkin/<uuid:checkin_uuid>/chat/", SafetyCheckinChatConsumer.as_asgi()),
    path("ws/safety/contact/<uuid:token>/chat/", SafetyCheckinChatConsumer.as_asgi()),
]
