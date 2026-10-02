# Ask: two production commands Jess wants run now, and restore tooling is yours

- **Status: SENT, 2026-10-02.**
- **Direction: outbound.** This repo to whoever owns `UrbanLens/infrastructure`.
- `id: N33` · `status: current`

Jess decided these on 2026-10-02 (about 17:20-17:40Z). This repo has no access to production, so the runs are yours.
Both commands ship in the v0.8.0 image (`git cat-file -e v0.8.0:<path>`), so nothing waits for v0.9.0. Both use Django's
storage API, so they work against Garage.

## 1. `manage.py localize_article_images` on staging, then production (P165)

Jess: "Run it now, without waiting for 0.9.0 ... We're still pre-users."

```bash
python manage.py localize_article_images --dry-run   # prints the count
python manage.py localize_article_images
```

It downloads each remote image an article names once, keeps it, and points the article at the copy. Each changed article
gets one new revision, "Images stored on this site", so the old text stays in history. On `development_main` it changed
66 articles. Until it runs, images in articles saved before 2026-09-30 show broken under the image CSP. Run it from a pod
that can reach the image providers. Celery's egress policy allows the internet; web has no egress policy.

## 2. `manage.py sweep_unnamed_pin_images --delete` on staging, then production (P14)

Jess chose "report and delete in one go".

```bash
python manage.py sweep_unnamed_pin_images            # "Found N unnamed files under pin_images/ (X MiB)."
python manage.py sweep_unnamed_pin_images --delete
```

It removes files under `pin_images/` that no `Image` row names: leftovers from deletes before 2026-09-14. The media gate
already refuses them, so this only frees space. It skips anything younger than the task time limit and anything an undo
record still names. Please send back the two counts and sizes, so P14 can be closed with figures.

## 3. Postgres restore tooling is yours (P19)

Jess: "I'll take care of this." The restore script, its runbook and a test restore belong to the infrastructure repo.
This repo has closed the item and does nothing further.

## Your 0.8.0 findings, items 4-11

Each is filed here as its own problem, and gets a reply when it is fixed: P201 (Garage quorum failure on upload), P202
(the backup task on Kubernetes), P203 (the logged API key), P204 (media-copy's 503 at ERROR), P205 (Overpass's day-long
block) and P206 (`dashboard_location_cache`'s size). Item 10's two tiers are yours to build; whether the assistant has
to work in production now is a question for Jess. Item 11, an unreachable REData cached as empty for seven days, is
P187, which now carries your figures.

The `UL_HUGGINGFACE_AI_*` keys in both sites' `urbanlens-app-env` are no longer read: the stub that declared them is
gone in v0.9.0. You can drop them whenever it suits.
