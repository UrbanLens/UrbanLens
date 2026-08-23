# Storage bonuses for community contribution — findings before implementation

Survey and adversarial review of the brief to replace the all-or-nothing wiki
quota exemption with graduated bonuses. **Not yet implemented.** One finding
needs a decision from the owner first, and several traps are recorded here so
they are not rediscovered.

## What the current system actually is

- **Quota is derived, never stored.** `get_storage_used_bytes` is a live
  `SUM(Image.file_size)` filtered on `quota_exempt_reason=""`, recomputed on
  every check. No counter, no cache (`storage.py` imports `django.core.cache`
  and never uses it).
- So today's exemption is a **filter, not an amount**. Graduated bonuses cannot
  be expressed by it; they need a real quantity, which is a schema change.
- **Downvotes already exist.** `MediaRelevance` is three-state per
  (profile, location, source, item_key): `is_relevant=True` up, `False` down,
  absent neutral.
- It is keyed by **location, not wiki**, and by a SHA-1 of the image *URL*
  rather than the `Image` row. It is current-state, not an event log — a
  withdrawn vote deletes the row. So a true per-wiki *vote rate* is not
  derivable; the honest substitute is standing marks per contributed photo.

## The finding that needs a decision

**"Shared to a wiki is immediately free" plus "anyone can create a wiki" is
unlimited free storage.**

`models/pin/signals.py` enqueues `ensure_draft_wiki_for_location` on *every pin
creation*, and `services/wiki/wiki_creation.py` promotes that draft to official
with one click. Wikis are not scarce and not vetted. So a bonus that equals the
stored size the instant a photo is attached, with no other signal required,
means: create a pin, create its wiki, attach every photo, pay nothing. Ever.

The brief's own rule produces this, so it is a product decision rather than an
implementation bug. Options, none of them free:

1. **Gate the base bonus on earned signal.** The bonus reaches the full stored
   size only with votes; before that it is a fraction. Closest to today's
   behaviour, and the least generous reading of the brief.
2. **Keep "immediately free" and make wikis scarce.** Requires vetting or a
   contribution threshold before a wiki can pay bonuses. A much larger change,
   and it changes what a wiki is.
3. **Cap total bonus per account** — a bonus pool proportional to the paid
   quota, so the scheme cannot exceed a known ceiling however it is worked.
4. **Require the photo to be visible and seen** before any bonus, not merely
   attached.

(3) composes with any of the others and is the cheapest ceiling on the whole
scheme; (1) is the smallest change that keeps the quota meaningful.

## Traps found, all verified against the code

- **`Image.wiki` does not mean "deliberately contributed".**
  `services/photos/uploads.py:_owner_fields` stamps
  `wiki=Wiki.objects.get_for_location(location)` on *every pin-gallery upload*
  at a location with a wiki — the docstring says so, it is what makes "send to
  wiki" a no-op. Keying a bonus off `Image.wiki` would make most pin photos free.
- **...but those photos are already published.** `controllers/wiki_media.py`
  renders the wiki Photos panel as `Image.objects.filter(wiki=wiki)`, so
  auto-stamped pin photos are *already* on the wiki as far as a visitor is
  concerned. "Deliberate vs automatic" is not a distinction the site currently
  makes to users, and inventing it in the bonus rules alone would be incoherent.
- **`send_to_wiki` cannot supply the deliberate signal** as written:
  `images.exclude(wiki=wiki).update(wiki=wiki)` is a no-op for photos the
  upload already stamped.
- **A cascade would reopen the un-sharing defect through a path with no code in
  it.** Deleting a wiki cascades its `ImageAttachment` rows, firing no recompute,
  so a denormalised bonus on the image would survive. Counted bytes must be
  computed against live attachment rows, not a denormalised copy.
- **Re-encoding does not strip EXIF for HEIC.** `pillow_heif` copies `img.info`
  (including `exif`) on save, so a rendition written "with no exif kwarg" keeps
  its metadata — including GPS. Any private rendition must pass
  `exif=None, xmp=None, comment=None` explicitly, and be tested on a HEIC.
- **There is no `pre_delete`/`post_delete` receiver for `Image` anywhere.** File
  cleanup is not structural, so a second rendition would leak on delete unless
  one is added.
- **A private rendition directory needs a mounted volume.** `docker-compose.yml`
  mounts only `static_volume`, `media_volume` and `backups`; a path under the
  source tree is neither shared with the workers nor persistent.
- **Every `/media/...` request goes through one `MediaGateView`**, dev and
  production alike. "Unreachable even by guessing" means outside `MEDIA_ROOT`,
  not merely unlinked.

## Sequencing

The accounting change (bonus as an amount, computed from live attachments) is
separable from anti-gaming and from relevance-upscaling, and should land and be
observed first. The upscaling work depends on a storage location that does not
exist yet, and on an `Image` delete receiver that has never existed.
