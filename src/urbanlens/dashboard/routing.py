from django.urls import path

from urbanlens.dashboard.consumers import RequestStatusConsumer

websocket_urlpatterns = [
    path("ws/request_status/", RequestStatusConsumer.as_asgi()),
]
