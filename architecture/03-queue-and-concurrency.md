# 03 — Queue & Concurrency (the "max 2 workers" rule)

This is the heart of the architecture: a queue buffers jobs, and a dispatcher
guarantees **at most two Docker workers run at any instant**. When a worker
finishes, if the queue still has jobs, the next one starts immediately.

---

## The concurrency invariant

> **`0 ≤ running_workers ≤ 2` at all times, and a queued job starts the moment a
> slot frees.**

The dispatcher is the single gatekeeper. The API never launches workers; it only
enqueues. Workers never pull from the queue themselves (in dispatcher mode);
they are launched by the dispatcher and process exactly one job.

### Dispatcher loop (pseudocode)

```
MAX_CONCURRENCY = 2

loop forever:
    reap_exited_workers()                 # frees slots for containers that ended
    while running_count < MAX_CONCURRENCY:
        job = queue.receive(wait=20s)     # long-poll / blocking pop
        if job is None:
            break                         # queue empty -> idle cheaply
        launch_worker(job)                # docker run / ECS RunTask (detached)
        running_count += 1
    sleep(0.5s)
```

- `reap_exited_workers()` inspects the launched containers/tasks and decrements
  `running_count` for any that have exited (and, in queue systems with explicit
  ack, deletes/acks the message on success or lets it redeliver on failure).
- The `while` refills all free slots each tick, so after any worker exits the
  next queued job starts within one loop iteration.
- If the queue is empty, the dispatcher blocks on `queue.receive` and starts
  nothing until a new job arrives.

### `launch_worker(job)` maps a job to container env

```
docker run --rm --detach \
  -e VIDEO_URL="<job.video_url>" \
  -e CALLBACK_URL="<job.callback_url>" \
  -e VIDEO_ID="<job.video_id>" \
  -e JOB_ID="<job.job_id>" \
  -e SERPAPI_API_KEY=... \
  --memory=6g --cpus=4 \
  object-detection-process:latest
```

The container is unchanged CV code; it knows nothing about concurrency. The
dispatcher alone enforces the "max 2" rule.

---

## Queue backend options

Pick the backend that matches your deployment. The contract is the same: a
durable buffer with at-least-once delivery.

| Backend | Best for | Durability | Notes |
|---|---|---|---|
| **In-process queue** (Python `queue.Queue` inside the API) | Single box, simplest possible setup. | **Not durable** — jobs are lost if the API restarts. | Fine for dev / low-stakes. The dispatcher lives in the same process. |
| **Redis list / stream** | Single box or small cluster; survives API restarts. | Durable (with AOF/RDB). | `LPUSH`/`BRPOP` or Redis Streams with consumer groups + acks. Recommended default. |
| **AWS SQS** | Cloud / ECS; managed, scales, DLQ built-in. | Durable + managed. | Visibility timeout + DLQ give retries for free. Matches Architecture 1's queue. |

The rest of this document assumes **Redis or SQS** (durable). For SQS the
"receive → process → delete" pattern with a visibility timeout gives
at-least-once delivery and a dead-letter queue automatically.

---

## Backpressure

Two independent levers:

1. **Worker cap (always on): `MAX_CONCURRENCY = 2`.** This protects the machine.
   Extra jobs wait in the queue; they do not run.
2. **Queue depth cap (optional): `MAX_QUEUE_DEPTH`.** If set, the API returns
   `429` when the queue already holds this many waiting jobs. Use this to fail
   fast instead of accumulating an unbounded backlog. Leave it unset for an
   effectively unbounded queue (jobs just wait longer).

Throughput is bounded by `2 × (1 / per_video_seconds)`. On CPU a video takes
minutes (see [`05-system-requirements.md`](05-system-requirements.md)), so plan
queue depth around your arrival rate.

---

## Retries, failures & dead-letter

| Failure | What happens |
|---|---|
| **Worker crashes** before callback | Message is not acked/deleted → after the visibility timeout it is redelivered → dispatcher reprocesses it (up to `maxReceiveCount`). |
| **Worker finishes but callback POST fails** | Worker retries the POST with backoff. If it still fails, the worker exits non-zero and the job can be redelivered (so the caller eventually gets it). |
| **Poison message** (always fails) | After `maxReceiveCount` retries it goes to the **dead-letter queue (DLQ)** for inspection; it stops blocking the queue. |
| **video_url unreachable** | Worker sends a `status: "failed"` callback and exits; the message is acked (a bad URL will never succeed, so don't loop forever). |

### Idempotency

At-least-once delivery means a job can run more than once in rare crash
scenarios, so a callback can arrive more than once. The caller should dedupe on
`video_id` + `job_id`. The pipeline itself is deterministic given the same
input, so a re-run produces equivalent results.

---

## Why exactly 2, and how to change it

`MAX_CONCURRENCY = 2` is a single knob. It is set to 2 per the requirement,
sized so two workers fit comfortably in the host's CPU/RAM (or GPU) budget — see
[`05-system-requirements.md`](05-system-requirements.md). To change the cap you
change **one** environment variable on the dispatcher (`MAX_CONCURRENCY`) and
re-check the host has enough RAM/CPU/VRAM for the new count. Nothing else in the
system changes.

### Native ECS alternative

Instead of a custom dispatcher, you can express the same cap natively in ECS by
running the worker as an ECS **Service** with `desiredCount = 2`, each task in a
long-poll mode that pulls one SQS message at a time. ECS keeps exactly two tasks
alive and replaces any that exit. The custom dispatcher is the portable option
that also works with plain Docker on one box.
