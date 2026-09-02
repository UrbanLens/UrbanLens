"""Serializers for the external API's AI assistant domain."""

from __future__ import annotations

from rest_framework import serializers

from urbanlens.dashboard.services.ai.assistant import MAX_MESSAGE_CHARS


class AssistantHistoryEntrySerializer(serializers.Serializer):
    """One prior turn in the conversation, carried by the client between requests.

    The assistant is stateless server-side for this API - unlike the web
    chat, which keeps history in the Django session, a bearer-token client
    has no session to keep it in. A client resends the ``history`` this
    domain returns, unmodified, as the next call's input.
    """

    role = serializers.ChoiceField(choices=["user", "assistant"])
    content = serializers.CharField()


class AssistantMessageRequestSerializer(serializers.Serializer):
    """A chat turn: the new message plus the conversation so far."""

    message = serializers.CharField(max_length=MAX_MESSAGE_CHARS, allow_blank=False)
    history = AssistantHistoryEntrySerializer(many=True, required=False, default=list)


class AssistantProposalSerializer(serializers.Serializer):
    """One write-tool proposal awaiting the caller's confirmation.

    Never carries the tool's ``args`` - those stay server-side
    (``services.ai.turns.store_turn_proposals``) until a confirm request
    replays them for real; this is only what a client needs to render a
    confirm control and address it (``n``, this domain's own
    ``POST /assistant/turn/<turn_id>/confirm/<n>/``).
    """

    n = serializers.IntegerField(read_only=True)
    tool = serializers.CharField(read_only=True)
    confirm_label = serializers.CharField(read_only=True)


class AssistantMessageResponseSerializer(serializers.Serializer):
    """The assistant's reply to one chat turn, plus the history to resend next time."""

    reply = serializers.CharField(read_only=True)
    actions = serializers.ListField(child=serializers.CharField(), read_only=True)
    proposals = AssistantProposalSerializer(many=True, read_only=True, default=list)
    history = AssistantHistoryEntrySerializer(many=True, read_only=True)


class AssistantProposalConfirmResponseSerializer(serializers.Serializer):
    """Result of confirming one proposal - the write either ran or it didn't."""

    status = serializers.ChoiceField(choices=["done", "error"], read_only=True)
    message = serializers.CharField(read_only=True)


class AssistantTurnPendingSerializer(serializers.Serializer):
    """Body of a 202 response: the turn was enqueued (or is still running) on ai-worker.

    ``turn_id`` is only present on the initial POST's 202 - the poll
    endpoint's own URL already carries it, so echoing it back there would be
    redundant.
    """

    turn_id = serializers.CharField(read_only=True, required=False)
    ready = serializers.BooleanField(default=False)
    poll_after_seconds = serializers.IntegerField(read_only=True)


class AssistantResetResponseSerializer(serializers.Serializer):
    """Body of the reset endpoint: always an empty history - see AssistantResetView."""

    history = AssistantHistoryEntrySerializer(many=True, read_only=True)
