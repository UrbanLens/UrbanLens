# dashboard/models/safety/ — Goals (from docs/GOALS.md)

## Safety check-ins

- Contacts do **not** get live location by default — only the trip plan, and only if the user
  fails to check in on time (an "incident"). A contact never needs to be a site member; access
  is via token link. The check-in owner can specify that a contact does get live location and
  an ability to see the plan before a failed check-in, but this must be explicitly chosen and
  consent-focused.
