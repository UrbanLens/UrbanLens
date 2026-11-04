"""A user-drawn pin boundary created, edited or cleared regroups that user's pins."""

from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from urbanlens.dashboard.models.boundary.model import Boundary


def _profile_id(instance: Boundary) -> int | None:
    return instance.pin.profile_id if instance.pin_id and instance.pin is not None else None


@receiver(post_save, sender=Boundary, dispatch_uid="boundary_nest_pins_on_save")
def nest_pins_on_boundary_save(sender: type[Boundary], instance: Boundary, **kwargs) -> None:
    """Regroup after a drawn polygon is written."""
    if (profile_id := _profile_id(instance)) is None:
        return
    from urbanlens.dashboard.services.pins.boundary_nesting import request_nesting

    request_nesting(profile_id)


@receiver(post_delete, sender=Boundary, dispatch_uid="boundary_nest_pins_on_delete")
def nest_pins_on_boundary_delete(sender: type[Boundary], instance: Boundary, **kwargs) -> None:
    """Return pins the cleared boundary nested to the top level."""
    try:
        profile_id = _profile_id(instance)
    except Exception:
        return  # the pin itself is being deleted
    if profile_id is None:
        return
    from urbanlens.dashboard.services.pins.boundary_nesting import request_nesting

    request_nesting(profile_id)
