#!/usr/bin/env python3
"""ytclip: download an exact clip from a YouTube video without fetching the whole thing.

    python clip.py URL START END [-o FOLDER_OR_FILE] [--res 1080] [--vertical | --audio]
    python clip.py URL                   the whole video

Needs yt-dlp, ffmpeg and ffprobe on PATH. Standard library only.

How it works: yt-dlp only resolves the stream addresses. ffmpeg then reads just the
wanted part straight from YouTube and re-encodes it, so the cut is frame exact and the
file is plain H.264 + AAC that every editor opens. Every result is checked with ffprobe,
and if a route fails the next one is tried.
"""
import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

VIDEO = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart"]
# How each output mode is encoded. "vertical" keeps the middle 9:16 slice for Reels and Shorts;
# the min() leaves a video that is already vertical alone.
ENCODE = {
    "video": VIDEO,
    "vertical": ["-vf", "crop='min(iw,trunc(ih*9/16/2)*2)':ih", *VIDEO],
    "audio": ["-c:a", "libmp3lame", "-b:a", "192k"],
}
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


def extension(mode):
    return ".mp3" if mode == "audio" else ".mp4"


# The web app runs without a console. Without these, every yt-dlp and ffmpeg call
# would flash a black window, and inheriting the missing stdin can fail on Windows.
HIDDEN = {"stdin": subprocess.DEVNULL, "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", **HIDDEN)


class Unfixable(Exception):
    """The request itself is wrong, so trying another route will not help."""


# yt-dlp errors that no other route can get past.
PERMANENT = ("private video", "video unavailable", "video is unavailable", "video is not available",
             "members-only", "confirm your age", "unsupported url", "incomplete youtube id", "not a valid url")


def ytdlp(url, res, net, *extra):
    """Build a yt-dlp command. Every call that hands yt-dlp a link goes through here."""
    if not re.match(r"https?://", url):  # also stops a "link" like --exec being read as an option
        raise Unfixable("That is not a link. It should start with https://")
    # "/bv*" at the end is for videos that have no audio at all.
    return ["yt-dlp", "--no-playlist", "--encoding", "utf-8", *net, "-f", "bv*+ba/b/bv*",
            "-S", f"res:{res},vcodec:h264,acodec:m4a", *extra, "--", url]


def resolve(url, res, net):
    """Ask yt-dlp for the title and stream addresses. One request to YouTube."""
    done = run(ytdlp(url, res, net, "-j"))
    if done.returncode != 0:
        errors = [line for line in done.stderr.splitlines() if line.startswith("ERROR")]
        reason = errors[-1][:200] if errors else "yt-dlp could not read the video"
        raise (Unfixable if any(mark in reason.lower() for mark in PERMANENT) else RuntimeError)(reason)
    return json.loads(done.stdout)


def lookup(url, res):
    """resolve() over IPv4, then over the default route."""
    try:
        return resolve(url, res, IPV4)
    except RuntimeError:
        return resolve(url, res, AUTO)


def show_percent(percent):
    print(f"\r  {percent:3.0f}%", end="" if percent < 100 else "\n", flush=True)


def remote(fmt):
    """ffmpeg input options for reading one of YouTube's streams."""
    headers = fmt.get("http_headers") or {"User-Agent": "Mozilla/5.0"}
    # YouTube throttles open-ended requests to ~50 KB/s. Asking in 5 MB pieces
    # avoids that (needs ffmpeg from 2025 or newer).
    return ["-request_size", "5000000",
            # YouTube drops the audio connection while a slow encode keeps it idle.
            "-reconnect", "1", "-reconnect_on_network_error", "1", "-reconnect_delay_max", "5",
            "-rw_timeout", "30000000",  # give up if the network stalls for 30s
            "-headers", "".join(f"{k}: {v}\r\n" for k, v in headers.items())]


def streams_for(mode, has_audio):
    """Which streams the output must contain."""
    if mode == "audio":
        if not has_audio:
            raise Unfixable("This video has no sound to save")
        return ("audio",)
    return ("video", "audio") if has_audio else ("video",)


def ffmpeg_cut(inputs, start, length, out, tick, streams, mode):
    """Encode `length` seconds starting at `start`. `inputs` is a list of (address, input options)."""
    cmd = ["ffmpeg", "-y", "-v", "error", "-nostats", "-progress", "pipe:1"]
    for address, options in inputs:
        cmd += [*options, "-ss", str(start), "-i", address]
    cmd += ["-t", str(length)]
    if "video" in streams:
        cmd += ["-map", "0:v:0"]
    if "audio" in streams:
        cmd += ["-map", f"{len(inputs) - 1}:a:0"]
    cmd += [*ENCODE[mode], str(out)]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace", **HIDDEN)
    finished, errors = False, []
    try:
        for line in proc.stdout:
            key, _, value = line.strip().partition("=")
            if key == "out_time_us" and value.isdigit():
                tick(min(99, int(value) / 1e6 / length * 100))
            elif line.strip() == "progress=end":
                finished = True
                break
            elif "=" not in line and line.strip():
                errors.append(line.strip())
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass  # the file is complete at progress=end, a slow connection close is not worth waiting for
    finally:
        proc.kill()  # also stops ffmpeg on Ctrl+C. Does nothing if it already exited.
    if not finished or not Path(out).is_file():
        Path(out).unlink(missing_ok=True)  # never leave a half-written clip behind
        raise RuntimeError(errors[0][:160] if errors else "ffmpeg stopped early")
    tick(100)


