"""Media helpers: ffprobe spec checks and frame-level measurements.

Only ffmpeg/ffprobe and the standard library are used. Frame brightness is
measured by decoding a frame to a small grayscale raw image and averaging
it in pure Python, so no image library is required.
"""
import json
import os
import subprocess
import tempfile


def run(cmd, **kwargs):
    return subprocess.run(cmd, capture_output=True, text=False, **kwargs)


def ffprobe(path: str) -> dict:
    """Return duration/resolution/audio info for a media file."""
    out = run([
        "ffprobe", "-v", "quiet", "-print_format", "json",
        "-show_format", "-show_streams", path,
    ])
    if out.returncode != 0:
        return {"ok": False, "error": out.stderr.decode(errors="replace")[:300]}
    data = json.loads(out.stdout.decode() or "{}")
    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    audio = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
    duration = float(data.get("format", {}).get("duration", 0) or 0)
    return {
        "ok": True,
        "duration": duration,
        "width": int(video["width"]) if video else 0,
        "height": int(video["height"]) if video else 0,
        "has_audio": audio is not None,
        "audio_channels": int(audio.get("channels", 0)) if audio else 0,
        "audio_rate": int(audio.get("sample_rate", 0)) if audio else 0,
        "video_codec": video["codec_name"] if video else "",
        "audio_codec": audio["codec_name"] if audio else "",
    }


def spec_check(path: str, expect_duration: float = 10.0,
               expect_size=None) -> dict:
    """Spec check only. A spec pass says nothing about realism."""
    info = ffprobe(path)
    if not info.get("ok"):
        return {"spec_pass": False, "note": f"ffprobe 失败：{info.get('error', '')}"}
    problems = []
    if abs(info["duration"] - expect_duration) > 0.1:
        problems.append(f"时长 {info['duration']:.3f}s ≠ {expect_duration:.3f}s")
    if expect_size and (info["width"], info["height"]) != expect_size:
        problems.append(f"分辨率 {info['width']}x{info['height']} ≠ {expect_size[0]}x{expect_size[1]}")
    if not info["has_audio"]:
        problems.append("无音频轨")
    note = "规格通过" if not problems else "规格问题：" + "；".join(problems)
    return {
        "spec_pass": not problems,
        "note": note,
        "duration": info["duration"],
        "width": info["width"],
        "height": info["height"],
        "has_audio": info["has_audio"],
    }


def extract_frame(video_path: str, at_seconds: float, out_path: str) -> bool:
    out = run([
        "ffmpeg", "-y", "-v", "error", "-ss", str(at_seconds),
        "-i", video_path, "-frames:v", "1", "-q:v", "2", out_path,
    ])
    return out.returncode == 0 and os.path.exists(out_path)


def frame_gray_small(image_path: str, size: int = 64) -> bytes:
    """Decode an image to a size x size grayscale raw buffer."""
    out = run([
        "ffmpeg", "-v", "error", "-i", image_path,
        "-vf", f"scale={size}:{size}",
        "-f", "rawvideo", "-pix_fmt", "gray", "-",
    ])
    if out.returncode != 0:
        return b""
    return out.stdout


def mean_brightness(gray: bytes) -> float:
    if not gray:
        return 0.0
    return sum(gray) / len(gray)


def frame_diff(a: bytes, b: bytes) -> float:
    """Mean absolute per-pixel difference between two gray buffers."""
    if not a or not b or len(a) != len(b):
        return 255.0
    return sum(abs(x - y) for x, y in zip(a, b)) / len(a)


def extract_three_frames(video_path: str, tmpdir: str):
    """Extract head / middle / tail frames. Returns {name: gray buffer}."""
    frames = {}
    for name, t in (("head", 0.5), ("mid", 5.0), ("tail", 9.5)):
        jpg = os.path.join(tmpdir, f"{name}.jpg")
        if extract_frame(video_path, t, jpg):
            frames[name] = frame_gray_small(jpg)
    return frames
