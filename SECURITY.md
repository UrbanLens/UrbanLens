# Security Policy

## Supported versions

Security fixes are provided for the latest released version of UrbanLens. Keep deployments on the most recent GitHub release and container image tag.

## Reporting a vulnerability

Please do not open a public issue for sensitive security reports. Instead, use GitHub's private vulnerability reporting feature for this repository, or contact the maintainers directly if private reporting is unavailable.

Include:

- A concise description of the issue and impact.
- Steps to reproduce or proof-of-concept details.
- Affected version, commit SHA, deployment mode, and relevant logs.
- Any suggested mitigation or patch, if known.

Maintainers should acknowledge reports within 5 business days, triage severity, and coordinate disclosure after a fix is available.

## Automated security coverage

The repository runs CodeQL, dependency review, Dependabot updates, and container provenance generation for release images. These checks supplement, but do not replace, manual review and runtime hardening.
