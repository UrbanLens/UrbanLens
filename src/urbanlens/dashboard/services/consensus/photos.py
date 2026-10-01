"""In-round photo capture for Consensus - reuses the existing upload pipeline.
This module only handles the other direction: recording a photo a player uploads *during* a round."""

from __future__ import annotations

from typing import TYPE_CHECKING

from urbanlens.dashboard.models.consensus.model import ConsensusRoundPhoto

if TYPE_CHECKING:
    from urbanlens.dashboard.models.consensus.model import ConsensusRound
    from urbanlens.dashboard.models.images.model import Image
    from urbanlens.dashboard.models.profile.model import Profile


def record_in_round_upload(round_: ConsensusRound, image: Image, profile: Profile) -> ConsensusRoundPhoto:
    """Record that ``image`` was captured/uploaded during ``round_`` by ``profile``."""
    if image.wiki_id is None:
        image.wiki = round_.wiki
        image.save(update_fields=["wiki", "updated"])
    return ConsensusRoundPhoto.objects.create(round=round_, image=image, profile=profile)
