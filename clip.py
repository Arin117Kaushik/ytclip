#!/usr/bin/env python3
"""ytclip: download an exact clip from a YouTube video without fetching the whole thing.

    python clip.py URL START END [-o FOLDER_OR_FILE] [--res 1080]

Needs yt-dlp, ffmpeg and ffprobe on PATH. Standard library only.

How it works: yt-dlp only resolves the stream addresses. ffmpeg then reads just the
wanted part straight from YouTube and re-encodes it, so the cut is frame exact and the
file is plain H.264 + AAC that every editor opens. Every result is checked with ffprobe,
and if a route fails the next one is tried.
"""
import argparse
import json
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ENCODE = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
          "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart"]
TOLERANCE = 0.5  # seconds a clip may differ from the requested length
# YouTube often bot-blocks one of a connection's two addresses (IPv4 / IPv6) but not the other.
IPV4, AUTO = ["--force-ipv4"], []


def parse_time(text):
    """'90', '1:30', '1:02:03.5' -> seconds."""
    if not re.fullmatch(r"\d+(:\d{1,2}){0,2}(\.\d+)?", text.strip()):
        raise argparse.ArgumentTypeError(f"bad timestamp {text!r}, use 90 or 1:30 or 1:02:03")
    seconds = 0.0
    for part in text.strip().split(":"):
        seconds = seconds * 60 + float(part)
    return seconds


def label(seconds):
    """Filename-safe timestamp: 125.5 -> '2m05.5s'."""
    m, s = divmod(round(seconds, 2), 60)
    return f"{int(m)}m{s:05.2f}".rstrip("0").rstrip(".") + "s"


def safe_name(title):
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", title)[:80].strip(" .") or "clip"


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")


def ytdlp_base(res, net):
    return ["yt-dlp", "--no-playlist", *net, "-f", "bv*+ba/b", "-S", f"res:{res},vcodec:h264,acodec:m4a"]


def resolve(url, res, net):
    """Ask yt-dlp for the title and stream addresses. One request to YouTube."""
    done = run([*ytdlp_base(res, net), "-j", url])
    if done.returncode != 0:
        reason = [l for l in done.stderr.splitlines() if l.startswith("ERROR")]
        raise RuntimeError(reason[-1][:200] if reason else "yt-dlp could not read the video")
    return json.loads(done.stdout)


class Unfixable(Exception):
    """The request itself is wrong, so trying another route will not help."""


def show_percent(percent):
    print(f"\r  {percent:3.0f}%", end="" if percent < 100 else "\n", flush=True)


def ffmpeg_cut(inputs, start, length, out, tick):
    """Encode `length` seconds starting at `start`. `inputs` is a list of (address, headers)."""
    cmd = ["ffmpeg", "-y", "-v", "error", "-nostats", "-progress", "pipe:1"]
    for address, headers in inputs:
        if headers:  # a remote stream
            # YouTube throttles open-ended requests to ~50 KB/s. Asking in 5 MB pieces
            # avoids that (needs ffmpeg from 2025 or newer).
            cmd += ["-request_size", "5000000",
                    # YouTube drops the audio connection while a slow encode keeps it idle.
                    "-reconnect", "1", "-reconnect_on_network_error", "1", "-reconnect_delay_max", "5",
                    "-rw_timeout", "30000000",  # give up if the network stalls for 30s
                    "-headers", "".join(f"{k}: {v}\r\n" for k, v in headers.items())]
        cmd += ["-ss", str(start), "-i", address]
    cmd += ["-t", str(length), "-map", "0:v:0", "-map", f"{len(inputs) - 1}:a:0", *ENCODE, str(out)]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace")
    finished, errors = False, []
    for line in proc.stdout:
        key, _, value = line.strip().partition("=")
        if key == "out_time_us" and value.isdigit():
            tick(min(99, int(value) / 1e6 / length * 100))
        elif line.strip() == "progress=end":
            finished = True
            break
        elif "=" not in line and line.strip():
            errors.append(line.strip())
    # The file is finished here, so never let a slow connection close hold us up.
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    if not finished or not Path(out).is_file():
        Path(out).unlink(missing_ok=True)  # never leave a half-written clip behind
        raise RuntimeError(errors[0][:160] if errors else "ffmpeg stopped early")
    tick(100)


def section(url, start, end, res, out_for, net, tick, info=None):
    """Fetch only the wanted section. `info` is an earlier resolve() result to reuse."""
    info = info or resolve(url, res, net)
    if end > (info.get("duration") or end) + TOLERANCE:
        raise Unfixable(f"END is past the end of the video, which is {info['duration']:g}s long")
    formats = info.get("requested_formats") or [info]
    out = out_for(info["title"])
    inputs = [(f["url"], f.get("http_headers") or {"User-Agent": "Mozilla/5.0"}) for f in formats]
    # Fresh stream addresses answer 403 for the first second or two, so retry the open.
    for attempt in range(5):
        time.sleep(2)
        try:
            ffmpeg_cut(inputs, start, end - start, out, tick)
            return out
        except RuntimeError as err:
            if "403" not in str(err) or attempt == 4:
                raise


