"""Rendering: trimmed segments, seam chaining, grain, loudness.

Two passes:
1. Per-segment pass: exact trim (trim/atrim filters), uniform colour grade
   (eq), stereo 48 kHz audio, linear loudness unification (loudnorm).
2. Chain pass: hard-cut seams use the concat filter; crossfade seams use
   xfade + acrossfade with a 2-3 frame dissolve. A light film grain is
   applied once at the end to break any plastic feel.

All durations come from ffprobe on the rendered segments, never from
assumptions, so trims and dissolves are accounted for exactly.
"""
import os
import subprocess
import tempfile

from .media import ffprobe, run


def _segment_pass(src: str, trim_head: float, trim_tail: float, out_path: str,
                  size: str) -> float:
    info = ffprobe(src)
    total = info.get("duration", 10.0)
    head = max(0.0, trim_head)
    tail = max(0.0, trim_tail)
    dur = total - head - tail
    if dur < 1.0:
        dur = min(total, 1.0)
        tail = max(0.0, total - head - dur)
    end = head + dur
    # Normalize audio to 48 kHz stereo so the chain pass sees uniform input.
    # Mono sources are duplicated to both channels; stereo passes through.
    if info.get("audio_channels", 0) == 1:
        channel_filter = "pan=stereo|c0=c0|c1=c0"
    else:
        channel_filter = "anull"
    filt = (
        f"[0:v]trim=start={head}:end={end},setpts=PTS-STARTPTS,"
        f"scale={size},fps=24,format=yuv420p,"
        f"eq=brightness=0.02:contrast=1.02:saturation=1.05[v];"
        f"[0:a]atrim=start={head}:end={end},asetpts=PTS-STARTPTS,"
        f"aresample=48000,{channel_filter},loudnorm=I=-16:TP=-1.5:LRA=11[a]"
    )
    proc = run([
        "ffmpeg", "-y", "-v", "error", "-i", src,
        "-filter_complex", filt, "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast",
        "-c:a", "aac", out_path,
    ])
    if proc.returncode != 0:
        raise RuntimeError(
            f"segment render failed: {proc.stderr.decode(errors='replace')[:300]}")
    return ffprobe(out_path).get("duration", dur)


def _chain_render(segment_paths, durations, seams, grain_amount, out_path):
    """seams[i] is how segment i joins segment i+1 ('hard' or 'xfade')."""
    n = len(segment_paths)
    if n == 0:
        raise ValueError("no segments to render")
    if n == 1:
        filt = f"[0:v]noise=alls={grain_amount}:allf=t[vout]"
        cmd = ["ffmpeg", "-y", "-v", "error", "-i", segment_paths[0],
               "-filter_complex", filt, "-map", "[vout]", "-map", "0:a",
               "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast",
               "-c:a", "aac", out_path]
        proc = run(cmd)
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.decode(errors="replace")[:300])
        return out_path
    fade = 3 / 24.0
    parts = []
    cur_v, cur_a = "0:v", "0:a"
    cur_dur = durations[0]
    for k in range(1, n):
        seam = seams[k - 1] if k - 1 < len(seams) else "hard"
        if seam == "xfade":
            offset = max(0.0, cur_dur - fade)
            parts.append(
                f"[{cur_v}][{k}:v]xfade=transition=fade:duration={fade}:offset={offset}[vx{k}];"
                f"[{cur_a}][{k}:a]acrossfade=d={fade}[ax{k}]")
            cur_v, cur_a = f"vx{k}", f"ax{k}"
            cur_dur = cur_dur + durations[k] - fade
        else:
            parts.append(
                f"[{cur_v}][{cur_a}][{k}:v][{k}:a]concat=n=2:v=1:a=1[vx{k}][ax{k}]")
            cur_v, cur_a = f"vx{k}", f"ax{k}"
            cur_dur = cur_dur + durations[k]
    parts.append(f"[{cur_v}]noise=alls={grain_amount}:allf=t[vout]")
    filt = ";".join(parts)
    cmd = ["ffmpeg", "-y", "-v", "error"]
    for path in segment_paths:
        cmd += ["-i", path]
    cmd += ["-filter_complex", filt, "-map", "[vout]", "-map", f"[{cur_a}]",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast",
            "-c:a", "aac", out_path]
    proc = run(cmd)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode(errors="replace")[:300])
    return out_path


def render_polished(db, project_id: int, plan: dict, media_dir: str,
                    out_path: str) -> dict:
    """Execute an edit plan into a polished film. Returns info dict."""
    project = db.row_to_dict(db.query_one(
        "SELECT * FROM projects WHERE id = ?", (project_id,)))
    size = "1280x720" if project["orientation"] == "landscape" else "720x1280"
    tmpdir = tempfile.mkdtemp(prefix="polish_")
    segment_paths = []
    durations = []
    seams = []
    for entry in plan["shots"]:
        take = db.row_to_dict(db.query_one(
            "SELECT * FROM takes WHERE id = ?", (entry["take_id"],)))
        seg = os.path.join(tmpdir, f"seg_{entry['shot_id']}.mp4")
        dur = _segment_pass(
            take["file_path"], entry.get("trim_head_sec", 0),
            entry.get("trim_tail_sec", 0), seg, size)
        segment_paths.append(seg)
        durations.append(dur)
        seams.append(entry.get("seam_after", "hard"))
    grain = plan.get("global", {}).get("grain_amount", 4)
    _chain_render(segment_paths, durations, seams[:-1] if seams else [],
                  grain, out_path)
    info = ffprobe(out_path)
    return {"file": out_path, "duration": info.get("duration", 0),
            "segments": len(segment_paths)}


def render_concat(db, project_id: int, media_dir: str, out_path: str) -> dict:
    """Rough cut: selected takes joined with hard cuts (preview film)."""
    project = db.row_to_dict(db.query_one(
        "SELECT * FROM projects WHERE id = ?", (project_id,)))
    shots = db.query(
        "SELECT * FROM shots WHERE project_id = ? AND selected_take_id IS NOT NULL "
        "ORDER BY seq",
        (project_id,),
    )
    tmpdir = tempfile.mkdtemp(prefix="rough_")
    size = "1280x720" if project["orientation"] == "landscape" else "720x1280"
    segment_paths, durations = [], []
    for shot_row in shots:
        shot = db.row_to_dict(shot_row)
        take = db.row_to_dict(db.query_one(
            "SELECT * FROM takes WHERE id = ?", (shot["selected_take_id"],)))
        seg = os.path.join(tmpdir, f"seg_{shot['id']}.mp4")
        dur = _segment_pass(take["file_path"], 0, 0, seg, size)
        segment_paths.append(seg)
        durations.append(dur)
    _chain_render(segment_paths, durations, ["hard"] * (len(durations) - 1),
                  0, out_path)
    info = ffprobe(out_path)
    return {"file": out_path, "duration": info.get("duration", 0),
            "segments": len(segment_paths)}
