# Scope: Move HLS encoding to a Celery worker (async, Option B)

**Status:** proposal / not yet built. Review before implementing.

## Goal

Today HLS packaging runs **synchronously inside the upload request** — the
"Post" button blocks ~10–30s while three renditions encode, and a long clip
could brush the 180s request cap. This scopes moving only the HLS step onto a
background Celery worker so uploads return instantly. The clip plays its MP4 the
moment the post is created and silently upgrades to adaptive HLS once the worker
finishes.

## Why this is small (and why now)

The original SY-1 plan in `api/tasks.py` imagined moving the *whole* transcode
(MP4 + thumbnail + dimensions) to a worker, which needed a "processing" post
state, a feed readiness gate, and a frontend contract. **We don't need any of
that**, because the synchronous pipeline already produces a playable MP4 +
thumbnail before the post commits. Only the *slow, optional* HLS ladder moves
async. The post is always immediately playable, so:

- No new "processing" status on `Post`.
- No change to `post_visibility_q` / home-feed rules.
- **No frontend changes** — the app already prefers `hls` and falls back to the
  MP4 (`item.hls ?? item.video`); `hls` is simply null until the worker backfills
  it, then populated on the next feed fetch / refresh.

The infrastructure is already in place: `backend/celery.py` (`Celery("here")`),
`CELERY_TASK_ALWAYS_EAGER = not bool(CELERY_BROKER_URL)`, and
`deploy/systemd/celery-worker.service`. So nothing changes until a broker is live.

## Design

1. **Add `process_post_media(post_media_id)` task** in `api/tasks.py`.
   - Re-fetch the `PostMedia` by id (tasks take serialisable args — an int id,
     never a model instance).
   - Read the already-stored MP4 bytes from `pm.file` (it's in S3/storage by the
     time the task runs — no need to stage the raw upload anywhere).
   - `build_hls_ladder(bytes)` → `store_hls_bundle(...)` → set `pm.hls_master` →
     `pm.save(update_fields=["hls_master"])`.
   - Idempotent: if `pm.hls_master` is already set, return early (handles task
     redelivery under `acks_late`). Best-effort: on failure, log and leave
     `hls_master` null — the MP4 keeps playing, exactly like today's fallback.

2. **Extract the storage helper.** Move `_store_hls_bundle` out of
   `create.py` into `api/services/media/hls.py` as `store_hls_bundle(bundle)` so
   both the (now-removed) inline path and the task import it from one place.

3. **Change the upload flow** in `api/views/posts/create.py`:
   - Remove the inline `build_hls_ladder` call from `_process_media_files` and
     the `_store_hls_bundle` block from the create loop.
   - After the `transaction.atomic()` block, for each video `PostMedia`, enqueue:
     `transaction.on_commit(lambda mid=pm.id: process_post_media.delay(mid))`.
   - `on_commit` guarantees the row is committed before the task runs and that a
     rolled-back post never enqueues. One task per video media (parallelisable).

4. **No model/migration/serializer/response changes.** `hls_master` already
   exists; the `hls` field is already emitted everywhere and is null-safe.

## Behaviour in each environment

- **Dev / CI / tests (no broker):** `CELERY_TASK_ALWAYS_EAGER` is true, so
  `.delay()` runs inline — same end state as today. One caveat: `on_commit`
  callbacks **don't fire inside `TestCase`'s rolled-back transaction**, so HLS
  won't be generated in standard `TestCase` uploads. That's fine (upload tests
  don't assert HLS); a dedicated task test (below) covers the logic, and any
  test that needs the on_commit path uses `TransactionTestCase`.
- **Production (broker live):** post returns immediately (HLS no longer in the
  request); the worker encodes and backfills `hls_master` seconds later.

## Files touched

| File | Change |
|---|---|
| `api/tasks.py` | + `process_post_media(post_media_id)` task |
| `api/services/media/hls.py` | + `store_hls_bundle(...)` (moved from create.py) |
| `api/views/posts/create.py` | remove inline HLS build/store; enqueue task via `on_commit` |
| `api/tests/test_hls_task.py` | + new test (call task directly, assert `hls_master` set) |

No frontend changes. No migration.

## Operational steps (your environment — the real cost)

1. Set `REDIS_URL` (or a dedicated `CELERY_BROKER_URL`, e.g. `redis://host:6379/2`).
2. Enable the existing worker unit: `systemctl enable --now celery-worker`
   (`deploy/systemd/celery-worker.service`).
3. Ensure the **worker host has the `ffmpeg` binary** and the **same S3
   credentials** as the web host (it reads the MP4 from and writes segments to
   storage).
4. Monitor the worker (queue depth, failures). This ongoing operational
   responsibility is the main cost of Option B — the code change itself is modest.

## Risks / edge cases

- **`HLS_TIMEOUT` vs worker limits.** `hls.py` uses `HLS_TIMEOUT = 300`, equal to
  the Celery **soft** limit (`CELERY_TASK_SOFT_TIME_LIMIT = 300`). For short reels
  (~10–30s) this never bites, but consider lowering `HLS_TIMEOUT` (e.g. 180) or
  raising the worker soft limit so ffmpeg's own timeout fires first with a clean
  error rather than a `SoftTimeLimitExceeded`.
- **Retry orphans.** `store_hls_bundle` writes under a fresh uuid prefix each run.
  A retry after a partial failure could leave one orphaned partial set. Mitigate
  with the `hls_master`-already-set early return + optional `max_retries`.
- **Brief no-HLS window.** Between post creation and task completion the clip
  plays MP4. Acceptable and invisible (same bytes the user would have seen).
- **Multiple accounts / multi-media posts.** One task per video media keeps each
  job small and lets them run in parallel.

## Verification plan (when built)

1. Existing upload tests stay green in eager mode (no behaviour change).
2. New `test_hls_task.py`: create a `PostMedia` with a small real clip, call
   `process_post_media(pm.id)` directly, assert `pm.hls_master` is populated and
   the master playlist + segments landed in storage. (ffmpeg-backed, like the
   existing standalone HLS validation.)
3. Manual end-to-end: set `REDIS_URL` locally, run a worker, upload a clip,
   confirm the post returns instantly and `hls_master` backfills within seconds.

## Rollback

Set the broker env var back to empty → instantly reverts to eager (inline)
behaviour. Or revert the `create.py` enqueue change to restore fully-synchronous
HLS. The MP4 fallback means there is no user-facing breakage at any point.
