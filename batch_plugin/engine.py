"""Round engine: capped batches, auto-continuation, requeue rules.

Rules implemented here (see README "派发约束"):
- A round counts only successful videos, at most 6 per round. Failures do
  not count toward the cap.
- When a round's last report lands, the next round for queued takes is
  dispatched automatically, round after round, until the project assembles
  or the queue is empty.
- A failed dispatch returns the take to the queue (up to `max_attempts`
  tries; afterwards it is marked failed and no longer retried).
- Takes left "generating" with no activity for `stale_seconds` (default
  30 minutes) are returned to the queue by the watchdog; a later round
  takes them over. Server start also recovers any such leftovers.
- Once every shot has a selected take the project assembles and surplus
  queued candidates are NOT generated.
- In auto mode the first completed take of a shot becomes its selected
  take; later, higher-scoring takes do not replace it automatically.
"""
import os
import threading
import time

from .db import now
from .media import spec_check
from .publish import render_publish_copy

MAX_SUCCESSES_PER_ROUND = 6
MAX_ATTEMPTS = 3
DEFAULT_STALE_SECONDS = 30 * 60

_WORKERS = {}
_WORKERS_LOCK = threading.Lock()


# ---------------------------------------------------------------- helpers

def get_project(db, project_id):
    row = db.query_one("SELECT * FROM projects WHERE id = ?", (project_id,))
    return db.row_to_dict(row)


def get_shots(db, project_id):
    rows = db.query(
        "SELECT * FROM shots WHERE project_id = ? ORDER BY seq", (project_id,))
    return [db.row_to_dict(r) for r in rows]


def get_takes_for_shot(db, shot_id):
    rows = db.query(
        "SELECT * FROM takes WHERE shot_id = ? ORDER BY take_no", (shot_id,))
    return [db.row_to_dict(r) for r in rows]


def is_assembled(db, project_id) -> bool:
    shots = get_shots(db, project_id)
    return bool(shots) and all(s.get("selected_take_id") for s in shots)


def assemble_project(db, project_id):
    """Mark assembled and generate the publish copy (with AI label)."""
    project = get_project(db, project_id)
    shots = get_shots(db, project_id)
    copy_text = render_publish_copy(project, shots)
    db.execute(
        "UPDATE projects SET status = 'assembled', publish_copy = ? WHERE id = ?",
        (copy_text, project_id),
    )


def queue_takes(db, project_id):
    """Create takes_per_shot queued takes for every shot of the project."""
    project = get_project(db, project_id)
    ts = now()
    for shot in get_shots(db, project_id):
        for take_no in range(1, project["takes_per_shot"] + 1):
            db.execute(
                "INSERT OR IGNORE INTO takes "
                "(shot_id, take_no, status, attempts, updated_at, created_at) "
                "VALUES (?, ?, 'queued', 0, ?, ?)",
                (shot["id"], take_no, ts, ts),
            )


# ---------------------------------------------------------------- rounds

def _next_queued_take(db, project_id, exclude_ids):
    rows = db.query(
        "SELECT takes.* FROM takes JOIN shots ON shots.id = takes.shot_id "
        "WHERE shots.project_id = ? AND takes.status = 'queued' "
        "AND takes.attempts < ? ORDER BY shots.seq, takes.take_no",
        (project_id, MAX_ATTEMPTS),
    )
    for row in rows:
        take = db.row_to_dict(row)
        if take["id"] not in exclude_ids:
            return take
    return None


def _take_file_path(media_dir, project_id, shot_seq, take_no):
    return os.path.join(
        media_dir, f"project_{project_id}",
        f"shot{shot_seq}_take{take_no}.mp4")


