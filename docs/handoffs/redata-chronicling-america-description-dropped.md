# REData's Chronicling America search drops every page's text

- **Status: OPEN as of 2026-10-03.** Found by UrbanLens's P196 work, checking which fields each reference provider
  returns. The REData code cited is `main` as of 2026-10-03.
- **Direction: outbound**, from `UrbanLens/UrbanLens` to `../REData`.

## What happens

`ChroniclingAmericaGateway.search` (`src/redata/parcels/services/reference_documents/gateways.py`) builds each record's
`description` with `_strip_html(item.get("description"))`. LoC returns a newspaper page's OCR excerpt there as a **list**
of strings, and `_strip_html` returns `""` for anything that is not a `str`. So every Chronicling America record reaches
UrbanLens with an empty description. Its sibling `LibraryOfCongressGateway.search` already joins a list description.

## Why it matters now

UrbanLens shows a media item only when it names the place (P196). A Chronicling America record's title is the paper's
own dateline ("River Falls journal (River Falls, Pierce County, Wis.), July 30, 1908"). That never names the place, and
its town and county read as conflicting geography. With no description, the Historic Newspapers tab is empty for every
place.

## The ask

Join a list `description` before stripping tags, as `LibraryOfCongressGateway` does, and keep the joined excerpt whole,
or at least a few hundred characters, so the place's name survives into the record. A test with LoC's real response
shape (a list) would hold it.

UrbanLens-side follow-up, once this lands: P216 in `docs/PROBLEMS.md`. Even with the text, a dateline naming another
town shouldn't veto a page whose excerpt names the place. That rule is UrbanLens's to adjust.
