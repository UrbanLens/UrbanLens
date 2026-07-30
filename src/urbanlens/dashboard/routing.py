from django.urls import path

from urbanlens.dashboard.consumers import (
    ConsensusSessionConsumer,
    DirectMessageConsumer,
    GameSessionConsumer,
    SafetyCheckinChatConsumer,
    TriviaSessionConsumer,
    UserNotificationConsumer,
)

websocket_urlpatterns = [
    path("ws/notifications/", UserNotificationConsumer.as_asgi()),
    path("ws/messages/", DirectMessageConsumer.as_asgi()),
    path("ws/safety/checkin/<uuid:checkin_uuid>/chat/", SafetyCheckinChatConsumer.as_asgi()),
    path("ws/safety/contact/<uuid:token>/chat/", SafetyCheckinChatConsumer.as_asgi()),
    path("ws/spotguessr/session/<int:session_id>/", GameSessionConsumer.as_asgi()),
    path("ws/trivia/session/<int:session_id>/", TriviaSessionConsumer.as_asgi()),
    path("ws/consensus/session/<int:session_id>/", ConsensusSessionConsumer.as_asgi()),
]