def _run_one_round(db, project_id, backend, media_dir):
    """Run a single round; returns (successes, round_id)."""
    project = get_project(db, project_id)
    cur = db.execute(
        "INSERT INTO rounds (project_id, kind, status, message, successes, "
        "last_activity, created_at) VALUES (?, 'generate', 'running', ?, 0, ?, ?)",
        (project_id, "本轮进行中", now(), now()),
    )
    round_id = cur.lastrowid
    successes = 0
    attempted = set()
    while successes < MAX_SUCCESSES_PER_ROUND:
        if is_assembled(db, project_id):
            break
        take = _next_queued_take(db, project_id, attempted)
        if take is None:
            break
        attempted.add(take["id"])
        shot = db.row_to_dict(db.query_one(
            "SELECT * FROM shots WHERE id = ?", (take["shot_id"],)))
        project = get_project(db, project_id)
        db.execute(
            "UPDATE takes SET status = 'generating', attempts = attempts + 1, "
            "updated_at = ? WHERE id = ?",
            (now(), take["id"]),
        )
        out_path = _take_file_path(
            media_dir, project_id, shot["seq"], take["take_no"])
        result = backend.generate(
            prompt=shot["prompt_full"], out_path=out_path,
            orientation=project["orientation"], duration=10.0,
            meta={"shot_seq": shot["seq"], "take_no": take["take_no"],
                  "shot_id": shot["id"], "take_id": take["id"]},
        )
        if result.get("ok"):
            spec = spec_check(out_path)
            note = spec["note"] + "；接缝待终审复核。"
            db.execute(
                "UPDATE takes SET status = 'done', file_path = ?, "
                "check_note = ?, updated_at = ? WHERE id = ?",
                (out_path, note, now(), take["id"]),
            )
            successes += 1
            # Auto mode: first completed take of a shot stays selected.
            if project["auto_mode"] and not shot.get("selected_take_id"):
                db.execute(
                    "UPDATE shots SET selected_take_id = ? WHERE id = ?",
                    (take["id"], shot["id"]),
                )
            if is_assembled(db, project_id):
                assemble_project(db, project_id)
        else:
            attempts = take["attempts"] + 1
            new_status = "queued" if attempts < MAX_ATTEMPTS else "failed"
            db.execute(
                "UPDATE takes SET status = ?, updated_at = ? WHERE id = ?",
                (new_status, now(), take["id"]),
            )
        db.execute(
            "UPDATE rounds SET successes = ?, last_activity = ? WHERE id = ?",
            (successes, now(), round_id),
        )
    db.execute(
        "UPDATE rounds SET status = 'completed', last_activity = ? WHERE id = ?",
        (now(), round_id),
    )
    return successes, round_id


def _worker_loop(db, project_id, backend, media_dir):
    """Dispatch rounds until assembled or the queue is exhausted."""
    while True:
        if is_assembled(db, project_id):
            return
        queued = db.query_one(
            "SELECT COUNT(*) AS c FROM takes JOIN shots ON shots.id = takes.shot_id "
            "WHERE shots.project_id = ? AND takes.status = 'queued' "
            "AND takes.attempts < ?",
            (project_id, MAX_ATTEMPTS),
        )["c"]
        if queued == 0:
            return
        successes, _ = _run_one_round(db, project_id, backend, media_dir)
        # Last report of the round landed: auto-dispatch the next round.
        # No early exit on zero successes: a failed take consumes an
        # attempt each round, so attempts are bounded by MAX_ATTEMPTS and
        # the loop always terminates (assembled, queue empty, or all failed).
        time.sleep(0.2)


def launch(db, project_id, backend, media_dir):
    """Queue takes (if none exist) and start/keep the worker running."""
    existing = db.query_one(
        "SELECT COUNT(*) AS c FROM takes JOIN shots ON shots.id = takes.shot_id "
        "WHERE shots.project_id = ?",
        (project_id,),
    )["c"]
    if existing == 0:
        queue_takes(db, project_id)
    db.execute(
        "UPDATE projects SET status = 'generating' WHERE id = ? "
        "AND status NOT IN ('assembled')",
        (project_id,),
    )
    with _WORKERS_LOCK:
        thread = _WORKERS.get(project_id)
        if thread is not None and thread.is_alive():
            return {"ok": True, "already_running": True}
        thread = threading.Thread(
            target=_worker_loop, args=(db, project_id, backend, media_dir),
            daemon=True,
        )
        thread.start()
        _WORKERS[project_id] = thread
    return {"ok": True, "already_running": False}


def requeue_stale(db, stale_seconds: int = DEFAULT_STALE_SECONDS) -> dict:
    """Return stuck in-flight takes to the queue; mark stale rounds."""
    cutoff = now() - stale_seconds
    stuck = db.query(
        "SELECT id FROM takes WHERE status = 'generating' AND updated_at < ?",
        (cutoff,),
    )
    for row in stuck:
        db.execute(
            "UPDATE takes SET status = 'queued', updated_at = ? WHERE id = ?",
            (now(), row["id"]),
        )
    stale_rounds = db.query(
        "SELECT id FROM rounds WHERE status = 'running' AND last_activity < ?",
        (cutoff,),
    )
    for row in stale_rounds:
        db.execute(
            "UPDATE rounds SET status = 'stale' WHERE id = ?", (row["id"],))
    return {"requeued_takes": len(stuck), "stale_rounds": len(stale_rounds)}


def recover_on_startup(db):
    """Crash recovery: anything left generating is unreported, requeue it."""
    return requeue_stale(db, stale_seconds=0)
