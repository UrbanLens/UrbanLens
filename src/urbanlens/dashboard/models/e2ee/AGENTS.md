# dashboard/models/e2ee/ — Goals (from docs/GOALS.md)

## Encryption

- Direct messages: end-to-end encrypted by default, as close to unconditionally enforced as
  possible — there should be no way to turn it off. Optional self-destruct/disappearing
  messages; once self-destructed (read or timed out), the message — encrypted blob included —
  is gone from the server, not just hidden.
- Backups: strongly encrypted at rest, retained only as long as needed. Self-destructing
  messages are excluded from backups entirely, even encrypted.
- Logs — ours and any related service that stores IPs/access data — rotate and purge on a
  regular interval, not indefinitely.
- Trip photo archives (especially for past/completed trips) should be encrypted, ideally
  end-to-end.
- Longer-term direction, roughly in order of feasibility:
  1. Private photos and private notes: fully end-to-end encryptable today with no real
     tradeoff — photo bytes are never searched (only EXIF/filename/AI tags), and notes are
     never shareable to begin with.
  2. Private pins: main blocker is site search needing plaintext/queryable fields. A baseline
     worth building even if not final: decrypt-on-login into memory, re-encrypt and discard the
     key on logout/timeout. Aggregate cross-pin stats and "pins in common" features need to stop
     comparing pins/location models directly regardless — compare at the Place or Wiki level.
  3. Markup maps / shared photos: private and encrypted by default; sharing one either
     duplicates it into an open copy, or is treated as the user electing to decrypt that one
     item — re-encrypt it if every share of it is later revoked.
- Add tests that actively try to defeat the encryption (property tests, brute-force-timing
  simulation, etc.), not just tests that assert the happy path round-trips.
