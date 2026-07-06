# Contributing to UrbanLens

Thanks for your interest in UrbanLens. This document covers the project's license, what that means for contributors, and how to submit changes.

## License

UrbanLens is licensed under the **GNU Affero General Public License v3.0 (AGPL-3.0)**. The full text is in [`LICENSE`](./LICENSE).

### Why AGPL instead of MIT/Apache/GPL?

UrbanLens is a self-hosted web application, not a library or CLI tool. Permissive licenses (MIT, Apache 2.0) and even standard copyleft licenses (GPL) have a gap for network services: someone can take the code, modify it, and run it as a hosted service without ever distributing the modified source — because they never "distribute" the software in the traditional sense, only its output over a network.

AGPL closes that gap. Section 13 of the AGPL requires that if you run a modified version of this software and let users interact with it over a network, you must offer them the corresponding source code. The practical effect: anyone who forks UrbanLens and stands up a competing hosted instance has to share their changes back, the same as if they'd distributed a binary.

### What this means for you as a contributor

- Any code you contribute will be distributed under AGPL-3.0. By submitting a pull request, you agree to license your contribution under the same terms.
- You retain copyright on your own contributions — AGPL doesn't require a copyright assignment or CLA.
- If you run your own modified fork of UrbanLens (including privately hosting it for a group of users), you're expected to make your modified source available to those users.
- Purely personal, non-networked use of a modified copy (e.g. running it locally for yourself) does not trigger the network-source disclosure requirement — that only kicks in when others interact with your modified version over a network.

## Third-party dependencies

UrbanLens bundles or depends on third-party code under a mix of licenses (MIT, BSD, Apache 2.0, LGPL, and a few AGPL/GPL packages). All are compatible with AGPL-3.0 distribution. If you add a new dependency:

1. Check its license before adding it — GPL-incompatible licenses (e.g. certain "source available" or field-of-use-restricted licenses) are not acceptable.
2. Run a license audit against the project's actual dependency set (not your whole system environment) before a release:
   ```bash
   uv pip list --format=freeze
   uv run pip-licenses --format=markdown --with-urls
   ```
3. If a dependency is GPL-2.0-only (not "or later"), flag it for review — GPLv2-only code cannot be combined with AGPL-3.0 code in one work.

## Data and location information

UrbanLens's codebase is open under AGPL, but location data, coordinates, and site-specific details for exploration sites are **not** covered by this license and should not be assumed public. Do not submit pull requests that add real site coordinates, addresses, or access details to the public repository. Location data lives in the private database layer, separate from the open-source application code.

## How to contribute

1. Fork the repo and create a branch off `main`.
2. Reference the relevant Jira issue (e.g. `UL-123`) in your branch name or commit message where applicable.
3. Keep PRs scoped — one logical change per PR.
4. Make sure `basedpyright` and existing tests pass before submitting.
5. Open a pull request with a clear description of what changed and why.

## Questions

Open a GitHub issue or reference the relevant Jira ticket if you're unsure whether something is in scope.
