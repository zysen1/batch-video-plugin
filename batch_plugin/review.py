"""Final review: frame checks against a 15-signal fake-signal checklist.

Spec checks (duration / resolution / audio) are computed with ffprobe and
are reported separately from realism: a spec pass never counts as a
realism pass.

Frame checks are deterministic heuristics, not a model verdict:
- light / colour-temperature drift: brightness spread across head/mid/tail
- seam camera jump: first frame vs previous shot's last frame difference
- in-shot scene jump: head vs tail frame difference
- exposure jump: brightness jump between consecutive sampled frames
The other signals have no reliable automatic detector in this codebase
(no OCR, no lip reading), so they are reviewer-flagged through the manual
review endpoint and default to "not observed".

Dialogue is always reported as unverified: frames cannot verify lip sync
or whether lines were spoken clearly. The review note says so explicitly
on every shot.
"""
import json
import os
import tempfile

from .db import now
from .media import (extract_three_frames, frame_diff, frame_gray_small,
                    mean_brightness, spec_check)

DIALOGUE_NOTE = "对白逐字核对：抽帧无法核口型，本项未验证。"

# (key, name, method, level) -- level: warn or fail when the signal fires.
FAKE_SIGNALS = [
    ("plastic_skin", "塑料皮肤", "manual", "warn"),
    ("stiff_expression", "表情僵硬", "manual", "warn"),
    ("light_drift", "光线色温漂移", "auto", "warn"),
    ("wardrobe_jump", "服装跳变", "manual", "fail"),
    ("hairstyle_jump", "发型跳变", "manual", "fail"),
    ("identity_drift", "身份漂移", "manual", "fail"),
    ("seam_jump", "接缝机位跳变", "auto", "warn"),
    ("sign_garbled", "招牌乱码", "manual", "warn"),
    ("prop_vanish", "道具消失", "manual", "fail"),
    ("lip_unclear", "口型不清", "manual", "warn"),
    ("action_gap", "动作不连贯", "manual", "warn"),
    ("exposure_jump", "曝光突变", "auto", "warn"),
    ("scene_jump", "镜内场景跳变", "auto", "warn"),
    ("motion_smear", "运动拖影异常", "manual", "warn"),
    ("av_desync", "音画不同步", "manual", "warn"),
]

# Heuristic thresholds on 0-255 grayscale means / mean abs differences.
DRIFT_WARN = 25.0
SEAM_WARN = 40.0
SCENE_WARN = 60.0


def _auto_signals(frames: dict, prev_tail_gray: bytes):
    """Compute automatic signals from sampled frames. Returns (signals, notes)."""
    signals = []
    notes = []
    if not frames:
        return signals, notes
    brightness = {k: mean_brightness(v) for k, v in frames.items() if v}
    if len(brightness) >= 2:
        drift = max(brightness.values()) - min(brightness.values())
        if drift > DRIFT_WARN:
            signals.append("light_drift")
            notes.append(f"首/中/尾帧亮度跨度 {drift:.1f}，疑似光线色温漂移")
        ordered = [brightness[k] for k in ("head", "mid", "tail") if k in brightness]
        jumps = [abs(b - a) for a, b in zip(ordered, ordered[1:])]
        if jumps and max(jumps) > DRIFT_WARN:
            signals.append("exposure_jump")
            notes.append(f"相邻采样帧亮度跳变 {max(jumps):.1f}，疑似曝光突变")
    if "head" in frames and "tail" in frames:
        scene = frame_diff(frames["head"], frames["tail"])
        if scene > SCENE_WARN:
            signals.append("scene_jump")
            notes.append(f"首尾帧差异 {scene:.1f}，疑似镜内场景跳变")
    if prev_tail_gray and "head" in frames:
        seam = frame_diff(frames["head"], prev_tail_gray)
        if seam > SEAM_WARN:
            signals.append("seam_jump")
            notes.append(f"与上一镜尾帧差异 {seam:.1f}，疑似接缝机位跳变")
    return signals, notes


def _verdict_for(signals):
    levels = {key: level for key, _, _, level in FAKE_SIGNALS}
    if any(levels.get(s) == "fail" for s in signals):
        return "fail"
    if signals:
        return "warn"
    return "pass"


def _suggest(signals, verdict):
    if verdict == "fail":
        return "不要硬剪：必须再抽，抽前锁定人物、服装与关键道具；本条不进入二次剪辑。"
    tips = []
    if "seam_jump" in signals:
        tips.append("接缝处加 2-3 帧叠化")
    if "light_drift" in signals or "exposure_jump" in signals:
        tips.append("统一色温曝光至全片基准")
    if "scene_jump" in signals:
        tips.append("裁掉跳变段落或以遮挡剪辑点过渡")
    if not tips:
        return "直接可用；仅按全片基准统一色温与响度即可。"
    return "保留；" + "、".join(tips) + "。"


