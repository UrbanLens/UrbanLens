# I7 — What a block does to a group chat or trip both people are in

`id: I7` · `status: accepted` · `updated: 2026-09-30`

**Ruled by Jess 2026-09-30:** 1b and 2b. Adding someone who has a block with an existing member is allowed, and the two people's messages and presence are hidden from each other. A block placed while both are already members hides each from the other from then on.

Today a block (`services/social/friendship.py:block_profile`) only revokes safety partners, pending pin shares and
map shares. Group chats and trips ignore it (N29 G4-1, G4-2):

- Two people who blocked each other keep reading each other's group messages, and both keep the group key.
- A third person who can message both can create a group, or add a member, that puts them together.

The same actor-only check sits at group create and add (`services/messaging/group_chats.py`) and at trip member
add and invite (`services/trips/trip_membership.py`, `services/trips/trip_invitations.py`).

## The two questions

**1. Adding a member who has a block with an existing member.** Options:

- **a. Refuse, with the same generic message as any other refusal.** This protects the blocker. But the person
  adding learns that one of the existing members cannot be joined with this person. That leaks the existence of a
  block, though not its direction.
- **b. Allow the add, but hide the pair's messages and presence from each other.** Nothing leaks to the adder, but
  both sides need filtering, and an E2EE group key still reaches both.
- **c. Allow it, as today.**

**2. A block placed while both are already members.** Options:

- **a. Nothing changes in the group** (today).
- **b. Hide each other's messages in that group from then on**, and rotate the group key if the product wants the
  blocked person unable to read new messages at all. `GroupKey` already refuses keys held by non-members, so a
  rotation is cheap.
- **c. Remove the blocked person from groups the blocker owns**, and apply b elsewhere.

## Suggested default

1a for trips, where membership is visible to everyone anyway, so the leak is small. 1b plus 2b for group chats.
Either way, one pairwise "may these two share a space" check built on `Profile.are_blocked` belongs in one place,
batched per add like `visible_profile_pks`' block veto. Every create, add and invite path would call it.
