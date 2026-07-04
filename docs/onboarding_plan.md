# UrbanLens onboarding plan

UrbanLens onboarding should appear as contextual, dismissible guidance while people use the site normally. The implemented pattern is a set of coach cards that highlight the relevant control or section, offer one direct action, support a session-level "Later" choice, and persist "Don't show again" in browser storage.

## Implemented map nudges

1. **Import pins for smaller collections**: when the map loads and the user has fewer than 100 pins, invite them to import Google Takeout pins and pulse the import toolbar button.
2. **Add a first pin**: for empty maps, explain the Add Pin button and right-click map shortcut.
3. **Search and filter**: once the user has a small collection, point out filters for ratings, visits, badges, and names.
4. **Map layers**: introduce satellite, topographic, and weather overlays as situational tools for exploration planning.

## Implemented pin-detail nudges

1. **Personal detail map**: introduce private detail pins, shapes, arrows, labels, hazards, and access-route annotation.
2. **Photos**: point users toward the gallery and GPS-aware photo map layer.
3. **Aliases**: explain private alternate names and how they differ from shared wiki naming.
4. **Community wiki**: direct users to the shared wiki when knowledge should benefit everyone.

## Implemented community wiki nudges

1. **Shared edits**: explain that suggested edits immediately improve the shared location page.
2. **Community detail pins**: distinguish shared wiki detail pins from personal pin-detail annotations.
3. **Shared photos**: encourage useful location documentation and GPS placement.
4. **Discussion and history**: point out collaborative comments and transparent edit history.

## Implemented Organize nudges

1. **Priority order**: explain that higher labels win map icon/color precedence when a pin has multiple labels.
2. **Priority visual**: show a stacked-label-to-map-marker diagram so users can see how the top item becomes the marker.
3. **View modes and bulk cleanup**: introduce list/gallery/tree views and multi-select tools for organizing labels efficiently.

## Implemented trip-detail nudges

1. **Route overview**: connect numbered map markers to activity order and the completed-stop toggle.
2. **Activities**: explain proposed/confirmed stops, notes, votes, secret locations, and child trips.
3. **Members and RSVPs**: guide users to collaboration and permissions.
4. **Personal layout, weather, and comments**: explain draggable sections and planning context.

## Future candidates

- Trip-planning hints after the user has multiple visited or wish-list pins near each other.
- Privacy and sharing reminders when users first interact with friends, comments, or public profile settings.
- Advanced map annotation tips after a user creates their first line or shape.