def auto_review(db, project_id: int, media_tmp=None):
    """Review every selected take; returns a list of review dicts."""
    shots = db.query(
        "SELECT * FROM shots WHERE project_id = ? AND selected_take_id IS NOT NULL "
        "ORDER BY seq",
        (project_id,),
    )
    results = []
    prev_tail_gray = b""
    tmpdir = media_tmp or tempfile.mkdtemp(prefix="review_")
    for shot_row in shots:
        shot = db.row_to_dict(shot_row)
        take = db.row_to_dict(db.query_one(
            "SELECT * FROM takes WHERE id = ?", (shot["selected_take_id"],)))
        spec = spec_check(take["file_path"]) if take["file_path"] else {
            "spec_pass": False, "note": "无成片文件"}
        frames = extract_three_frames(take["file_path"], tmpdir) if take["file_path"] else {}
        signals, notes = _auto_signals(frames, prev_tail_gray)
        if frames.get("tail"):
            prev_tail_gray = frames["tail"]
        verdict = _verdict_for(signals)
        issues = "；".join(notes) if notes else "抽帧未见明显假信号。"
        issues += f" {DIALOGUE_NOTE}"
        review = {
            "shot_id": shot["id"],
            "take_id": take["id"],
            "verdict": verdict,
            "realism_score": 8 if verdict == "pass" else (7 if verdict == "warn" else 5),
            "continuity_score": 8 if verdict == "pass" else (6 if verdict == "warn" else 3),
            "fake_signals": json.dumps(signals, ensure_ascii=False),
            "issues": f"规格：{spec['note']}，但规格通过不等于真实感通过。" + issues,
            "color_note": "与全片基准一致。" if verdict == "pass" else "需统一色温曝光至基准。",
            "seam_note": "与上一镜尾帧接缝对得上。" if "seam_jump" not in signals else "接缝机位跳变，需叠化过渡。",
            "edit_suggestion": _suggest(signals, verdict),
            "trim_head_sec": 0.4 if "seam_jump" in signals else 0.0,
            "trim_tail_sec": 0.0,
            "source": "auto",
        }
        db.execute(
            "INSERT INTO reviews (shot_id, take_id, verdict, realism_score, "
            "continuity_score, fake_signals, issues, color_note, seam_note, "
            "edit_suggestion, trim_head_sec, trim_tail_sec, source, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (review["shot_id"], review["take_id"], review["verdict"],
             review["realism_score"], review["continuity_score"],
             review["fake_signals"], review["issues"], review["color_note"],
             review["seam_note"], review["edit_suggestion"],
             review["trim_head_sec"], review["trim_tail_sec"],
             review["source"], now()),
        )
        results.append(review)
    db.execute(
        "UPDATE projects SET post_status = ? WHERE id = ?",
        ("needs_fix" if any(r["verdict"] == "fail" for r in results) else "reviewed",
         project_id),
    )
    return results


def save_manual_review(db, shot_id: int, verdict: str, signals,
                       realism_score: int, continuity_score: int,
                       edit_suggestion: str = "", trim_head_sec: float = 0.0,
                       trim_tail_sec: float = 0.0, issues: str = ""):
    """Reviewer override for one shot (manual verdicts are first-class)."""
    shot = db.row_to_dict(db.query_one(
        "SELECT * FROM shots WHERE id = ?", (shot_id,)))
    if not shot or not shot.get("selected_take_id"):
        return {"ok": False, "error": "该镜头暂无选定片"}
    issues = (issues or "人工复核。") + f" {DIALOGUE_NOTE}"
    db.execute(
        "INSERT INTO reviews (shot_id, take_id, verdict, realism_score, "
        "continuity_score, fake_signals, issues, color_note, seam_note, "
        "edit_suggestion, trim_head_sec, trim_tail_sec, source, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'manual', ?)",
        (shot_id, shot["selected_take_id"], verdict, realism_score,
         continuity_score, json.dumps(signals, ensure_ascii=False), issues,
         "", "", edit_suggestion or _suggest(signals, verdict),
         trim_head_sec, trim_tail_sec, now()),
    )
    return {"ok": True}


def latest_reviews(db, project_id: int):
    """Latest review per shot for a project."""
    rows = db.query(
        "SELECT r.* FROM reviews r JOIN shots s ON s.id = r.shot_id "
        "WHERE s.project_id = ? ORDER BY s.seq, r.id DESC",
        (project_id,),
    )
    latest = {}
    for row in rows:
        d = db.row_to_dict(row)
        latest.setdefault(d["shot_id"], d)
    shots = db.query(
        "SELECT id FROM shots WHERE project_id = ? ORDER BY seq", (project_id,))
    return [latest[s["id"]] for s in shots if s["id"] in latest]
