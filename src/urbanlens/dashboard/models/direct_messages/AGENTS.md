# dashboard/models/direct_messages/ — Goals (from docs/GOALS.md)

## Encryption

- Direct messages: end-to-end encrypted by default, as close to unconditionally enforced as
  possible — there should be no way to turn it off. Optional self-destruct/disappearing
  messages; once self-destructed (read or timed out), the message — encrypted blob included —
  is gone from the server, not just hidden.
