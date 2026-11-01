"""Publish copy generation.

Every generated copy carries the AI-generation label in its text. The
picture itself never burns in a watermark or text; the label lives in the
project info and the publish copy, not in the frames.
"""

AI_LABEL = "本片全部画面由 AI 生成，非实拍。"


def render_publish_copy(project: dict, shots: list) -> str:
    total_seconds = len(shots) * 10
    lines = [
        f"《{project['title']}》",
        project["idea"],
        f"全片 {total_seconds} 秒 · {len(shots)} 个镜头 · {project['genre']}",
        AI_LABEL,
        "分镜：",
    ]
    for shot in shots:
        start = (shot["seq"] - 1) * 10
        lines.append(
            f"{shot['seq']}. [{start // 60:02d}:{start % 60:02d}] "
            f"{shot['title']} — {shot['camera']}。"
        )
    return "\n".join(lines)