def has_sound(fmt):
    return fmt.get("acodec") not in (None, "none")


def section(url, start, end, res, out_for, net, tick, mode, info=None):
    """Fetch only the wanted section. `info` is an earlier resolve() result to reuse."""
    info = info or resolve(url, res, net)
    if end > (info.get("duration") or end) + TOLERANCE:
        raise Unfixable(f"END is past the end of the video, which is {info['duration']:g}s long")
    formats = info.get("requested_formats") or [info]
    streams = streams_for(mode, any(has_sound(f) for f in formats))
    if mode == "audio":
        formats = [f for f in formats if has_sound(f)]  # no need to fetch the picture
    out = out_for(info["title"])
    inputs = [(f["url"], remote(f)) for f in formats]
    # Fresh stream addresses answer 403 for the first second or two, so retry the open.
    for attempt in range(5):
        time.sleep(2)
        try:
            ffmpeg_cut(inputs, start, end - start, out, tick, streams, mode)
            return checked(out, end - start, streams)
        except RuntimeError as err:
            if "403" not in str(err) or attempt == 4:
                raise


def whole_then_cut(url, start, end, res, out_for, net, tick, mode):
    """Last resort: fetch the full video with yt-dlp's own downloader, cut it locally."""
    with tempfile.TemporaryDirectory() as tmp:
        full, title = download_full(url, res, net, tmp, tick)
        streams = streams_for(mode, "audio" in stream_lengths(full))
        out = out_for(title)
        ffmpeg_cut([(str(full), [])], start, end - start, out, tick, streams, mode)
        return checked(out, end - start, streams)


def download_full(url, res, net, folder, tick):
    """Download the whole video into `folder` with yt-dlp's own downloader. Returns (file, title)."""
    cmd = ytdlp(url, res, net, "--merge-output-format", "mp4", "--progress", "--newline",
                "--print", "after_move:%(title)s", "-o", str(Path(folder) / "full.%(ext)s"))
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace", **HIDDEN)
    title, problem, files_done = "clip", "full download failed", 0
    try:
        for line in proc.stdout:
            line = line.strip()
            percent = re.match(r"\[download\]\s+([\d.]+)%", line)
            if percent:
                # Video and audio arrive as two files. The video is nearly all of it, so the
                # bar follows the video and then waits at 99 for the audio and the merge.
                tick(99 if files_done else min(99, float(percent.group(1))))
                files_done += percent.group(1) == "100.0" or line.startswith("[download] 100%")
            elif line.startswith("ERROR"):
                problem = line[:200]
            elif line and not line.startswith(("[", "WARNING")):
                title = line
        proc.wait()
    finally:
        proc.kill()
    full = next(Path(folder).glob("full.*"), None)
    if proc.returncode != 0 or not full:
        raise RuntimeError(problem)
    return full, title


def stream_lengths(path):
    """{'video': seconds, 'audio': seconds} for the streams a file has."""
    probe = run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,duration", "-of", "json", str(path)])
    return {s["codec_type"]: float(s.get("duration") or 0) for s in json.loads(probe.stdout)["streams"]}


def verify(path, want, streams=("video", "audio")):
    """Return None if the file is a good clip of `want` seconds, else the reason it is not."""
    if not path.is_file() or path.stat().st_size < 10_000:
        return "no output file"
    try:
        lengths = stream_lengths(path)
    except (ValueError, KeyError):
        return "file is not readable"
    # Checked per stream: a dropped connection can cut the audio short while the video is whole.
    for kind in streams:
        if kind not in lengths:
            return f"no {kind} in the file"
        if abs(lengths[kind] - want) > TOLERANCE:
            return f"{kind} is {lengths[kind]:.2f}s long, expected {want:.2f}s"
    # Decode the whole clip. Catches corrupt or truncated downloads.
    if run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "null", "-"]).stderr.strip():
        return "file has decode errors"
    return None


def checked(out, want, streams):
    """Return `out` if it is a good clip, otherwise delete it and raise."""
    problem = verify(out, want, streams)
    if problem:
        out.unlink(missing_ok=True)
        raise RuntimeError(problem)
    return out


def try_routes(steps, say):
    """Run each (name, step) until one returns. Unfixable stops at once."""
    problem = "nothing was tried"
    for name, step in steps:
        say(f"{name}...")
        try:
            return step()
        except Unfixable:
            raise
        except Exception as err:  # whatever went wrong, the next route may still work
            problem = str(err) or type(err).__name__
            say(f"  did not work: {problem}")
    raise RuntimeError(f"Every route failed. Last problem: {problem}")


