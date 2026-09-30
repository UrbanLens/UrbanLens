# X31 — What one chat message costs, and where a flood would hurt

> **Written by a Claude agent. Not authoritative.** Measured on `development_main`
> on 2026-09-30 with `DEBUG=True` on a host at load ~6 of 8 cores, so every figure
> is high. Re-run before relying on one.

`id: X31` · `status: holds` · `updated: 2026-09-30`

Jess's question: at what message rate does chat become a denial-of-service risk?
The per-sender limit (`UL_MESSAGES_PER_MINUTE`, 20) would otherwise be better off
removed. Notifications are not a reason to limit: they already collapse to one per
unread conversation (`_notify_recipient`), and people can turn them off.

## Per message, in the process that handles the send

The probe called the service functions directly, 150 sends each, and ignored the first 20.

| Send | Wall p50 | CPU p50 | Queries |
|---|---|---|---|
| Direct message | 58 ms | 32 ms | 23 |
| Group of 2 | 95 ms | 46 ms | 26 |
| Group of 20 (the `max_group_chat_members` default) | 116 ms | 63 ms | — |

The live push is then one `broadcast_channel_group_messages` task on the
`interactive` queue, which is the same four-slot pool that safety escalations
use. The task body takes 5 ms of CPU for a 2-person group and 14 ms for a
20-person group.

## Where that becomes a DoS

- **daphne.** `app-ws` runs as one process, so every signed-in socket on the site
  shares about one core. At 30–60 ms per message, one sender saturates it at
  **about 15–30 messages/s**, and every other user's socket stalls. That rate is
  the real threshold. At 4/s, one sender takes roughly 12–25% of it.
- **HTTP fallback (gunicorn, 4 cores).** The whole tier saturates at about
  60–120 messages/s. X28 found no headroom at 1,000 users.
- **`interactive` worker.** It would take hundreds of messages per second before
  the broadcasts crowded out safety tasks.
- **Recipients' browsers.** A few messages per second is fine.

A per-sender limit only protects against one account. N accounts get N times the
budget, so the defence for daphne is capacity (more daphne processes), not this
limit.

## Two further findings

- The limit uses a fixed 60 s window. A person typing single letters or emoji
  spends 20 messages in seconds, then waits up to a minute. A token bucket with a
  burst allowance fits chat better.
- `UL_WEBSOCKET_FRAMES_PER_MINUTE` (120, i.e. 2 frames/s including typing
  indicators and pings) caps the socket path below any message limit above
  ~100/min. Raising one without the other does nothing on the socket.

## What changed (2026-09-30)

Every `FrameBudget` and `ConnectionRate` is now a token bucket (GCRA, one Lua
call on Dragonfly). It holds `burst` charges and refills at `limit` per
`window_seconds`. The defaults are:

- messages: 240/min with a burst of 30 (`UL_MESSAGE_BURST`);
- socket frames: 360/min with a burst of 60 (`UL_WEBSOCKET_FRAME_BURST`), so the
  frame budget carries the message budget plus typing and keep-alives.

A burst is capped at one window's allowance. Adding daphne processes, which
would raise the ~15-30/s ceiling itself, is deferred.

Probe: `msg_cost_probe.py` in the session scratchpad. It creates throwaway
`rl_probe_tmp_*` users, sets the limit to 0 in-process, patches the Celery
enqueue to capture the broadcast, then times the task body separately.
