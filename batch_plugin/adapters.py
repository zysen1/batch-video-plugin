"""Pluggable video-generation backends.

A backend turns one shot prompt into one ten-second clip file. The engine
only depends on the `VideoBackend` interface, so a real vendor adapter can
be added without touching rounds, selection, review or editing.

Included here:
- `MockAdapter`: builds a placeholder clip with ffmpeg (flat colour picture
  + sine tone, 10.000 s, with audio). It exists so the whole pipeline runs
  end to end without any real video model. Mock clips are intentionally
  uniform in picture, so frame-based auto checks pass on them; they are
  placeholders, never presented as real footage.

To add a real vendor, subclass `VideoBackend` and implement `generate`:
the engine passes the shot's full prompt, the target path, orientation and
duration, and expects a dict with at least `ok: bool`. On failure return
`{"ok": False, "error": "..."}` -- the engine requeues the take (up to
`max_attempts`) instead of counting it toward the round's success cap.
"""
import os
import subprocess


class VideoBackend:
    name = "base"

    def generate(self, prompt: str, out_path: str, orientation: str = "landscape",
                 duration: float = 10.0, meta: dict = None) -> dict:
        raise NotImplementedError


class MockAdapter(VideoBackend):
    """Placeholder backend: ffmpeg colour picture + sine audio, no model."""
    name = "mock"

    # Test hook: {(shot_seq, take_no)} that fail exactly once, then succeed.
    fail_once_keys = set()

    def __init__(self, color: str = "0x2e5e4e", fps: int = 24):
        self.color = color
        self.fps = fps

    def generate(self, prompt: str, out_path: str, orientation: str = "landscape",
                 duration: float = 10.0, meta: dict = None) -> dict:
        meta = meta or {}
        key = (meta.get("shot_seq"), meta.get("take_no"))
        if key in self.fail_once_keys:
            self.fail_once_keys.discard(key)
            return {"ok": False, "error": "mock injected failure (test hook)"}
        size = "1280x720" if orientation == "landscape" else "720x1280"
        freq = 440 + 20 * int(meta.get("take_no", 1))
        os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
        cmd = [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i",
            f"color=c={self.color}:s={size}:r={self.fps}:d={duration}",
            "-f", "lavfi", "-i",
            f"sine=frequency={freq}:duration={duration}",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast",
            "-c:a", "aac", "-shortest", out_path,
        ]
        proc = subprocess.run(cmd, capture_output=True)
        if proc.returncode != 0 or not os.path.exists(out_path):
            return {"ok": False,
                    "error": proc.stderr.decode(errors="replace")[:300]}
        return {"ok": True, "file": out_path, "backend": self.name}


# Example skeleton for a real vendor adapter. Not wired in; copy, fill in
# the vendor call, and register it in server.py.
class VendorAdapterTemplate(VideoBackend):
    """Template: replace `call_vendor` with a real provider SDK/API call.

    Requirements for any real adapter:
    - honour `duration` (10 s shots) and `orientation`;
    - write exactly one file to `out_path` (mp4, H.264 + AAC);
    - return {"ok": False, "error": ...} on any failure, never a partial
      file presented as done (delete partial output first);
    - never burn watermarks, subtitles or text into the picture.
    """
    name = "vendor-template"

    def call_vendor(self, prompt: str, out_path: str, orientation: str,
                    duration: float) -> dict:
        raise NotImplementedError("wire a real video provider here")

    def generate(self, prompt: str, out_path: str, orientation: str = "landscape",
                 duration: float = 10.0, meta: dict = None) -> dict:
        try:
            return self.call_vendor(prompt, out_path, orientation, duration)
        except Exception as exc:  # noqa: BLE001 - surface as engine failure
            if os.path.exists(out_path):
                os.remove(out_path)
            return {"ok": False, "error": str(exc)[:300]}
