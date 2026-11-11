#!/usr/bin/env python3
"""ytclip web page: paste a link, set start and end, download the clip.

    python app.py            open http://127.0.0.1:8765 on this computer
    python app.py --lan      also reachable from a phone on the same Wi-Fi

Standard library only. The clip work is done by clip.py.
"""
import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
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
SETTINGS = Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".config") / "ytclip" / "settings.json"
# Runs in its own process: a folder dialog needs a main thread, and the server's is busy.
DIALOG = """
import sys, tkinter, tkinter.filedialog
root = tkinter.Tk()
root.withdraw()
root.attributes("-topmost", True)
sys.stdout.reconfigure(encoding="utf-8")
print(tkinter.filedialog.askdirectory(initialdir=sys.argv[1], title="Save ytclip downloads to"))
"""


def save_folder():
    """Where this computer's downloads go. The browser is not allowed to write to Downloads, so the app does."""
    try:
        return Path(json.loads(SETTINGS.read_text(encoding="utf-8"))["folder"])
    except (OSError, ValueError, KeyError, TypeError):
        return Path.home() / "Downloads"


def pick_folder():
    """Show the system's folder dialog and remember the choice. Closing the dialog keeps the old folder."""
    done = subprocess.run([sys.executable, "-c", DIALOG, str(save_folder())], capture_output=True,
                          text=True, encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL)
    if done.returncode != 0:
        raise RuntimeError("The folder dialog could not be opened")
    chosen = done.stdout.strip()
    if chosen:
        SETTINGS.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS.write_text(json.dumps({"folder": chosen}), encoding="utf-8")
    return save_folder()


def get_info(url, res):
    cached = INFO.get((url, res))
    if cached and time.time() - cached[0] < INFO_MAX_AGE:
        return cached[1]
    info = clip.lookup(url, res)
    INFO[(url, res)] = (time.time(), info)
    return info


def run_job(job, url, start, end, res, mode, dest):
    def say(text):
        job.update(step=text.strip(), percent=0)
        job["log"].append(text.strip())  # so a failed route can be diagnosed from /api/job/<id>

    def tick(percent):
        job["percent"] = round(percent)

    try:
        # Kept in the cache on success, so more clips from the same video cost no new lookup.
        cached = INFO.get((url, res))
        fresh = cached[1] if cached and time.time() - cached[0] < INFO_MAX_AGE else None
        if start is None:
            made = clip.whole_video(url, res, dest, say, tick, fresh, mode)
        else:
            made = clip.make_clip(url, start, end, res, dest, say, tick, fresh, mode)
        job.update(state="done", path=made, name=made.name, percent=100)
    except Exception as err:  # a job must always end in "done" or "error", or the page waits forever
        INFO.pop((url, res), None)
        job.update(state="error", error=str(err) or type(err).__name__)


class Server(ThreadingHTTPServer):
    # The default lets a second copy bind the same port on Windows, and the two then split requests.
    allow_reuse_address = os.name != "nt"


