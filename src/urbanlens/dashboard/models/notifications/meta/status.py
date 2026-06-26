# Generic imports
from __future__ import annotations

from django.utils.translation import gettext as _

from urbanlens.dashboard.models.abstract.choices import TextChoices


class Status(TextChoices):
    """
    Choices used for recording the status of a notification.

    This is used as a class, and never instantiated.

    Examples:
            >>> if foo.status == Status.VALIDATED:
            >>> ...

            >>> if fo.status in Status.ready_statuses:
            >>> ...

            >>> def sample( status : Status ):
            >>> ...
            >>> sample(Status.READY) # param is str("ready")

    """

    UNREAD = "unread", _("Notification is unread: has not been seen.")
    READ = "read", _("Notification has been seen.")
    DISMISSED = "dismissed", _("Notification was dismissed.")
