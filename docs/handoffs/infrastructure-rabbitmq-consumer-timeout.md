# RabbitMQ's 30-minute consumer timeout makes a Celery worker exit while a long countdown waits

- **Status: OPEN as of 2026-10-04.** Written for UrbanLens P290 (`docs/archive/PROBLEMS-ARCHIVE.md`).
- **Direction: outbound**, from `UrbanLens/UrbanLens` to `UrbanLens/infrastructure`.

## What happens

A Celery task with a countdown is delivered at once and held unacknowledged by the worker until it has run
(`CELERY_TASK_ACKS_LATE = True`). RabbitMQ's default `consumer_timeout` is 30 minutes. When a delivery stays
unacknowledged past it, RabbitMQ closes the channel and the worker exits:

```
CRITICAL/MainProcess] Unrecoverable error: PreconditionFailed(406, 'PRECONDITION_FAILED - delivery acknowledgement on channel 1 timed out. Timeout value used: 1800000 ms. ...
```

Every task that worker was running is lost and redelivered. The redelivered countdown task is held again, so the
worker exits again 31 minutes later. On UrbanLens's dev stack (`development_main`) the interactive worker did this
every 31 minutes for 11 hours on 2026-09-30, and twice more on 2026-10-04. The interactive queue also carries signup
mail and safety alerts.

The countdowns that run past 30 minutes are real. A boundary provider's deferral is retried after up to two hours,
deferred pin resolution after up to six, and import and export cleanup after one.

## What the app repo changed

- `services/core/celery.py::LONGEST_COUNTDOWN_SECONDS` (6 h) bounds every countdown; `safely_enqueue_task` shortens a
  longer one.
- `docker-compose.yml`'s `rabbitmq` service sets
  `RABBITMQ_SERVER_ADDITIONAL_ERL_ARGS: "-rabbit consumer_timeout 28800000"` (8 h: the longest countdown plus the
  one-hour task time limit, with room). Verified on dev: `rabbitmqctl eval 'application:get_env(rabbit, consumer_timeout).'`
  answers `{ok,28800000}`, and every worker reconnected without a restart.
- The Redis fallback's `visibility_timeout` is 8 h for the same reason.

## What this repo needs

`platform/rabbitmq/base/deployment.yaml` runs the same `rabbitmq:3.13-management-alpine` image with no config, so
site-a and site-b keep the 30-minute default. Add the same variable to the container's `env`:

```yaml
            - name: RABBITMQ_SERVER_ADDITIONAL_ERL_ARGS
              value: "-rabbit consumer_timeout 28800000"
```

It takes effect when the pod restarts. Check it with the `rabbitmqctl eval` line above. If a site raises
`UL_CELERY_TASK_TIME_LIMIT` above an hour, the timeout must grow with it: it must exceed six hours plus that limit.

If damballa's compose stacks pin their own copy of the compose file rather than this repo's, they need the same line.
