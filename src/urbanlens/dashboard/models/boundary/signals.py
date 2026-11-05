"""A user-drawn pin boundary created, changed or cleared regroups the owner's affected pins."""

from django.db.models.signals import post_delete, post_save, pre_save
from django.dispatch import receiver

from urbanlens.dashboard.models.boundary.model import Boundary


@receiver(pre_save, sender=Boundary, dispatch_uid="boundary_remember_drawn_polygon")
def remember_drawn_polygon(sender: type[Boundary], instance: Boundary, **kwargs) -> None:
    """Keep the stored outline so post-save can tell a redraw from a refit of the generated one."""
    update_fields = kwargs.get("update_fields")
    instance.previous_drawn_polygon = None
    instance.drawn_polygon_changed = False
    if not instance.pin_id:
        return
    if update_fields is not None and "polygon" not in update_fields:
        return
    previous = Boundary.objects.filter(pk=instance.pk).values_list("polygon", flat=True).first() if instance.pk else None
    instance.previous_drawn_polygon = previous
    instance.drawn_polygon_changed = (previous is None) != (instance.polygon is None) or (previous is not None and instance.polygon is not None and not previous.equals_exact(instance.polygon))


@receiver(post_save, sender=Boundary, dispatch_uid="boundary_nest_pins_on_save")
def nest_pins_on_boundary_save(sender: type[Boundary], instance: Boundary, **kwargs) -> None:
    """Regroup after a drawn polygon is written or cleared."""
    if not getattr(instance, "drawn_polygon_changed", False) or (profile_id := instance.profile_id) is None or instance.pin_id is None:
        return
    from urbanlens.dashboard.services.pins.boundary_nesting import request_boundary_nesting

    request_boundary_nesting(profile_id, instance.pin_id, instance.previous_drawn_polygon, instance.polygon)


@receiver(post_delete, sender=Boundary, dispatch_uid="boundary_nest_pins_on_delete")
def nest_pins_on_boundary_delete(sender: type[Boundary], instance: Boundary, **kwargs) -> None:
    """Return pins the cleared boundary nested to the top level."""
    if not instance.pin_id or instance.polygon is None or (profile_id := instance.profile_id) is None:
        return
    from urbanlens.dashboard.services.pins.boundary_nesting import request_boundary_nesting

    request_boundary_nesting(profile_id, instance.pin_id, instance.polygon, None)
