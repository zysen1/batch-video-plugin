"""Tests for the ffmpeg render paths (needs ffmpeg on PATH).

Run:  python3 -m unittest discover -s tests -v
      (from the repository root)
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from batch_plugin.adapters import MockAdapter
from batch_plugin.assemble import _chain_render, _segment_pass
from batch_plugin.media import ffprobe


class AssembleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.adapter = MockAdapter()

    def _clip(self, name, orientation="landscape"):
        path = os.path.join(self.tmp, name)
        res = self.adapter.generate("prompt", path, orientation=orientation,
                                    meta={"take_no": 1})
        self.assertTrue(res["ok"], res)
        return path

    def test_hard_cut_chain_duration(self):
        a = self._clip("a.mp4")
        b = self._clip("b.mp4")
        out = os.path.join(self.tmp, "hard.mp4")
        _chain_render([a, b], [10.0, 10.0], ["hard"], 0, out)
        self.assertAlmostEqual(ffprobe(out)["duration"], 20.0, delta=0.3)

    def test_crossfade_chain_duration(self):
        a = self._clip("a.mp4")
        b = self._clip("b.mp4")
        out = os.path.join(self.tmp, "xfade.mp4")
        _chain_render([a, b], [10.0, 10.0], ["xfade"], 0, out)
        # 3-frame dissolve at 24 fps overlaps 0.125 s.
        self.assertAlmostEqual(ffprobe(out)["duration"], 20.0 - 0.125, delta=0.3)

    def test_single_segment_with_grain(self):
        a = self._clip("a.mp4")
        out = os.path.join(self.tmp, "single.mp4")
        _chain_render([a], [10.0], [], 4, out)
        self.assertAlmostEqual(ffprobe(out)["duration"], 10.0, delta=0.3)

    def test_segment_trim_and_portrait(self):
        clip = self._clip("p.mp4", orientation="portrait")
        info = ffprobe(clip)
        self.assertEqual((info["width"], info["height"]), (720, 1280))
        seg = os.path.join(self.tmp, "seg.mp4")
        dur = _segment_pass(clip, 0.4, 0.0, seg, "720x1280")
        self.assertAlmostEqual(dur, 9.6, delta=0.3)


if __name__ == "__main__":
    unittest.main()
