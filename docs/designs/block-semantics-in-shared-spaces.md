# I7 — A block hides the pair from each other inside a group chat or trip they share, from the moment it is placed

> **Written by a Claude agent. Not authoritative.**
>
> This records what one automated session measured or believed on the date
> below. It was not independently reviewed, its numbers may be stale, and the
> code may have moved. Re-run the measurement before relying on it, and
> **rewrite this file** when you do — do not add a correction underneath the
> old claim. When this file and the code disagree, the code wins.

`id: I7` · `status: absorbed` · `updated: 2026-09-30` · supersedes this file's earlier options write-up

**Ruled by Jess 2026-09-30: 1b and 2b.** Adding someone who has a block with an existing member is allowed,
and the two people's messages and presence are hidden from each other. A block placed while both are already
members hides each from the other from then on.

Before this, a block (`services/social/friendship.py:block_profile`) only revoked safety partners, pending pin
shares and map shares; group chats and trips ignored it (N29 G4-1, G4-2).

## The rule as built

For two people joined by a block, in either direction:

- **Presence** is hidden at once: neither appears in the other's member list, roster or member count.
- **Content** is hidden from the block's time on: anything either posts at or after `Friendship.blocked_at`
  (messages, comments, replies, reactions, the credit for adding a trip stop) is not shown to the other, not
  counted for them and not announced to them. Anything posted before stays visible.
- **Adding is unchanged.** There was no member-versus-member refusal to remove. The refusals that remain at
  group create/add (`can_direct_message`) and trip add (`add_member_by_username`) are about the *actor's own*
  block with the person being added, answer exactly like any other refusal, and are not what 1a/1b decided.
- The cutoff belongs to the live block. Lifting a block hides nothing any more, so whatever the pair posted
  during it becomes visible to both; blocking again starts a new cutoff. Blocking back while already blocked
  keeps the first time.

## The cutoff

`Friendship` had only `created`/`updated`, and `updated` moves on unrelated writes, so it could not serve.
`Friendship.blocked_at` (squashed into migration `0032_v0_8_0`) is set by `Friendship.save()` exactly while
`status` is `BLOCKED`, kept when a block is re-applied, and cleared when the row leaves `BLOCKED`. Check
constraint `friendship_blocked_at_only_while_blocked` (migration `0118`, separate so it is not added in the
transaction that rewrote the rows) holds the same both ways, which catches a queryset `update()` that skips
`save()`: a blocked row without a time, or a lifted one keeping a stale time that the next block would
reuse. Existing blocks were backfilled from their row's `updated`, the closest record there is; not measured
against real data.

## The one helper

`models/friendship/blocks.py:SharedSpaceBlocks` — one query per viewer (`for_viewer`) or per membership
(`for_profiles`), both directions, the same block veto `Profile.accepting_direct_messages_pks` now also reads.
`hides_profile`, `hides_content(author, created)`, `hidden_from_at(created)`, and the SQL forms
`exclude_hidden` / `exclude_hidden_profiles`. Every path below calls it.

## Where it applies

Group chats:

- `GroupMessageQuerySet.visible_window` — the thread, older pages, the web thread partial and the external API
  thread all read through it.
- `services/messaging/group_chats.py`: `group_inbox_rows` (preview, unread, member count),
  `unread_group_conversation_count`, `_notify_group_message` (no notification, and the "already unread" check
  skips what the member cannot see), `broadcast_group_message` (no live frame), `delete_group_message` (no
  tombstone event), `toggle_group_reaction` (refuses a message outside the reactor's window; no event for a
  member the message is hidden from; per-member summary), `share_pin_in_group_message` (no share row),
  `visible_memberships` (member lists), `hidden_by_block` (single-message lookups).
