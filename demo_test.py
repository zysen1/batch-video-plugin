"""End-to-end demo test: runs the real server + mock ffmpeg backend.

Scenario (generic demo story, invented for this test only):
  清晨面包房：年轻面包师烤出第一炉面包，街坊闻香而来
  4 shots x 3 takes = 12 queued; round cap 6; auto-continue; stop after
  assembly; fail-once requeue; auto review; edit plan; ffmpeg polish;
  manual fail -> reroll -> re-review -> polish v2 -> approve; disk import.

Run:  python3 demo_test.py
Exit code 0 = all checks passed. The server runs in a temp data dir and is
stopped at the end; nothing is left in the repository.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request

PORT = 18765
BASE = f"http://127.0.0.1:{PORT}"

BEATS = [
    {"visual": "天未亮，面包师推开面包房的门，开灯、系上围裙，开始揉面。",
     "dialogue": "面包师（轻声）：“今天的第一炉，得准时。”", "camera": "固定机位"},
    {"visual": "面团进烤箱，面包师擦着手守在炉边，透过炉窗看面团慢慢鼓起。",
     "dialogue": "面包师：“再等三分钟，皮就脆了。”", "camera": "缓慢推轨"},
    {"visual": "第一炉面包出炉，热气腾腾，面包师把面包摆上木架，街坊推门进来。",
     "dialogue": "街坊：“好香！刚出炉的？”", "camera": "跟拍"},
    {"visual": "街坊们围在木架前挑选面包，面包师笑着递出刚出炉的一只，晨光洒满小店。",
     "dialogue": "面包师：“来，趁热尝一个。”", "camera": "环绕"},
]

CHARACTER_LOCK = ("面包师：年轻男性，深色围裙、白衬衫，全片同一人、同一套服装；"
                  "街坊：中年女性，米色外套，不得与面包师同款服装。")
LOCATION_LOCK = "清晨的面包房：木架、烤箱、临街小店；全片明亮晨光，同一光源、同一色温。"
VISUAL_LOCK = "电影感摄影，柔和均匀光线；真实皮肤质感不磨皮；画面内不烧录水印或文字。"


def api(path, method="GET", body=None):
    req = urllib.request.Request(BASE + path, method=method)
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, data=data, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        raise RuntimeError(f"{method} {path} -> HTTP {e.code}: {detail}")


def check(name, cond, extra=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {extra}")
    if not cond:
        raise AssertionError(name)


def wait_until(pred, timeout=300, interval=2, label=""):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if pred():
                return True
        except Exception:
            pass  # not ready yet (e.g. connection refused during startup)
        time.sleep(interval)
    raise TimeoutError(f"timeout waiting: {label}")


def detail(pid):
    return api(f"/api/projects/{pid}")


def main():
    tmp = tempfile.mkdtemp(prefix="batch_demo_")
    data_dir = os.path.join(tmp, "data")
    env = dict(os.environ, BATCH_ALLOW_TEST_HOOKS="1")
    proc = subprocess.Popen(
        [sys.executable, "server.py", "--port", str(PORT),
         "--data-dir", data_dir, "--allow-test-hooks"],
        cwd=os.path.dirname(os.path.abspath(__file__)),
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        wait_until(lambda: api("/api/health").get("ok"), timeout=30,
                   label="server health")

        # 1. Create project + locks + plan
        res = api("/api/projects", "POST", {
            "title": "清晨面包房", "idea": "清晨面包房：年轻面包师烤出第一炉面包，街坊闻香而来",
            "genre": "记录", "orientation": "landscape", "takesPerShot": 3,
            "autoMode": True})
        pid = res["projectId"]
        api(f"/api/projects/{pid}/locks", "POST", {
            "characterLock": CHARACTER_LOCK, "locationLock": LOCATION_LOCK,
            "visualLock": VISUAL_LOCK})
        plan_res = api(f"/api/projects/{pid}/plan", "POST",
                       {"shotCount": 4, "beats": BEATS})
        check("规划出 4 镜", len(plan_res["shots"]) == 4)
        d = detail(pid)
        check("锁定逐字进提示词",
              CHARACTER_LOCK in d["shots"][0]["prompt_full"])
        check("第 2 镜起有接力规则",
              "从上一镜的尾帧直接接续" in d["shots"][1]["prompt_full"]
              and "从上一镜的尾帧直接接续" not in d["shots"][0]["prompt_full"])

        # 2. Fail-once injection + launch + auto rounds
        api(f"/api/projects/{pid}/test-fail-once", "POST",
            {"shotSeq": 2, "takeNo": 2})
        api(f"/api/projects/{pid}/launch", "POST", {})
        wait_until(lambda: detail(pid)["project"]["status"] == "assembled",
                   timeout=300, label="assembled")
        d = detail(pid)
        rounds = d["rounds"]
        check("至少自动续跑 2 轮", len(rounds) >= 2, f"rounds={len(rounds)}")
        oldest = rounds[-1]
        check("首轮成功数 = 6 上限", oldest["successes"] == 6,
              f"successes={oldest['successes']}")
        takes = [t for s in d["shots"] for t in s["takes"]]
        done = [t for t in takes if t["status"] == "done"]
        queued = [t for t in takes if t["status"] == "queued"]
        check("成片后多余候选不再生成", len(done) == 10 and len(queued) == 2,
              f"done={len(done)} queued={len(queued)}")
        for shot in d["shots"]:
            first = next(t for t in shot["takes"] if t["take_no"] == 1)
            check(f"第 {shot['seq']} 镜首条完成即选定",
                  shot["selected_take_id"] == first["id"])
        shot2 = d["shots"][1]
        s2t2 = next(t for t in shot2["takes"] if t["take_no"] == 2)
        check("失败条目回排队后重试成功", s2t2["status"] == "done"
              and s2t2["attempts"] == 2, f"attempts={s2t2['attempts']}")
        check("发布文案含 AI 标识",
              "本片全部画面由 AI 生成，非实拍" in d["project"]["publish_copy"])

        # 3. Auto review (mock frames are uniform -> all pass)
        rev = api(f"/api/projects/{pid}/review", "POST", {})
        check("终审覆盖 4 镜", len(rev["reviews"]) == 4)
        for r in rev["reviews"]:
            check(f"终审镜 {r['shot_id']} 规格与真实感分开计分",
                  "规格通过" in r["issues"] and "不等于真实感" in r["issues"])
            check(f"终审镜 {r['shot_id']} 对白标注未验证",
                  "抽帧无法核口型" in r["issues"])
        check("mock 占位片终审全通过",
              all(r["verdict"] == "pass" for r in rev["reviews"]))

        # 4. Edit plan + ffmpeg polished film
        plan = api(f"/api/projects/{pid}/edit-plan", "POST", {})["plan"]
        check("精剪方案含 4 镜、无再抽", len(plan["shots"]) == 4
              and plan["reroll_shot_ids"] == [])
        pol = api(f"/api/projects/{pid}/polish", "POST", {})
        check("精剪成片 40 秒", abs(pol["duration"] - 40.0) < 1.0,
              f"duration={pol['duration']}")
        final_id = pol["finalVideoId"]

        # 5. Manual fail -> reroll -> re-review -> polish v2 -> approve
        shot3_id = d["shots"][2]["id"]
        api(f"/api/projects/{pid}/review-manual", "POST", {
            "shotId": shot3_id, "verdict": "fail", "signals": ["identity_drift"],
            "realismScore": 5, "continuityScore": 3,
            "editSuggestion": "不要硬剪：必须再抽，本条不进入二次剪辑。",
            "issues": "人工复核：人物身份漂移。"})
        plan2 = api(f"/api/projects/{pid}/edit-plan", "POST", {})["plan"]
        check("不通过镜头列入再抽、不进精剪",
              plan2["reroll_shot_ids"] == [shot3_id]
              and len(plan2["shots"]) == 3)
        api(f"/api/projects/{pid}/reroll-failed", "POST", {})
        wait_until(
            lambda: detail(pid)["project"]["status"] == "assembled",
            timeout=300, label="re-assembled after reroll")
        d = detail(pid)
        shot3 = next(s for s in d["shots"] if s["id"] == shot3_id)
        new_take = next(t for t in shot3["takes"] if t["take_no"] == 4)
        check("再抽新条完成并自动选定",
              new_take["status"] == "done"
              and shot3["selected_take_id"] == new_take["id"])
        rev2 = api(f"/api/projects/{pid}/review", "POST", {})
        check("补审后第 3 镜通过",
              next(r for r in rev2["reviews"]
                   if r["shot_id"] == shot3_id)["verdict"] == "pass")
        plan3 = api(f"/api/projects/{pid}/edit-plan", "POST", {})["plan"]
        check("补审后精剪方案恢复 4 镜", len(plan3["shots"]) == 4)
        pol2 = api(f"/api/projects/{pid}/polish", "POST", {})
        check("精剪 v2 仍为 40 秒", abs(pol2["duration"] - 40.0) < 1.0,
              f"duration={pol2['duration']}")
        api(f"/api/projects/{pid}/approve", "POST",
            {"finalVideoId": pol2["finalVideoId"]})
        d = detail(pid)
        check("定稿标记成功",
              next(f for f in d["finalVideos"]
                   if f["id"] == pol2["finalVideoId"])["approved"] == 1)

        # 6. Disk import: reuse an existing generated clip as an import
        src_take = d["shots"][0]["takes"][0]
        src_path = src_take["file_path"]
        with open(src_path, "rb") as fh:
            blob = fh.read()
        boundary = "----demo-boundary"
        body = (f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="file"; filename="clip.mp4"\r\n'
                f"Content-Type: video/mp4\r\n\r\n").encode() + blob + \
               f"\r\n--{boundary}--\r\n".encode()
        req = urllib.request.Request(
            BASE + f"/api/projects/{pid}/import/{d['shots'][1]['id']}",
            method="POST", data=body)
        req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
        with urllib.request.urlopen(req, timeout=30) as resp:
            imp = json.loads(resp.read().decode())
        check("磁盘导入候选成功", imp.get("ok") is True
              and imp.get("takeId") is not None)

        print("\nALL DEMO CHECKS PASSED")
        return 0
    except Exception:
        # Surface the server log to make failures diagnosable.
        proc.terminate()
        try:
            out, _ = proc.communicate(timeout=10)
            print("\n--- server output ---\n" + (out or ""))
        except Exception:
            pass
        raise
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()


if __name__ == "__main__":
    sys.exit(main())
