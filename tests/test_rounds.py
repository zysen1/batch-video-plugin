"""Unit tests for round rules. No ffmpeg, no server, no network.

Run:  python3 -m unittest discover -s tests -v
      (from the repository root)
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from batch_plugin import engine
from batch_plugin.db import DB, now


class FakeBackend:
    """Scripted backend: outcomes[(shot_seq, take_no)] = list of ok flags."""

    def __init__(self, outcomes=None):
        self.outcomes = outcomes or {}

    def generate(self, prompt, out_path, orientation="landscape",
                 duration=10.0, meta=None):
        key = (meta.get("shot_seq"), meta.get("take_no"))
        queue = self.outcomes.get(key)
        if queue:
            ok = queue.pop(0)
        else:
            ok = True
        if ok:
            return {"ok": True, "file": out_path, "backend": "fake"}
        return {"ok": False, "error": "fake failure"}


def make_project(db, shots=4, takes=3):
    cur = db.execute(
        "INSERT INTO projects (title, idea, genre, orientation, "
        "takes_per_shot, auto_mode, status, created_at) "
        "VALUES ('demo', 'demo idea', '记录', 'landscape', ?, 1, 'planning', ?)",
        (takes, now()))
    pid = cur.lastrowid
    for seq in range(1, shots + 1):
        db.execute(
            "INSERT INTO shots (project_id, seq, title, camera, visual, "
            "dialogue, start_state, end_state, purpose, prompt_full) "
            "VALUES (?, ?, ?, '固定机位', 'v', 'd', 's', 'e', 'p', 'prompt')",
            (pid, seq, f"第{seq}镜"))
    return pid


class RoundRulesTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp()
        self.db = DB(os.path.join(tmp, "test.db"))
        self.media = os.path.join(tmp, "media")

    def _run(self, pid, backend):
        engine.queue_takes(self.db, pid)
        engine._worker_loop(self.db, pid, backend, self.media)

    def _takes(self, pid):
        rows = self.db.query(
            "SELECT takes.* FROM takes JOIN shots ON shots.id = takes.shot_id "
            "WHERE shots.project_id = ? ORDER BY shots.seq, takes.take_no",
            (pid,))
        return [self.db.row_to_dict(r) for r in rows]

    def _rounds(self, pid):
        rows = self.db.query(
            "SELECT * FROM rounds WHERE project_id = ? ORDER BY id", (pid,))
        return [self.db.row_to_dict(r) for r in rows]

    def test_round_cap_and_auto_continue_and_stop_after_assembly(self):
        pid = make_project(self.db, shots=4, takes=3)
        self._run(pid, FakeBackend())
        rounds = self._rounds(pid)
        self.assertGreaterEqual(len(rounds), 2, "第二轮应自动派发")
        self.assertEqual(rounds[0]["successes"], 6, "首轮成功数不得超过 6")
        takes = self._takes(pid)
        done = [t for t in takes if t["status"] == "done"]
        queued = [t for t in takes if t["status"] == "queued"]
        self.assertEqual(len(done), 10)
        self.assertEqual(len(queued), 2, "成片后多余候选不得再生成")
        self.assertTrue(engine.is_assembled(self.db, pid))
        shots = engine.get_shots(self.db, pid)
        for shot in shots:
            first = next(t for t in takes
                         if t["shot_id"] == shot["id"] and t["take_no"] == 1)
            self.assertEqual(shot["selected_take_id"], first["id"],
                             "自动模式下首条完成即选定")

    def test_failure_does_not_count_and_requeues(self):
        pid = make_project(self.db, shots=4, takes=3)
        backend = FakeBackend({(1, 1): [False, True]})
        self._run(pid, backend)
        takes = self._takes(pid)
        # Find shot 1 take 1 precisely:
        shot1 = engine.get_shots(self.db, pid)[0]
        s1t1 = next(t for t in takes
                    if t["shot_id"] == shot1["id"] and t["take_no"] == 1)
        self.assertEqual(s1t1["status"], "done")
        self.assertEqual(s1t1["attempts"], 2, "失败一次后回排队并重试成功")
        self.assertEqual(self._rounds(pid)[0]["successes"], 6,
                         "失败不计入 6 条上限")
        self.assertTrue(engine.is_assembled(self.db, pid))

    def test_attempts_cap_marks_failed(self):
        pid = make_project(self.db, shots=1, takes=1)
        backend = FakeBackend({(1, 1): [False, False, False, False]})
        self._run(pid, backend)
        take = self._takes(pid)[0]
        self.assertEqual(take["status"], "failed")
        self.assertEqual(take["attempts"], 3)

    def test_zombie_round_takeover(self):
        pid = make_project(self.db, shots=1, takes=1)
        engine.queue_takes(self.db, pid)
        take = self._takes(pid)[0]
        old = now() - 3600
        self.db.execute(
            "UPDATE takes SET status = 'generating', updated_at = ? WHERE id = ?",
            (old, take["id"]))
        self.db.execute(
            "INSERT INTO rounds (project_id, kind, status, message, successes, "
            "last_activity, created_at) VALUES (?, 'generate', 'running', '', 0, ?, ?)",
            (pid, old, old))
        result = engine.requeue_stale(self.db, stale_seconds=1800)
        self.assertEqual(result["requeued_takes"], 1)
        self.assertEqual(result["stale_rounds"], 1)
        self.assertEqual(self._takes(pid)[0]["status"], "queued")


if __name__ == "__main__":
    unittest.main()
