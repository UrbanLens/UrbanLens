"""Account data deletion helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import json
from pathlib import Path
import shutil
import zipfile

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import serializers
from django.db import models, transaction
from django.db.models.deletion import Collector
from django.utils import timezone

RETENTION_DAYS = 31


@dataclass(frozen=True)
class DataDeletionResult:
    request_id: str
    purge_after: object
    archive_path: str


def deletion_archive_root() -> Path:
    """Return the private directory used for temporary deletion restore archives."""
    root = Path(getattr(settings, "UL_DATA_DELETION_DIR", Path(settings.MEDIA_ROOT) / "data_deletions"))
    root.mkdir(parents=True, exist_ok=True)
    return root


def request_user_data_deletion(user_id: int) -> DataDeletionResult:
    """Archive and remove all data reachable from the user's account.

    The archive is retained for at least ``RETENTION_DAYS`` so an operator can
    restore it with Django's deserializers before the archive is purged.
    """
    from urbanlens.dashboard.models.data_deletion import DataDeletionRequest

    User = get_user_model()
    with transaction.atomic():
        user = User.objects.select_for_update().get(pk=user_id)
        purge_after = timezone.now() + timedelta(days=RETENTION_DAYS)
        deletion = DataDeletionRequest.objects.create(
            user_id_snapshot=user.pk,
            username=user.get_username(),
            email=user.email,
            purge_after=purge_after,
            status=DataDeletionRequest.Status.ARCHIVING,
        )

        collector = Collector(using="default")
        collector.collect([user])
        archive_path = deletion_archive_root() / f"deletion-{deletion.request_id}.zip"
        payload = []
        for model, objects in collector.data.items():
            payload.extend(json.loads(serializers.serialize("json", list(objects))))

        files_to_remove = []
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for model, objects in collector.data.items():
                for obj in objects:
                    for field in obj._meta.fields:
                        if not isinstance(field, models.FileField):
                            continue
                        file_value = getattr(obj, field.name, None)
                        if not file_value or not getattr(file_value, "name", ""):
                            continue
                        try:
                            if file_value.storage.exists(file_value.name):
                                archive_name = f"files/{model._meta.label_lower}/{obj.pk}/{field.name}/{Path(file_value.name).name}"
                                with file_value.storage.open(file_value.name, "rb") as fh:
                                    zf.writestr(archive_name, fh.read())
                                files_to_remove.append((file_value.storage, file_value.name))
                        except OSError:
                            continue

            zf.writestr("manifest.json", json.dumps({
                "request_id": str(deletion.request_id),
                "user_id": user.pk,
                "username": user.get_username(),
                "email": user.email,
                "created": timezone.now().isoformat(),
                "purge_after": purge_after.isoformat(),
                "retention_days": RETENTION_DAYS,
            }, indent=2))
            zf.writestr("objects.json", json.dumps(payload, indent=2))

        deletion.archive_path = str(archive_path)
        deletion.status = DataDeletionRequest.Status.ARCHIVED
        deletion.save(update_fields=["archive_path", "status", "updated"])
        user.delete()
        for storage, name in files_to_remove:
            try:
                if storage.exists(name):
                    storage.delete(name)
            except OSError:
                continue
        deletion.status = DataDeletionRequest.Status.DELETED
        deletion.deleted_at = timezone.now()
        deletion.save(update_fields=["status", "deleted_at", "updated"])
        return DataDeletionResult(str(deletion.request_id), deletion.purge_after, deletion.archive_path)


def purge_expired_deletion_archives(*, now=None) -> int:
    """Permanently remove deletion restore archives whose retention has expired."""
    from urbanlens.dashboard.models.data_deletion import DataDeletionRequest

    now = now or timezone.now()
    purged = 0
    for deletion in DataDeletionRequest.objects.filter(purge_after__lte=now).exclude(status=DataDeletionRequest.Status.PURGED):
        if deletion.archive_path:
            path = Path(deletion.archive_path)
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            elif path.exists():
                path.unlink()
        deletion.status = DataDeletionRequest.Status.PURGED
        deletion.purged_at = now
        deletion.save(update_fields=["status", "purged_at", "updated"])
        purged += 1
    return purged
