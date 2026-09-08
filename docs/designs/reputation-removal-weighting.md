# D9 — A moderator's removal costs reputation, but only slightly, and reversibly

`id: D9` · `status: accepted` · `updated: 2026-09-07`

**Decided 2026-09-07 by Jess**, answering P86's question - should a contribution deleted by somebody
else stop counting toward its contributor's reputation?

> "Sure, but ideally in a way that can be reversed or weighted differently in the future. Tracking
> reputation isn't the end of the world to implement imprecisely - it is best effort only - so no
> major architectural decisions should be made to account for reputation tracking. However, that
> said, an ideal system would allow tracking reputation with granularity, where metrics that
> influenced it could be weighted using a heuristic, and a ML model could later be built on it and
> used when it beat the heuristic. Some elements of this already exist, some are outsourced to
> REData, and so on. Overall... I'm fine with a moderator's removal impacting a contributor's
> reputation, but I would lean toward it impacting it only very slightly for the time being."

Four constraints follow, and the first two rule out the implementation P86 assumed:

1. **Slight, not total.**
2. **Reversible, and re-weightable later** without a data migration or losing what happened.
3. **Best effort.** No architecture is to be reshaped for reputation's sake.
4. **Granular enough to learn from** - per-event, so a heuristic weight can later be replaced by a
   model where the model wins.

## What this rules out

**`retract_event` is the wrong mechanism here**, which is worth stating because it is the obvious
one and P86 proposed it. `retracted` is a boolean: it takes the contribution's whole value out of
the total. That is exactly right for a *withdrawal* - the contributor ended their own contribution,
so it stops counting entirely (P55, fixed 2026-09-06) - and it is the opposite of "only very
slightly".

**Re-scoring the row is also wrong.** `score_event` scores an *unscored* row, applying diminishing
returns and the per-rule/per-wiki caps as it goes; running it again over a scored row would re-apply
both. The value is written once, deliberately.

## The shape that fits

A per-event **weight**, applied where the total is summed rather than where the value is written:

- `ReputationEvent.weight`, a `DecimalField` defaulting to 1, and `ReputationEventQuerySet.counting()`
  summing `value * weight` instead of `value`.
- A moderator removal sets it to something near 1 (0.9 is a starting number, not a researched one).
- Reversal is setting it back to 1. Re-weighting later is changing the constant that sets it, with
  no migration and no lost history - the event, its `value`, and the reason it was reduced all
  remain.
- A future model sets per-event weights directly. That is the granularity constraint (4), and it
  costs one column today rather than a redesign then.

**`lifetime_earned` must not move.** `recompute_total` is explicit that it ignores retraction so
that reverting somebody's contributions cannot take away access they already had; a weight has to
be excluded from it for the same reason. A moderator's removal should cost *standing*, never
already-granted access. That is also what keeps this decision cheap to reverse: the only thing it
moves is a number that is recomputed from the ledger anyway.

**Cost:** one nullable-with-default column, one migration, one `Sum` expression, and a call at the
removal sites. That is under the bar constraint (3) sets - it adds a field to a model whose whole
job is this, rather than shaping anything else around reputation.

## What is deliberately not decided

- **The number.** 0.9 is a placeholder. Constraint (2) is what makes picking it cheap to get wrong.
- **Which removals count.** A moderator deleting a photo is the case in hand. A cascade - deleting a
  wiki removes its comments, and those contributors did nothing - is not obviously the same thing,
  and P86 lists it as an open sub-question. A weight makes it safe to start with the narrow case.
- **Whether the contributor is told.** Out of scope here.

## Status

**Built 2026-09-07**, and building it corrected two of the premises the decision was given under.
Neither changes the decision; both change what it applies to.

**Users can still delete a wiki - just not a top-level one.** There is no top-level wiki-delete
route, as the sign-off assumed. But `location.wiki.detail_pin.edit` has a `delete()` that removes a
**child `Wiki`** - a detail pin *is* a child wiki - and `Wiki.parent_wiki` is `CASCADE`, so it takes
the whole subtree. `Comment.wiki` is `CASCADE` too, so other people's comments go with it. The
cascade case the sign-off treated as hypothetical is live, and
`CascadeKeepsBenefitsTests` pins the ruling on it: a cascade retracts nothing and weights nothing.

**There is no site to apply the weight to yet.** Every path by which a reputation-earning
contribution disappears was checked: the wiki photo delete is owner-only and already passes
`withdrawn_by_contributor=True`; `WikiCommentDeleteView` refuses a non-author;
`detach_image_from_wiki` has one caller. Nobody can currently remove somebody else's scored
contribution, so `MODERATED_REMOVAL_WEIGHT` has no caller - it is the mechanism, waiting for the
first moderation path that needs it. That is the point of building it now rather than later: the
column and the sum are in place, so the path that needs it is a one-line change instead of a
migration argument.

**What the implementation actually fixed** was therefore not the moderator case at all. It was that
a contributor **deleting their own wiki comment** kept its points, while withdrawing a photo
retracted them - the same act, two different answers. `WikiCommentDeleteView` is author-only, so
every deletion through it is a withdrawal; it retracts now, exactly as the photo path does.

Shipped: `ReputationEvent.weight`/`weight_reason` (migration `0032_v0_8_0`, which the v0.8.0 squash folded 0056 into), `total_value()` summing
`value * weight`, `lifetime_earned` left untouched, `weight_events_for_target`, and
`MODERATED_REMOVAL_WEIGHT = 0.9` as the placeholder the sign-off confirmed.