- `services/messaging/direct_messages.py:reaction_summary` leaves out reactions a block hides.
- Web: thread header count, members dialog, message delete and share-respond answer 404 for a hidden message.
- External API: conversations, groups list/detail counts, members, thread, reaction (looked up through the
  caller's window, so a pre-join message is now a 404 too), delete (404, not 403, for a hidden message).
- Presence: group chats have no typing indicator, online status or read receipt, so the member list and counts
  are the whole of it.

Trips:

- `TripCommentQuerySet.visible_to`, `build_comment_tree` (and its reaction tallies), `trip_comment_is_visible`
  (so reactions, the comment-image media gate and the panel agree), `visible_comment_count`.
- `add_comment` refuses a reply to a hidden comment and does not notify across a block; `set_comment_reaction`
  does not notify across one; `comment_reactions_for` serves the web reaction row.
- `list_members` (members panel, API members, API detail), `TripQuerySet.for_list_page` (list-card roster,
  `member_count`, `comment_count`).
- `build_activity_rows` withholds `added_by` for a stop the other added after the block; the stop itself stays
  on everyone's itinerary, since hiding it would break the shared plan. Template and API serializer read the
  row's `added_by`.
- Search: the comments provider drops hidden comments, and the trips provider no longer matches a trip on a
  word only a hidden comment holds.
- The account data export's trip roster (`services/import_export/export.py:_export_trips`) leaves the other out.

## E2EE groups

The group key still reaches both people, so the server withholding the ciphertext is what hides a message. No
endpoint found hands a member the ciphertext of a message hidden from them: thread pages, older pages, the
inbox preview, the live frame, notifications (which never carry ciphertext) and the group-key endpoint (which
serves only the caller's own envelopes) were each checked by
`test_block_hides_pair_in_group_chats.py:CiphertextIsUnreachableTests` and `WebSocketTests`. No key rotation
was added.

What the key endpoint (`controllers/e2ee.py:E2EEGroupKeyView`) still reveals is presence: its `members` list
carries an opaque token and public key for every active member, hidden ones included, because a rotating
client must seal the new key for all of them. So the member count there is one more than the roster shows,
and a client that already holds the other's public key can recognise it. `group_e2ee_ready` likewise counts
every member. Closing that would need someone other than the viewer to seal the hidden member's envelope.

## What this does not do

- A group creator or trip creator who blocked a member can no longer see them in the roster, so cannot remove
  them from the UI. Removing is option 2c, which was not chosen.
- Shared state stays shared: a group rename, a trip edit, a stop's title and notes, trip settings.
- Trip `creator` and group `creator_slug` fields are not masked by this.
- A trip creator's list of pending email invitations (`trip_invitations.invitations_visible_to`) still names
  the inviter, including one the other sent after the block; hiding it would also take away the creator's
  way to withdraw it.
- For trip comments, the existing comment-visibility veto (`Profile._barred_subject_pks`) already hid *all* of
  a blocker's comments from the person they blocked, including earlier ones; that is kept, so on trips the
  blocked person sees less than 2b alone requires. The render pass applied that veto but `visible_to` (SQL)
  did not, so the comment badge counted the blocker's earlier comments and `build_comment_tree` logged "gates
  disagree"; `visible_to` now carries it. Its other half, inactive authors, reads as still render-only; not tested.
- Direct messages are out of scope: a blocked person's typing frame addressed to the blocker, and the blocker's
  view of their online status in an old thread, were not changed.

## Tests

`test_shared_space_blocks.py` (the cutoff and the helper), `test_block_hides_pair_in_group_chats.py` (services,
web views, external API, E2EE ciphertext, and the real `DirectMessageConsumer` over `WebsocketCommunicator`),
`test_block_hides_pair_in_trips.py`. Run against `release/v_0_8_0` without the implementation, 50 of the 65
group and trip tests failed and the helper file did not import; the 15 that passed there are the guards (adds
allowed, earlier content kept, bystanders unaffected). The two search tests and the export test were written
after that run and each failed against the `release/v_0_8_0` copy of the file it covers.
`test_a_lifted_block_keeping_its_time_is_refused_by_the_database` and
`test_the_blocked_persons_count_matches_what_is_shown` each failed on this branch before the constraint was
made two-way and `visible_to` given the older veto.