def whole_then_cut(url, start, end, res, out_for, net, tick):
    """Last resort: fetch the full video with yt-dlp's own downloader, cut it locally."""
    with tempfile.TemporaryDirectory() as tmp:
        cmd = [*ytdlp_base(res, net), "--merge-output-format", "mp4", "--print", "after_move:%(title)s",
               "-o", str(Path(tmp) / "full.%(ext)s"), url]
        done = run(cmd)
        full = next(Path(tmp).glob("full.*"), None)
        if done.returncode != 0 or not full:
            raise RuntimeError("full download failed")
        out = out_for(done.stdout.strip().splitlines()[-1])
        ffmpeg_cut([(str(full), None)], start, end - start, out, tick)
        return out


def verify(path, want):
    """Return None if the file is a good clip of `want` seconds, else the reason it is not."""
    if not path.is_file() or path.stat().st_size < 10_000:
        return "no output file"
    probe = run(["ffprobe", "-v", "error", "-show_entries",
                 "stream=codec_type,duration", "-of", "json", str(path)])
    try:
        lengths = {s["codec_type"]: float(s["duration"]) for s in json.loads(probe.stdout)["streams"]}
    except (ValueError, KeyError):
        return "file is not readable"
    # Checked per stream: a dropped connection can cut the audio short while the video is whole.
    for kind in ("video", "audio"):
        if kind not in lengths:
            return f"no {kind} in the file"
        if abs(lengths[kind] - want) > TOLERANCE:
            return f"{kind} is {lengths[kind]:.2f}s long, expected {want:.2f}s"
    # Decode the whole clip. Catches corrupt or truncated downloads.
    if run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "null", "-"]).stderr.strip():
        return "file has decode errors"
    return None


def make_clip(url, start, end, res, dest, say=print, tick=show_percent, info=None):
    """Make the clip and return its path. `dest` is a folder or an .mp4 path.

    Raises Unfixable for a bad request and RuntimeError when every route failed.
    """
    if not re.match(r"https?://", url):
        raise Unfixable("That is not a link. It should start with https://")
    if end <= start:
        raise Unfixable("END must be after START")
    dest = Path(dest).expanduser()
    is_file = dest.suffix.lower() == ".mp4"
    (dest.parent if is_file else dest).mkdir(parents=True, exist_ok=True)

    def out_for(title):
        return dest if is_file else dest / f"{safe_name(title)} [{label(start)}-{label(end)}].mp4"

    job = (url, start, end, res, out_for)
    steps = [
        ("Downloading just the clip", lambda: section(*job, IPV4, tick, info)),
        ("Trying the other network route", lambda: section(*job, AUTO, tick)),
        ("Updating yt-dlp and trying again", lambda: run(["yt-dlp", "-U"]) and section(*job, IPV4, tick)),
        ("Downloading the full video and cutting it locally", lambda: whole_then_cut(*job, IPV4, tick)),
    ]
    problem = "nothing was tried"
    for name, step in steps:
        say(f"{name}...")
        try:
            made = step()
            problem = verify(made, end - start)
        except (RuntimeError, OSError, ValueError) as err:
            made, problem = None, str(err)
        if problem is None:
            return made
        say(f"  did not work: {problem}")
        if made:
            made.unlink(missing_ok=True)
    raise RuntimeError(f"Every route failed. Last problem: {problem}")


def main():
    ap = argparse.ArgumentParser(description="Download an exact clip from a YouTube video.")
    ap.add_argument("url")
    ap.add_argument("start", type=parse_time, help="e.g. 90 or 1:30 or 1:02:03")
    ap.add_argument("end", type=parse_time)
    ap.add_argument("-o", "--out", default=".", help="folder or .mp4 file (default: current folder)")
    ap.add_argument("--res", type=int, default=1080, help="max height, e.g. 720 (default 1080)")
    a = ap.parse_args()
    try:
        made = make_clip(a.url, a.start, a.end, a.res, a.out)
    except (Unfixable, RuntimeError) as err:
        print(f"Failed. {err}", file=sys.stderr)
        return 1
    print(f"Saved {a.end - a.start:g}s clip: {made.resolve()}")
    return 0


def self_test():
    assert parse_time("90") == 90 and parse_time("1:30") == 90 and parse_time("1:02:03.5") == 3723.5
    assert label(125.5) == "2m05.5s" and label(60) == "1m00s" and label(0) == "0m00s"
    assert safe_name('a: b/c? "d"') == "a bc d" and safe_name("...") == "clip"
    for bad in ("abc", "1:2:3:4", "-5", ""):
        try:
            parse_time(bad)
        except argparse.ArgumentTypeError:
            continue
        raise AssertionError(bad)
    print("ok")


if __name__ == "__main__":
    sys.exit(self_test() if sys.argv[1:] == ["--self-test"] else main())
