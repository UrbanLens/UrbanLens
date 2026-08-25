# dashboard/models/pin_share/ — Goals (from docs/GOALS.md)

## Privacy model — pin sharing

- **Pin → pin**: sender shares coordinates (+ optional bundled fields) as a *suggestion*;
  recipient must accept; on accept it becomes the recipient's own independent pin. The
  recipient never gets access to the sender's actual pin, before or after acceptance.
- The only way pin data reaches anyone else is an explicit, opt-in, consent-tracked copy —
  never a live reference. Every REST endpoint and every query must enforce this by
  construction.
