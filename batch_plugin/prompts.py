"""Prompt assembly: style bible locks + realism master + relay rule.

The three lock sections are inserted verbatim into every shot's prompt, so a
lock edited later changes every shot consistently (see updatelocks in the
API layer, which rebuilds all prompts from the locks).
"""

REALISM_MASTER = (
    "真实感要求：单条连续长镜头，约 10 秒，不切镜；电影感摄影，柔和均匀的光线，"
    "整个镜头锁定同一个光源与色温，亮度与阴影方向不漂移；皮肤保留自然纹理，"
    "不过度磨皮，不出塑料感；人物表情放松、不摆拍，视线自然游移；带轻微手持晃动"
    "与真实运动模糊；使用真实环境声，不配乐。"
)

PICTURE_RULES = (
    "画面规则：画面内不烧录任何水印、台标、字幕或文字；背景招牌、屏幕、海报上"
    "不出现可读文字，避免乱码字；不慢动作、不突然变焦、不脸部畸变。"
)


def assemble_prompt(shot: dict, character_lock: str, location_lock: str,
                    visual_lock: str, prev_end_state: str = "",
                    is_first: bool = True) -> str:
    parts = [
        f"第 {shot['seq']} 镜《{shot['title']}》，10 秒，"
        + ("16:9 横屏" if shot.get("orientation", "landscape") == "landscape" else "9:16 竖屏")
        + "。",
    ]
    if character_lock:
        parts.append(f"人物锁定（全片统一，逐字沿用）：{character_lock}")
    if location_lock:
        parts.append(f"场景锁定（全片统一，逐字沿用）：{location_lock}")
    if visual_lock:
        parts.append(f"风格锁定（全片统一，逐字沿用）：{visual_lock}")
    parts.append(f"运镜：{shot['camera']}。")
    parts.append(f"画面：{shot['visual']}")
    if shot.get("dialogue"):
        parts.append(f"对白（说清楚，口型自然）：{shot['dialogue']}")
    if is_first:
        parts.append(f"起始画面：{shot['start_state']}")
    else:
        parts.append(
            "起始画面：从上一镜的尾帧直接接续——"
            f"{prev_end_state}。人物、服装、场景与光线与上一镜结尾完全一致，不重新开场。"
        )
    parts.append(f"结束画面（下一镜将从此尾帧接续）：{shot['end_state']}")
    parts.append(REALISM_MASTER)
    parts.append(PICTURE_RULES)
    parts.append("本片全部画面为 AI 生成。")
    return "\n".join(parts)
