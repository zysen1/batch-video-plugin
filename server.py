"""Server entry point.

Usage:
    python3 server.py [--port 8000] [--data-dir ./data] [--allow-test-hooks]

Data lives under <data-dir>/app.db and <data-dir>/media (both gitignored).
Requires ffmpeg/ffprobe on PATH. No pip dependencies.
"""
import argparse
import os
import threading
import time
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

from batch_plugin import engine
from batch_plugin.adapters import MockAdapter
from batch_plugin.db import DB
from batch_plugin.httpapi import Api, _send

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")


def make_handler(api: Api, media_dir: str, web_dir: str):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):  # keep stdout clean
            pass

        def _serve_file(self, path, content_type):
            try:
                with open(path, "rb") as fh:
                    data = fh.read()
            except OSError:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _handle(self, method):
            parsed = urlparse(self.path)
            path = unquote(parsed.path)
            if path.startswith("/media/"):
                rel = path[len("/media/"):]
                full = os.path.realpath(os.path.join(media_dir, rel))
                if not full.startswith(os.path.realpath(media_dir) + os.sep):
                    self.send_response(403)
                    self.end_headers()
                    return
                self._serve_file(full, "video/mp4")
                return
            if method == "GET" and path in ("/", "/index.html"):
                self._serve_file(os.path.join(web_dir, "index.html"), "text/html; charset=utf-8")
                return
            if method == "GET" and path == "/app.js":
                self._serve_file(os.path.join(web_dir, "app.js"), "application/javascript; charset=utf-8")
                return
            if method == "GET" and path == "/style.css":
                self._serve_file(os.path.join(web_dir, "style.css"), "text/css; charset=utf-8")
                return
            status, payload = api.route(self, method, path)
            _send(self, status, payload)

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

        def do_PUT(self):
            self._handle("PUT")

    return Handler


def watchdog_loop(db: DB, media_dir: str, backend, stale_seconds: int):
    """Every minute: requeue stuck takes and relaunch their projects."""
    while True:
        time.sleep(60)
        try:
            stuck_projects = {
                row["project_id"]
                for row in db.query(
                    "SELECT shots.project_id AS project_id FROM takes "
                    "JOIN shots ON shots.id = takes.shot_id "
                    "WHERE takes.status = 'generating'")
            }
            result = engine.requeue_stale(db, stale_seconds)
            if result["requeued_takes"]:
                for project_id in stuck_projects:
                    project = engine.get_project(db, project_id)
                    if project and project["status"] == "generating":
                        engine.launch(db, project_id, backend, media_dir)
        except Exception:
            continue


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--data-dir", default="./data")
    parser.add_argument("--allow-test-hooks", action="store_true")
    args = parser.parse_args()

    data_dir = os.path.abspath(args.data_dir)
    media_dir = os.path.join(data_dir, "media")
    os.makedirs(media_dir, exist_ok=True)
    db = DB(os.path.join(data_dir, "app.db"))
    # Crash recovery: anything left "generating" never reported, requeue it.
    engine.recover_on_startup(db)

    backend = MockAdapter()
    allow_hooks = args.allow_test_hooks or os.environ.get("BATCH_ALLOW_TEST_HOOKS") == "1"
    api = Api(db, media_dir, backend, allow_test_hooks=allow_hooks)

    stale_seconds = int(os.environ.get("BATCH_STALE_SECONDS", "1800"))
    watchdog = threading.Thread(
        target=watchdog_loop, args=(db, media_dir, backend, stale_seconds),
        daemon=True)
    watchdog.start()

    server = ThreadingHTTPServer((args.host, args.port), make_handler(api, media_dir, WEB_DIR))
    server.daemon_threads = True
    print(f"batch-video-plugin listening on http://{args.host}:{args.port}")
    print(f"data dir: {data_dir}  (backend: {backend.name})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
