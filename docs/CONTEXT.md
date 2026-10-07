# UrbanLens

A mapping app where photographers and urban explorers keep private records of places they know, and share them only by explicit, consent-tracked copies. Community knowledge about a place lives on its wiki, which a user can reach only after proving they already know the place.

## Language

### Pins, locations and places

**Pin**:
One profile's private, personal record of a spot: their name, notes, icon, priority, ratings and visit state, pointing at a shared Location. Nothing on a pin is ever visible to another user; every route out is a copy.
_Avoid_: marker, location (for a user's record)

**Child pin**:
A pin nested under another of the same owner's pins (`Pin.parent_pin`) to mark a building, entrance, point of interest or hazard within it. The code's related name is `detail_pins` and one screen heading says "Detail Pins", but most of the UI says "child pin".
_Avoid_: detail pin, sub-pin

**Location**:
An immutable, shared record of one exact coordinate plus official data about it (address, official name, Google place link, cached external results). No user can query it directly: it is only ever served alongside something the caller may already see.
_Avoid_: place, address

**Place**:
The real-world thing a coordinate resolves onto: a parcel, a building or a site, with official geometry. It is the single answer to "is this the same place?" (ADR-0004), and it anchors the wiki. Many Locations resolve onto one Place.
_Avoid_: property, location, boundary

**Parcel**:
A Place of kind `PARCEL`: a tax-lot-shaped piece of land.
_Avoid_: lot, property

**Building**:
A Place of kind `BUILDING`: one structure, usually `PART_OF` the parcel it stands on.
_Avoid_: structure

**Site**:
A Place of kind `SITE`: a named area such as a campus or park that spans several parcels, usually an aggregate place.
_Avoid_: campus (as a kind)

**Campus**:
An informal word for a parcel that holds several buildings, or for a site aggregating several parcels. No model or kind carries the name; use Parcel or Site in code.

**Access domain**:
A maximal set of Places joined by `PART_OF` edges, identified by `Place.domain_root`. A pin anywhere in the domain grants access to every wiki in it, upward and downward.
_Avoid_: property, domain root (that is only the identifying row)

**Aggregate place**:
A Place with `MEMBER_OF` children. Locations never resolve onto it, and a profile earns its wiki only by holding access to every member.
_Avoid_: parent parcel

**Superseded place**:
A Place whose geometry no longer describes the ground, usually because a parcel split. It keeps its geometry for display and history, but no coordinate resolves onto it.
_Avoid_: archived place, old parcel

**Official geometry**:
A Place's boundary (`Place.geometry`), written only by the provider chain and boundary voting. It is the only geometry access is derived from. User- and community-drawn shapes live in `Boundary` and markup, and never grant access.
_Avoid_: boundary (unqualified), polygon

**Pin type**:
What a marker physically represents (location, parcel, building, entrance, point of interest, ...). For a pin on a Place it is derived from the Place, not chosen by the pin's owner.
_Avoid_: category (Category is a label kind)

**Alias**:
An extra name for a pin (`PinAlias`, visible only to the pin's owner) or a wiki (`WikiAlias`, visible to everyone with access). Its kind is official, nickname or alternate; its `source` says who supplied it, and `user` marks a person's alias.
_Avoid_: nickname (that is one kind), alternate name

**Name tier**:
How specifically an automatic name candidate identifies a property: encyclopedia, then historic register, site, building, point of interest, road. The tier is ranked before the admin's source priority, and a road or address never names a place (ADR-0020).
_Avoid_: name priority

### Wikis and access

**Wiki**:
The community page for one Place (`Wiki.place`, one-to-one). It holds what users edit together: name, description, aliases, security indicators, photos and stats. `Wiki.location` remains only for coordinates and URL routing. Despite the name it is not public: only profiles with wiki access can find or see it.
_Avoid_: community page (except in UI copy), public page, location page

**Child wiki**:
A wiki nested under another (`Wiki.parent_wiki`) following place lineage, such as a building's wiki under its parcel's. The UI calls them "community detail pins".
_Avoid_: community detail pin, sub-wiki

**Wiki access**:
The right to find and see a wiki. A profile holds it only if it has a pin on the wiki's Location, has a pin in the same access domain, covers an aggregate place, or holds a place access grant. Without access the wiki answers 404, never 403. The one implementation is `services/wiki/wiki_access.py`.
_Avoid_: earned credit, wiki permission, visibility

**Place access grant**:
A stored `PlaceAccessGrant` that keeps a profile's access after containment can no longer prove it. It is written only by the place backfill, by split processing, and when a profile views or contributes to a wiki it already has access to (ADR-0019). No API writes it.
_Avoid_: permission, membership

**Concealment**:
The per-viewer projection that shows a flagged viewer a wiki indistinguishable from the same place with no user contributions (ADR-0002). It never grants or removes access. The predicate `concealment_active` is still a stub that always returns False.
_Avoid_: hiding, shadow-ban, reputation gate

**Wiki edit**:
One recorded change to a wiki's editable fields (`WikiEdit`), stored as a from/to diff. A revert is a new edit carrying the inverted diff.
_Avoid_: revision (that is `BoundaryRevision`/article revisions)

**Community stat**:
A wiki field (danger, vulnerability, priority, rating) whose value comes from profiles' 1–5 votes (`WikiStatVote`) rather than from edits.
_Avoid_: rating (that is one stat)

### Sharing and privacy

**Pin share**:
An offer from one profile to another of a place's coordinates plus consented objective fields (`PinShare`). On acceptance it becomes the recipient's own independent pin, and the recipient never reaches the sender's pin. Shares may also be auto-recorded when a shared markup map, a DM's text or a trip activity reveals a place.
_Avoid_: suggestion, pin suggestion, send

**Share exposure**:
The record that a profile first learned of a Location through a particular share (`LocationExposure`). It is used to chain onward shares back to the originating share (`resolve_origin_share` / `record_share_exposure`), and it never references the recipient's pins.
_Avoid_: share provenance (use it for the mechanism, not the row), infection

**Pin in common**:
A place two profiles have both pinned. The code counts it as the same Place where both pins resolve onto one, and as the same Location otherwise. It feeds the "Places in Common" stat and the `COMMON_PIN` visibility option. Having a pin in common is a fact, never consent to see the other's pin data.
_Avoid_: shared pin, common location

**Visibility setting**:
One of a profile's per-surface audience choices (`VisibilityChoice`: anyone, anything in common, common pin, common friend, common trip, friends, no one). It is applied on top of the container gate, never instead of it.
_Avoid_: privacy level, permission

**Container**:
The thing an item is deliberately shared into (wiki, trip, direct message, safety check-in, ...), which decides who can reach it. A pin is never a container.
_Avoid_: scope, channel

**Public pin**:
A place that passed the strict community vote (`PublicPinCandidate` / `PublicPinVote`, ADR-0003) and is offered to every account as a suggested pin, so new users have something on their map. Passing is permanent unless an admin reverts it.
_Avoid_: public location, featured pin

### People

**Profile**:
The app's account entity that owns pins, settings, keys and every other domain relationship. It is one-to-one with a Django `User`.
_Avoid_: user (in domain code), account

**User**:
The Django auth record behind a Profile: credentials, email and login factors only.
_Avoid_: profile

### Organising and browsing

**Label**:
A named marker a profile applies to things, with a `kind`: tag, category, status, people (on other profiles) or media. A global label has no profile; categories and statuses are always per-profile.
_Avoid_: tag (that is one kind)

**Pin list**:
A profile's named, ordered collection of its own pins (`PinList`).
_Avoid_: collection, folder

**Smart list**:
A pin list whose membership follows saved-filter criteria (`is_smart`). Manual adds and removals persist against the filter, and a pin is never listed twice.
_Avoid_: dynamic list, filter list

**Saved filter**:
A profile's named main-map filter combination (`SavedFilter`), usable on the map and as smart-list criteria.
_Avoid_: saved search, view

**Memories**:
The browse surface that collects a profile's timeline, maps, sharing, journal, visits and pending pin suggestions.
_Avoid_: history, journal (one tab)

**Vault**:
A profile's personal media library of photos, documents and albums not tied to a pin or wiki (`/vault/`). It is not end-to-end encrypted today.
_Avoid_: E2EE vault, library

**Visit**:
One recorded occasion a profile went to one of its pinned places (`PinVisit`), from the journal, imports, trips, photos, geolocation or a check-in.
_Avoid_: check-in, trip

**Pin suggestion**:
A place a batch photo scan (Immich or a local folder) found evidence of visiting, proposed to the scanning profile as a new pin or a visit (`PinSuggestion`).
_Avoid_: pin share, visit suggestion (a proposed Visit sent to a profile to confirm)

**Markup**:
User-drawn map annotations (lines, shapes, text) attached to a pin (private), a wiki (community) or a standalone markup map (`PinMarkup`, `MarkupMap`) that check-ins, comments and visits can attach.
_Avoid_: drawing, boundary

### Trips and safety

**Trip**:
A plan shared among member profiles, made of trip activities. Only members can see it.
_Avoid_: outing, expedition

**Trip activity**:
One planned item on a trip (`TripActivity`), usually at a Location, with its own schedule, status and RSVPs. Some prose calls an activity with a place a "stop". A shared trip's display data must come from the wiki or a consented copy, never a live pin.
_Avoid_: stop, event

**Safety check-in**:
A profile's declared outing with a check-in deadline and emergency contacts who are alerted if the owner does not check in (`SafetyCheckin`).
_Avoid_: trip (a check-in is not a Trip), check-in (alone, which collides with a Visit)

**Emergency contact**:
A person attached to one check-in (`SafetyCheckinContact`), who may have no account and reaches the check-in through a token link. A contact sees the plan only once the check-in is overdue.
_Avoid_: safety contact, partner

**Check-in partner**:
A trusted account the owner explicitly grants full, early visibility into a check-in, including any shared live location (`SafetyCheckinPartner`).
_Avoid_: emergency contact

**Overdue check-in**:
A check-in whose deadline and grace period passed without a check-in (status `OVERDUE`). GOALS.md calls this an "incident".
_Avoid_: incident (the code uses "incident" for crime-report history)

### Games and community knowledge

**SpotGuessr**:
A GeoGuessr-style game over places the player can already see, in Photos, Named Place and Street View modes, solo or multiplayer.
_Avoid_: guessing game

**Trivia**:
A quiz game of questions about places the player has pinned, drawn from deterministic templates, AI generation and user submissions, all screened by a content classifier.
_Avoid_: quiz

**Consensus**:
The wiki-completion game, and the only game that writes to shared data: a round asks for a missing or unconfirmed wiki field or photo coordinate at a place the player has a visited pin for. Its points and levels belong to Consensus alone.
_Avoid_: wiki game

**Fact**:
The resolved current value, confidence and status for one key about a Location, wiki or image. It is recomputed from append-only `FactEvidence` rows contributed by games, edits and providers.
_Avoid_: stat, attribute

**Consensus trust**:
A per-profile Beta-Bernoulli posterior of accuracy on Consensus trust-check rounds (`ConsensusProfile.trust_score`). It weights fact evidence and never gates access.
_Avoid_: reputation, trust rating (that is `ProfileTrust`, a private star rating of another user)

**Reputation**:
A hidden, append-only ledger of scored contributions per profile (`ReputationEvent`, summed in `ProfileReputation`). It is never shown to users, and nothing reads it to gate anything yet.
_Avoid_: points, karma, credit, trust

### Encryption

**Key bundle**:
A profile's E2EE identity: its public key plus copies of the private key wrapped client-side under a password, recovery key or passkey (`MessagingKeyBundle`, `E2EEPasskeyWrap`). Conversation and group keys are sealed to it, so the server can read no message.
_Avoid_: keypair (unqualified), encryption key

**Vault key**:
A planned per-profile key for data that must survive device loss, such as photos at rest and safety archives (docs/designs/e2ee-passkey-unlock.md, class 2). It is not built.
_Avoid_: E2EE vault, Vault (the media library)
