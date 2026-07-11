from __future__ import annotations

from typing import Any

from model_bakery.baker import Baker


class SignalSafeBaker(Baker):
    """A ``model_bakery`` Baker that tolerates the ``Profile`` auto-create signal.

    ``dashboard.models.profile.signals.create_user_profile`` runs on every
    ``User`` post_save and creates that user's ``Profile`` via
    ``get_or_create``. When ``baker.make()`` builds a ``Profile`` itself
    (directly, or transitively while filling an unrelated FK such as
    ``Pin.profile``), it first auto-generates a related ``User``, which fires
    that signal and inserts the ``Profile`` row - then baker tries to insert
    its *own* ``Profile`` instance for the same user, violating the
    one-profile-per-user unique constraint. Configured as ``BAKER_CUSTOM_CLASS``
    in test settings so every caller is covered without touching individual
    tests.
    """

    def instance(
        self,
        attrs: dict[str, Any],
        _commit: bool,
        _save_kwargs: dict[str, Any] | None,
        _from_manager: Any,
    ):
        from urbanlens.dashboard.models.profile.model import Profile

        if _commit and self.model is Profile:
            user = attrs.get("user")
            if user is not None:
                existing = Profile.objects.filter(user=user).first()
                if existing is not None:
                    for key, value in attrs.items():
                        if key != "user":
                            setattr(existing, key, value)
                    existing.save(**(_save_kwargs or {}))
                    return existing

        return super().instance(attrs, _commit, _save_kwargs, _from_manager)
