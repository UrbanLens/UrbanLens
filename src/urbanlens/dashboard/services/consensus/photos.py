"""In-round photo capture for Consensus - reuses the existing upload pipeline.

Photo *selection* (which existing photos are eligible to appear in a
``PHOTO_COORDINATES`` round) applies the same ``Image.wiki``-non-null
consent gate SpotGuessr's own photo selection uses (see
``services.spotguessr.photos``) before ever showing one player's photo to
another - a prior privacy bug (fixed in commit ``afc7ee8b``) leaked private
pin photos into other players' game sessions when this gate was missing.
This module only handles the other direction: recording a photo a player
uploads *during* a round.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from urbanlens.dashboard.models.consensus.model import ConsensusRoundPhoto

if TYPE_CHECKING:
    from urbanlens.dashboard.models.consensus.model import ConsensusRound
    from urbanlens.dashboard.models.images.model import Image
    from urbanlens.dashboard.models.profile.model import Profile


def record_in_round_upload(round_: ConsensusRound, image: Image, profile: Profile) -> ConsensusRoundPhoto:
    """Record that ``image`` was captured/uploaded during ``round_`` by ``profile``.

    Attaches the photo to this round's wiki directly if it isn't already
    attached anywhere - uploading a photo *during a Consensus round* is an
    explicit, unambiguous "share this with the wiki" action, unlike a
    profile's ordinary pin-gallery uploads (which stay private until
    explicitly shared) - so it's immediately eligible for future
    ``PHOTO_COORDINATES`` rounds via the same ``wiki``-non-null gate.
    """
    if image.wiki_id is None:
        image.wiki = round_.wiki
        image.save(update_fields=["wiki", "updated"])
    return ConsensusRoundPhoto.objects.create(round=round_, image=image, profile=profile)
