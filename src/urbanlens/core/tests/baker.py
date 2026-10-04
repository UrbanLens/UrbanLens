from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any

from model_bakery.baker import Baker
from model_bakery.random_gen import baker_random

if TYPE_CHECKING:
    from django.db.models import Field


class _NorthernLatitude:
    """A latitude between 0 and 90 degrees, as a field of either kind stores it.

    Baker's own decimal for a ``DecimalField(max_digits=9, decimal_places=6)`` runs to 99.999999, so about one baked
    location in ten was past the pole, and code that checks a coordinate refused it at random (P50). Its longitude,
    0 to 99.999999, is always real, so it is left to baker.
    """

    def __init__(self) -> None:
        self.required = [self._decimal_places]

    @staticmethod
    def _decimal_places(field: Field) -> tuple[str, int | None]:
        return "decimal_places", getattr(field, "decimal_places", None)

    def __call__(self, decimal_places: int | None = None) -> Decimal | float:
        value = baker_random.uniform(0, 89.999999)
        return value if decimal_places is None else Decimal(f"{value:.{decimal_places}f}")


class SignalSafeBaker(Baker):
    """A ``model_bakery`` Baker that tolerates the ``Profile`` auto-create signal.

    ``dashboard.models.profile.signals.create_user_profile`` runs on every ``User`` post_save and creates that
    user's ``Profile`` via ``get_or_create``.

    It also bakes a latitude on the globe (``_NorthernLatitude``).
    """

    attr_mapping: dict[str, Any] = {"latitude": _NorthernLatitude()}

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
