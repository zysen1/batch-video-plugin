"""Shot planning.

The default planner is template-based and fully deterministic: it turns one
story idea into N ten-second shots following a generic opening / development
/ turn / closing beat arc. It does not call any model. A deployment that
wants model-based planning can implement the same `plan_shots` function
signature and swap it in; everything downstream only consumes shot dicts.

Beats may be supplied by the caller as a list of strings (used as the shot's
visual description) or dicts with visual/dialogue/camera/start/end keys.
"""
from .prompts import assemble_prompt

CAMERA_CYCLE = ["固定机位", "缓慢推轨", "跟拍", "环绕", "侧向跟拍", "低机位固定机位"]

BEAT_TITLES = ["开端", "发展", "转折", "收尾"]


def _beat_title(seq: int, total: int) -> str:
    if total <= len(BEAT_TITLES):
        return BEAT_TITLES[seq - 1]
    return f"第{seq}段"


def _default_beat(idea: str, seq: int, total: int) -> dict:
    title = _beat_title(seq, total)
    return {
        "visual": f"围绕「{idea}」展开的{title}画面",
        "dialogue": "（本镜对白待填写）",
        "camera": CAMERA_CYCLE[(seq - 1) % len(CAMERA_CYCLE)],
        "start": f"{title}开始时的画面状态（待细化）",
        "end": f"{title}结束时的画面状态（待细化，下一镜从此接续）",
        "purpose": f"{title}：推进故事，10 秒内只完成一个戏剧节拍",
    }


def normalize_beat(raw, idea: str, seq: int, total: int) -> dict:
    base = _default_beat(idea, seq, total)
    if isinstance(raw, str):
        base["visual"] = raw
    elif isinstance(raw, dict):
        for key in ("visual", "dialogue", "camera", "start", "end", "purpose"):
            if raw.get(key):
                base[key] = raw[key]
    return base


def plan_shots(idea: str, genre: str, shot_count: int, beats=None):
    """Return a list of shot dicts (without ids) for the given story."""
    shots = []
    prev_end = ""
    for seq in range(1, shot_count + 1):
        raw = beats[seq - 1] if beats and seq - 1 < len(beats) else None
        beat = normalize_beat(raw, idea, seq, shot_count)
        shot = {
            "seq": seq,
            "title": f"第{seq}镜（{(seq - 1) * 10}-{seq * 10}秒）：{_beat_title(seq, shot_count)}",
            "camera": beat["camera"],
            "visual": beat["visual"],
            "dialogue": beat["dialogue"],
            "start_state": prev_end if prev_end else beat["start"],
            "end_state": beat["end"],
            "purpose": beat["purpose"],
        }
        shots.append(shot)
        prev_end = beat["end"]
    return shots


def build_shots_with_prompts(idea: str, genre: str, shot_count: int,
                             beats, locks: dict):
    """Plan shots and assemble each shot's full prompt (locks verbatim)."""
    shots = plan_shots(idea, genre, shot_count, beats)
    prev_end = ""
    for shot in shots:
        shot["prompt_full"] = assemble_prompt(
            shot=shot,
            character_lock=locks.get("character_lock", ""),
            location_lock=locks.get("location_lock", ""),
            visual_lock=locks.get("visual_lock", ""),
            prev_end_state=prev_end,
            is_first=shot["seq"] == 1,
        )
        prev_end = shot["end_state"]
    return shots