class Handler(BaseHTTPRequestHandler):
    lan = False

    def log_message(self, *args):
        pass

    def allowed(self):
        """Only this page may call the API. Stops other websites driving the downloader."""
        host = self.headers.get("Host", "")
        name = host.rsplit(":", 1)[0]
        # A site name could be pointed at this computer by someone else. A bare address cannot.
        if name not in ("127.0.0.1", "localhost") and not (self.lan and re.fullmatch(r"[\d.]+", name)):
            return False
        origin = self.headers.get("Origin")
        return origin is None or urlsplit(origin).netloc == host

    def send_body(self, body, kind, status=200):
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, data, status=200):
        self.send_body(json.dumps(data).encode(), "application/json", status)

    def do_GET(self):
        if not self.allowed():
            return self.send_json({"error": "Not allowed"}, 403)
        parts = self.path.split("?")[0].strip("/").split("/")
        if parts == [""]:
            return self.send_body(PAGE.read_bytes(), "text/html; charset=utf-8")
        job = JOBS.get(parts[-1]) if len(parts) == 3 and parts[0] == "api" else None
        if job and parts[1] == "job":
            return self.send_json({k: job[k] for k in ("state", "step", "percent", "error", "name", "log", "saved")})
        if job and parts[1] == "file" and job["state"] == "done":
            self.send_response(200)
            self.send_header("Content-Type", "audio/mpeg" if job["name"].endswith(".mp3") else "video/mp4")
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
            # Only this computer saves straight to a folder. A phone on the Wi-Fi gets a normal download.
            local = self.client_address[0] in ("127.0.0.1", "::1")
            if self.path == "/api/folder":
                if not local:
                    return self.send_json({"folder": None})
                folder = pick_folder() if ask.get("pick") else save_folder()
                return self.send_json({"folder": str(folder), "name": folder.name or str(folder)})
            url, res = str(ask["url"]).strip(), int(ask.get("res", 1080))
            if self.path == "/api/info":
                info = get_info(url, res)
                return self.send_json({"title": info["title"], "duration": info.get("duration"),
                                       "thumbnail": info.get("thumbnail"), "name": clip.safe_name(info["title"])})
            if self.path == "/api/frame":
                return self.send_body(clip.frame(get_info(url, res), float(ask["at"])), "image/jpeg")
            if self.path == "/api/clip":
                mode = ask.get("mode", "video")
                if mode not in clip.ENCODE:
                    raise ValueError(mode)
                start = end = None  # None means the whole video
                if not ask.get("whole"):
                    start, end = clip.parse_time(str(ask["start"])), clip.parse_time(str(ask["end"]))
                job = {"id": uuid.uuid4().hex, "state": "working", "step": "Starting...",
                       "percent": 0, "error": "", "name": "", "path": None, "log": [], "saved": local}
                JOBS[job["id"]] = job
                threading.Thread(target=run_job, args=(job, url, start, end, res, mode, save_folder() if local else WORK / job["id"]), daemon=True).start()
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


def open_window(url):
    """Open the page in its own window, without tabs or an address bar.

    Returns the browser process, or None when it fell back to a normal browser tab.
    """
    roots = [os.environ.get(v) for v in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")]
    names = ("Google/Chrome/Application/chrome.exe", "Microsoft/Edge/Application/msedge.exe",
             "BraveSoftware/Brave-Browser/Application/brave.exe")
    found = [Path(r) / n for n in names for r in roots if r and (Path(r) / n).is_file()]
    exe = found[0] if found else shutil.which("google-chrome") or shutil.which("chromium")
    if not exe:
        webbrowser.open(url)
        return None
    # Its own profile folder keeps this window a separate process, so we can tell when it closes.
    profile = Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".cache") / "ytclip" / "window"
    return subprocess.Popen([str(exe), f"--app={url}", f"--user-data-dir={profile}",
                             "--no-first-run", "--window-size=720,900"])


def main():
    ap = argparse.ArgumentParser(description="ytclip app")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--lan", action="store_true", help="allow phones and other devices on your Wi-Fi")
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()
    Handler.lan = a.lan
    here = f"http://127.0.0.1:{a.port}"
    try:
        server = Server(("0.0.0.0" if a.lan else "127.0.0.1", a.port), Handler)
    except OSError:
        # Already running, so just bring up its window.
        shutil.rmtree(WORK, ignore_errors=True)
        if not a.no_browser:
            open_window(here)
        return
    print(f"ytclip is running at {here}")
    if a.lan:
        print(f"On your phone (same Wi-Fi): http://{lan_address()}:{a.port}")
    print("Press Ctrl+C to stop.")
    window = None if a.no_browser else open_window(here)
    try:
        if window and not a.lan:
            # Desktop app behaviour: closing the window quits ytclip.
            threading.Thread(target=server.serve_forever, daemon=True).start()
            window.wait()
        else:
            server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        shutil.rmtree(WORK, ignore_errors=True)


if __name__ == "__main__":
    main()
