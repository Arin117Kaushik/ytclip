#!/usr/bin/env python3
"""ytclip web page: paste a link, set start and end, download the clip.

    python app.py            open http://127.0.0.1:8765 on this computer
    python app.py --lan      also reachable from a phone on the same Wi-Fi

Standard library only. The clip work is done by clip.py.
"""
import argparse
import json
import shutil
import socket
import tempfile
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, urlsplit

import clip

PAGE = Path(__file__).with_name("web") / "index.html"
INFO_MAX_AGE = 30 * 60  # stream addresses expire after a few hours, so reuse them only briefly
JOBS = {}
INFO = {}  # (url, res) -> (time fetched, yt-dlp info)
# ponytail: finished clips stay in WORK until the server stops. Add a sweep if it runs for days.
WORK = Path(tempfile.mkdtemp(prefix="ytclip-"))


def get_info(url, res):
    if not url.startswith(("https://", "http://")):
        raise clip.Unfixable("That is not a link. It should start with https://")
    cached = INFO.get((url, res))
    if cached and time.time() - cached[0] < INFO_MAX_AGE:
        return cached[1]
    try:
        info = clip.resolve(url, res, clip.IPV4)
    except RuntimeError:
        info = clip.resolve(url, res, clip.AUTO)
    INFO[(url, res)] = (time.time(), info)
    return info


def run_job(job, url, start, end, res):
    def say(text):
        job["step"] = text.strip()

    def tick(percent):
        job["percent"] = round(percent)

    try:
        cached = INFO.pop((url, res), None)
        fresh = cached[1] if cached and time.time() - cached[0] < INFO_MAX_AGE else None
        made = clip.make_clip(url, start, end, res, WORK / job["id"], say, tick, fresh)
        job.update(state="done", path=made, name=made.name, percent=100)
    except (clip.Unfixable, RuntimeError, OSError) as err:
        job.update(state="error", error=str(err))


class Handler(BaseHTTPRequestHandler):
    lan = False

    def log_message(self, *args):
        pass

    def allowed(self):
        """Only this page may call the API. Stops other websites driving the downloader."""
        host = self.headers.get("Host", "")
        if not self.lan and host.split(":")[0] not in ("127.0.0.1", "localhost"):
            return False
        origin = self.headers.get("Origin")
        return origin is None or urlsplit(origin).netloc == host

    def send_json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self.allowed():
            return self.send_json({"error": "Not allowed"}, 403)
        parts = self.path.split("?")[0].strip("/").split("/")
        if parts == [""]:
            body = PAGE.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            return self.wfile.write(body)
        job = JOBS.get(parts[-1]) if len(parts) == 3 and parts[0] == "api" else None
        if job and parts[1] == "job":
            return self.send_json({k: job[k] for k in ("state", "step", "percent", "error", "name")})
        if job and parts[1] == "file" and job["state"] == "done":
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(job["path"].stat().st_size))
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(job['name'])}")
            self.end_headers()
            with job["path"].open("rb") as f:
                return shutil.copyfileobj(f, self.wfile)
        self.send_json({"error": "Not found"}, 404)

    def do_POST(self):
        if not self.allowed():
            return self.send_json({"error": "Not allowed"}, 403)
        try:
            ask = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            url, res = str(ask["url"]).strip(), int(ask.get("res", 1080))
            if self.path == "/api/info":
                info = get_info(url, res)
                return self.send_json({"title": info["title"], "duration": info.get("duration"),
                                       "thumbnail": info.get("thumbnail"), "name": clip.safe_name(info["title"])})
            if self.path == "/api/clip":
                start, end = clip.parse_time(str(ask["start"])), clip.parse_time(str(ask["end"]))
                job = {"id": uuid.uuid4().hex, "state": "working", "step": "Starting...",
                       "percent": 0, "error": "", "name": "", "path": None}
                JOBS[job["id"]] = job
                threading.Thread(target=run_job, args=(job, url, start, end, res), daemon=True).start()
                return self.send_json({"job": job["id"]})
            self.send_json({"error": "Not found"}, 404)
        except (clip.Unfixable, RuntimeError, argparse.ArgumentTypeError) as err:
            self.send_json({"error": str(err)}, 400)
        except (KeyError, ValueError, TypeError):
            self.send_json({"error": "Bad request"}, 400)


def lan_address():
    """This computer's address on the local network. No data is sent."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
        except OSError:
            return "127.0.0.1"


def main():
    ap = argparse.ArgumentParser(description="ytclip web page")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--lan", action="store_true", help="allow phones and other devices on your Wi-Fi")
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()
    Handler.lan = a.lan
    server = ThreadingHTTPServer(("0.0.0.0" if a.lan else "127.0.0.1", a.port), Handler)
    here = f"http://127.0.0.1:{a.port}"
    print(f"ytclip is running at {here}")
    if a.lan:
        print(f"On your phone (same Wi-Fi): http://{lan_address()}:{a.port}")
    print("Press Ctrl+C to stop.")
    if not a.no_browser:
        webbrowser.open(here)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        shutil.rmtree(WORK, ignore_errors=True)


if __name__ == "__main__":
    main()
