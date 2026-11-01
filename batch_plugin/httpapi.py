"""HTTP API layer (standard library only).

All routes are JSON except file import (multipart) and media playback.
The frontend in web/ is a thin client over these routes.
"""
import json
import os
import re

from . import assemble as assemble_mod
from . import editplan as editplan_mod
from . import engine
from . import review as review_mod
from .adapters import MockAdapter
from .db import now
from .media import spec_check
from .planner import build_shots_with_prompts
from .prompts import assemble_prompt


def _json_body(handler):
    length = int(handler.headers.get("Content-Length") or 0)
    if length == 0:
        return {}
    raw = handler.rfile.read(length)
    try:
        return json.loads(raw.decode())
    except Exception:
        return {}


def _send(handler, status, payload):
    data = json.dumps(payload, ensure_ascii=False).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def _project_detail(db, project_id):
    project = engine.get_project(db, project_id)
    if not project:
        return None
    shots = []
    for shot in engine.get_shots(db, project_id):
        shot = dict(shot)
        shot["takes"] = engine.get_takes_for_shot(db, shot["id"])
        shots.append(shot)
    rounds = [db.row_to_dict(r) for r in db.query(
        "SELECT * FROM rounds WHERE project_id = ? ORDER BY id DESC LIMIT 20",
        (project_id,))]
    reviews = review_mod.latest_reviews(db, project_id)
    plan = editplan_mod.latest_plan(db, project_id)
    finals = [db.row_to_dict(r) for r in db.query(
        "SELECT * FROM final_videos WHERE project_id = ? ORDER BY id DESC",
        (project_id,))]
    return {"project": project, "shots": shots, "rounds": rounds,
            "reviews": reviews, "plan": plan, "finalVideos": finals}


def _parse_multipart(handler):
    """Minimal single-file multipart parser (no cgi dependency)."""
    ctype = handler.headers.get("Content-Type", "")
    m = re.search(r"boundary=([^;]+)", ctype)
    if not m:
        return None
    boundary = m.group(1).strip().strip('"').encode()
    length = int(handler.headers.get("Content-Length") or 0)
    body = handler.rfile.read(length)
    parts = body.split(b"--" + boundary)
    for part in parts:
        if b"Content-Disposition" not in part:
            continue
        head, _, content = part.partition(b"\r\n\r\n")
        if b'name="file"' not in head:
            continue
        content = content.rsplit(b"\r\n", 1)[0]
        return content
    return None