def namer(dest, mode, suffix=""):
    """Make the output folder and return a function that turns a video title into the output path."""
    dest = Path(dest).expanduser()
    is_file = dest.suffix.lower() in (".mp4", ".mp3")
    (dest.parent if is_file else dest).mkdir(parents=True, exist_ok=True)
    return lambda title: dest if is_file else dest / f"{safe_name(title)}{suffix}{extension(mode)}"


def make_clip(url, start, end, res, dest, say=print, tick=show_percent, info=None, mode="video"):
    """Make the clip and return its path. `dest` is a folder or a file path.

    Raises Unfixable for a bad request and RuntimeError when every route failed.
    """
    if end <= start:
        raise Unfixable("END must be after START")
    job = (url, start, end, res, namer(dest, mode, f" [{label(start)}-{label(end)}]"))
    return try_routes([
        ("Downloading just the clip", lambda: section(*job, IPV4, tick, mode, info)),
        ("Trying the other network route", lambda: section(*job, AUTO, tick, mode)),
        ("Updating yt-dlp and trying again", lambda: run(["yt-dlp", "-U"]) and section(*job, IPV4, tick, mode)),
        ("Downloading the full video and cutting it locally", lambda: whole_then_cut(*job, IPV4, tick, mode)),
    ], say)


def whole_video(url, res, dest, say=print, tick=show_percent, info=None, mode="video"):
    """Download the whole video and return its path. `dest` is a folder or a file path."""
    out_for = namer(dest, mode)
    if mode != "video":
        # These need a re-encode, which is the clip path with the clip being everything.
        info = info or lookup(url, res)
        job = (url, 0, info["duration"], res, out_for)
        return try_routes([
            ("Downloading the whole video", lambda: section(*job, IPV4, tick, mode, info)),
            ("Trying the other network route", lambda: section(*job, AUTO, tick, mode)),
            ("Downloading the full video and converting it", lambda: whole_then_cut(*job, IPV4, tick, mode)),
        ], say)

    def fetch(net):
        with tempfile.TemporaryDirectory() as tmp:
            full, title = download_full(url, res, net, tmp, tick)
            if not stream_lengths(full).get("video"):
                raise RuntimeError("the download has no video in it")
            out = out_for(title)
            shutil.move(full, out)
            tick(100)
            return out

    return try_routes([
        ("Downloading the whole video", lambda: fetch(IPV4)),
        ("Trying the other network route", lambda: fetch(AUTO)),
        ("Updating yt-dlp and trying again", lambda: run(["yt-dlp", "-U"]) and fetch(IPV4)),
    ], say)


def frame(info, at):
    """One small JPEG of the video at `at` seconds, for previews. `info` is a resolve() result."""
    video = (info.get("requested_formats") or [info])[0]
    cmd = ["ffmpeg", "-v", "error", *remote(video), "-ss", str(at), "-i", video["url"],
           "-frames:v", "1", "-vf", "scale=-2:360", "-c:v", "mjpeg", "-f", "image2", "pipe:1"]
    for _ in range(4):
        done = subprocess.run(cmd, capture_output=True, **HIDDEN)
        if done.stdout:
            return done.stdout
        if b"403" not in done.stderr:
            break
        time.sleep(2)  # fresh stream addresses answer 403 at first
    raise RuntimeError("Could not read that frame")


def main():
    ap = argparse.ArgumentParser(description="Download an exact clip from a YouTube video.")
    ap.add_argument("url")
    ap.add_argument("start", type=parse_time, nargs="?",
                    help="e.g. 90 or 1:30 or 1:02:03. Leave out START and END for the whole video")
    ap.add_argument("end", type=parse_time, nargs="?")
    ap.add_argument("-o", "--out", default=".", help="folder or file (default: current folder)")
    ap.add_argument("--res", type=int, default=1080, help="max height, e.g. 720 (default 1080)")
    shape = ap.add_mutually_exclusive_group()
    shape.add_argument("--vertical", action="store_const", dest="mode", const="vertical",
                       help="crop to 9:16 for Reels and Shorts")
    shape.add_argument("--audio", action="store_const", dest="mode", const="audio", help="save only the sound, as mp3")
    a = ap.parse_args()
    mode = a.mode or "video"
    try:
        if a.start is None:
            made = whole_video(a.url, a.res, a.out, mode=mode)
        elif a.end is None:
            ap.error("give both START and END, or neither to download the whole video")
        else:
            made = make_clip(a.url, a.start, a.end, a.res, a.out, mode=mode)
    except (Unfixable, RuntimeError) as err:
        print(f"Failed. {err}", file=sys.stderr)
        return 1
    print(f"Saved: {made.resolve()}")
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
    assert ytdlp("https://youtu.be/x", 720, IPV4, "-j")[-2:] == ["--", "https://youtu.be/x"]
    try:
        ytdlp("--exec calc", 720, IPV4)
    except Unfixable:
        pass
    else:
        raise AssertionError("an option was accepted as a link")
    assert streams_for("video", True) == ("video", "audio") and streams_for("vertical", False) == ("video",)
    assert streams_for("audio", True) == ("audio",) and extension("audio") == ".mp3"
    print("ok")


if __name__ == "__main__":
    sys.exit(self_test() if sys.argv[1:] == ["--self-test"] else main())
