"""Secondary edit plan: generated from final review, editable by hand.

Rules:
- pass / warn shots enter the cut list with their trim points, seam
  treatment (hard cut, or a 2-3 frame crossfade where the review found a
  seam jump), and the global unification steps (colour/exposure, loudness,
  light grain).
- fail shots are NOT hard-edited. They go to the reroll list instead.
- The plan is stored as JSON and can be edited through the API before it
  is executed; execution reads the stored plan.
"""
import json

from .db import now
from .review import latest_reviews

FADE_FRAMES = 3
FPS = 24


def generate_edit_plan(db, project_id: int) -> dict:
    reviews = latest_reviews(db, project_id)
    shots = db.query(
        "SELECT * FROM shots WHERE project_id = ? AND selected_take_id IS NOT NULL "
        "ORDER BY seq",
        (project_id,),
    )
    entries = []
    reroll = []
    for shot_row in shots:
        shot = db.row_to_dict(shot_row)
        review = next((r for r in reviews if r["shot_id"] == shot["id"]), None)
        if review is None:
            continue
        if review["verdict"] == "fail":
            reroll.append(shot["id"])
            continue
        signals = json.loads(review["fake_signals"] or "[]")
        seam_after = "xfade" if "seam_jump" in signals else "hard"
        entries.append({
            "shot_id": shot["id"],
            "take_id": shot["selected_take_id"],
            "trim_head_sec": float(review["trim_head_sec"] or 0),
            "trim_tail_sec": float(review["trim_tail_sec"] or 0),
            "seam_after": seam_after,
            "unify_color": True,
            "unify_loudness": True,
        })
    plan = {
        "shots": entries,
        "reroll_shot_ids": reroll,
        "global": {
            "loudness_target_db": -16,
            "grain_amount": 4,
            "fade_frames": FADE_FRAMES,
            "fps": FPS,
        },
        "note": "fail 镜头不硬剪，已列入再抽清单；其余镜头按裁剪点与接缝处理执行。",
    }
    db.execute(
        "INSERT INTO edit_plans (project_id, plan_json, created_at) VALUES (?, ?, ?)",
        (project_id, json.dumps(plan, ensure_ascii=False), now()),
    )
    plan["id"] = db.query_one("SELECT MAX(id) AS id FROM edit_plans")[0]
    return plan


def latest_plan(db, project_id: int):
    row = db.query_one(
        "SELECT * FROM edit_plans WHERE project_id = ? ORDER BY id DESC",
        (project_id,),
    )
    if not row:
        return None
    plan = json.loads(row["plan_json"])
    plan["id"] = row["id"]
    return plan


def update_plan(db, project_id: int, plan: dict) -> dict:
    db.execute(
        "INSERT INTO edit_plans (project_id, plan_json, created_at) VALUES (?, ?, ?)",
        (project_id, json.dumps(plan, ensure_ascii=False), now()),
    )
    plan = dict(plan)
    plan["id"] = db.query_one("SELECT MAX(id) AS id FROM edit_plans")[0]
    return plan