class Api:
    def __init__(self, db, media_dir, backend, allow_test_hooks=False):
        self.db = db
        self.media_dir = media_dir
        self.backend = backend
        self.allow_test_hooks = allow_test_hooks

    # ------------------------------------------------------------ routes
    def route(self, handler, method, path):
        db = self.db
        seg = [s for s in path.split("/") if s]
        try:
            if method == "GET" and path == "/api/health":
                return 200, {"ok": True}
            if method == "GET" and seg == ["api", "projects"]:
                rows = db.query(
                    "SELECT id, title, idea, genre, orientation, status, "
                    "post_status, created_at FROM projects ORDER BY id DESC")
                return 200, {"projects": [db.row_to_dict(r) for r in rows]}
            if method == "POST" and seg == ["api", "projects"]:
                body = _json_body(handler)
                title = (body.get("title") or body.get("idea") or "未命名")[:80]
                idea = body.get("idea", "")[:500]
                if not idea:
                    return 400, {"ok": False, "error": "idea 不能为空"}
                takes = max(1, min(6, int(body.get("takesPerShot", 3))))
                cur = db.execute(
                    "INSERT INTO projects (title, idea, genre, orientation, "
                    "takes_per_shot, auto_mode, status, character_lock, "
                    "location_lock, visual_lock, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, 'planning', ?, ?, ?, ?)",
                    (title, idea, body.get("genre", "记录"),
                     body.get("orientation", "landscape"), takes,
                     1 if body.get("autoMode", True) else 0,
                     body.get("characterLock", ""), body.get("locationLock", ""),
                     body.get("visualLock", ""), now()),
                )
                return 200, {"ok": True, "projectId": cur.lastrowid}
            if len(seg) >= 3 and seg[0] == "api" and seg[1] == "projects":
                project_id = int(seg[2])
                return self._project_routes(handler, method, seg[2:], project_id)
            return 404, {"ok": False, "error": "unknown route"}
        except ValueError:
            return 400, {"ok": False, "error": "bad id"}
        except Exception as exc:  # noqa: BLE001 - surface as JSON error
            return 500, {"ok": False, "error": str(exc)[:500]}

    def _project_routes(self, handler, method, seg, project_id):
        # seg here is the full path segments after "api/projects", i.e.
        # [project_id, action, ...].
        db = self.db
        if engine.get_project(db, project_id) is None:
            return 404, {"ok": False, "error": "project not found"}
        action = seg[1] if len(seg) > 1 else None

        if method == "GET" and action is None:
            return 200, _project_detail(db, project_id)

        if method == "POST" and action == "plan":
            body = _json_body(handler)
            has_takes = db.query_one(
                "SELECT COUNT(*) AS c FROM takes JOIN shots ON shots.id = takes.shot_id "
                "WHERE shots.project_id = ?", (project_id,))["c"]
            if has_takes:
                return 400, {"ok": False, "error": "已有候选，无法重新规划"}
            project = engine.get_project(db, project_id)
            shot_count = max(1, min(12, int(body.get("shotCount", 4))))
            locks = {"character_lock": project["character_lock"],
                     "location_lock": project["location_lock"],
                     "visual_lock": project["visual_lock"]}
            shots = build_shots_with_prompts(
                project["idea"], project["genre"], shot_count,
                body.get("beats"), locks)
            db.execute("DELETE FROM shots WHERE project_id = ?", (project_id,))
            for shot in shots:
                shot = dict(shot)
                shot["orientation"] = project["orientation"]
                db.execute(
                    "INSERT INTO shots (project_id, seq, title, camera, visual, "
                    "dialogue, start_state, end_state, purpose, prompt_full) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (project_id, shot["seq"], shot["title"], shot["camera"],
                     shot["visual"], shot["dialogue"], shot["start_state"],
                     shot["end_state"], shot["purpose"], shot["prompt_full"]),
                )
            db.execute(
                "UPDATE projects SET status = 'planned' WHERE id = ?",
                (project_id,))
            return 200, {"ok": True, "shots": shots}

        if method == "POST" and action == "locks":
            body = _json_body(handler)
            db.execute(
                "UPDATE projects SET character_lock = ?, location_lock = ?, "
                "visual_lock = ? WHERE id = ?",
                (body.get("characterLock", ""), body.get("locationLock", ""),
                 body.get("visualLock", ""), project_id),
            )
            project = engine.get_project(db, project_id)
            prev_end = ""
            for shot in engine.get_shots(db, project_id):
                shot = dict(shot)
                shot["orientation"] = project["orientation"]
                prompt = assemble_prompt(
                    shot, project["character_lock"], project["location_lock"],
                    project["visual_lock"], prev_end, shot["seq"] == 1)
                db.execute("UPDATE shots SET prompt_full = ? WHERE id = ?",
                           (prompt, shot["id"]))
                prev_end = shot["end_state"]
            return 200, {"ok": True}

        if method == "POST" and action == "launch":
            result = engine.launch(db, project_id, self.backend, self.media_dir)
            return 200, result

        if method == "POST" and action == "takes" and len(seg) == 4 \
                and seg[3] == "select":
            take_id = int(seg[2])
            take = db.row_to_dict(db.query_one(
                "SELECT * FROM takes WHERE id = ?", (take_id,)))
            if not take:
                return 404, {"ok": False, "error": "take not found"}
            shot = db.row_to_dict(db.query_one(
                "SELECT * FROM shots WHERE id = ?", (take["shot_id"],)))
            if shot["project_id"] != project_id:
                return 400, {"ok": False, "error": "take 不属于该项目"}
            db.execute("UPDATE shots SET selected_take_id = ? WHERE id = ?",
                       (take_id, shot["id"]))
            if engine.is_assembled(db, project_id):
                engine.assemble_project(db, project_id)
            return 200, {"ok": True}

        if method == "POST" and action == "review":
            results = review_mod.auto_review(db, project_id)
            return 200, {"ok": True, "reviews": results}

        if method == "POST" and action == "review-manual":
            body = _json_body(handler)
            result = review_mod.save_manual_review(
                db, int(body.get("shotId", 0)), body.get("verdict", "warn"),
                body.get("signals", []), int(body.get("realismScore", 7)),
                int(body.get("continuityScore", 6)),
                body.get("editSuggestion", ""),
                float(body.get("trimHeadSec", 0)),
                float(body.get("trimTailSec", 0)), body.get("issues", ""))
            if not result.get("ok"):
                return 400, result
            reviews = review_mod.latest_reviews(db, project_id)
            status = "needs_fix" if any(
                r["verdict"] == "fail" for r in reviews) else "reviewed"
            db.execute("UPDATE projects SET post_status = ? WHERE id = ?",
                       (status, project_id))
            return 200, result

        if method == "POST" and action == "edit-plan":
            if not review_mod.latest_reviews(db, project_id):
                return 400, {"ok": False, "error": "请先终审"}
            plan = editplan_mod.generate_edit_plan(db, project_id)
            return 200, {"ok": True, "plan": plan}

        if method == "PUT" and action == "edit-plan":
            body = _json_body(handler)
            plan = editplan_mod.update_plan(db, project_id, body.get("plan", {}))
            return 200, {"ok": True, "plan": plan}

        if method == "POST" and action == "polish":
            plan = editplan_mod.latest_plan(db, project_id)
            if not plan or not plan.get("shots"):
                return 400, {"ok": False, "error": "精剪方案为空（可能全部镜头待再抽）"}
            out_path = os.path.join(
                self.media_dir, f"project_{project_id}",
                f"polished_{plan['id']}.mp4")
            info = assemble_mod.render_polished(
                db, project_id, plan, self.media_dir, out_path)
            cur = db.execute(
                "INSERT INTO final_videos (project_id, plan_id, file_path, "
                "created_at) VALUES (?, ?, ?, ?)",
                (project_id, plan["id"], out_path, now()),
            )
            return 200, {"ok": True, "finalVideoId": cur.lastrowid, **info}

        if method == "POST" and action == "roughcut":
            out_path = os.path.join(
                self.media_dir, f"project_{project_id}", "roughcut.mp4")
            info = assemble_mod.render_concat(
                db, project_id, self.media_dir, out_path)
            return 200, {"ok": True, **info}

        if method == "POST" and action == "approve":
            body = _json_body(handler)
            db.execute(
                "UPDATE final_videos SET approved = 1 WHERE id = ? "
                "AND project_id = ?",
                (int(body.get("finalVideoId", 0)), project_id))
            return 200, {"ok": True}

        if method == "POST" and action == "reroll-failed":
            reviews = review_mod.latest_reviews(db, project_id)
            fail_shot_ids = [r["shot_id"] for r in reviews
                             if r["verdict"] == "fail"]
            if not fail_shot_ids:
                return 400, {"ok": False, "error": "没有不通过的镜头"}
            project = engine.get_project(db, project_id)
            for shot_id in fail_shot_ids:
                db.execute(
                    "UPDATE shots SET selected_take_id = NULL WHERE id = ?",
                    (shot_id,))
                max_no = db.query_one(
                    "SELECT MAX(take_no) AS m FROM takes WHERE shot_id = ?",
                    (shot_id,))["m"] or 0
                ts = now()
                for i in range(1, project["takes_per_shot"] + 1):
                    db.execute(
                        "INSERT INTO takes (shot_id, take_no, status, attempts, "
                        "updated_at, created_at) VALUES (?, ?, 'queued', 0, ?, ?)",
                        (shot_id, max_no + i, ts, ts))
            db.execute(
                "UPDATE projects SET status = 'generating', post_status = 'none' "
                "WHERE id = ?",
                (project_id,))
            result = engine.launch(db, project_id, self.backend, self.media_dir)
            return 200, {"ok": True, "rerolledShots": len(fail_shot_ids), **result}

        if method == "POST" and action == "import" and len(seg) == 3:
            shot_id = int(seg[2])
            shot = db.row_to_dict(db.query_one(
                "SELECT * FROM shots WHERE id = ? AND project_id = ?",
                (shot_id, project_id)))
            if not shot:
                return 404, {"ok": False, "error": "shot not found"}
            data = _parse_multipart(handler)
            if data is None:
                return 400, {"ok": False, "error": "未收到文件（字段名须为 file）"}
            max_no = db.query_one(
                "SELECT MAX(take_no) AS m FROM takes WHERE shot_id = ?",
                (shot_id,))["m"] or 0
            take_no = max_no + 1
            rel = f"project_{project_id}/shot{shot['seq']}_take{take_no}_import.mp4"
            out_path = os.path.join(self.media_dir, rel)
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            with open(out_path, "wb") as fh:
                fh.write(data)
            spec = spec_check(out_path)
            ts = now()
            cur = db.execute(
                "INSERT INTO takes (shot_id, take_no, status, file_path, "
                "check_note, updated_at, created_at) VALUES (?, ?, 'done', ?, ?, ?, ?)",
                (shot_id, take_no, out_path, spec["note"] + "；磁盘导入。", ts, ts))
            take_id = cur.lastrowid
            project = engine.get_project(db, project_id)
            if project["auto_mode"] and not shot.get("selected_take_id"):
                db.execute(
                    "UPDATE shots SET selected_take_id = ? WHERE id = ?",
                    (take_id, shot_id))
            if engine.is_assembled(db, project_id):
                engine.assemble_project(db, project_id)
            return 200, {"ok": True, "takeId": take_id,
                         "file": "/media/" + rel}

        if method == "POST" and action == "test-fail-once":
            if not self.allow_test_hooks or not isinstance(self.backend, MockAdapter):
                return 403, {"ok": False, "error": "test hooks 未开启"}
            body = _json_body(handler)
            MockAdapter.fail_once_keys.add(
                (int(body.get("shotSeq", 0)), int(body.get("takeNo", 0))))
            return 200, {"ok": True}

        return 404, {"ok": False, "error": "unknown route"}
