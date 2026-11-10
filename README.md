# ytclip

Download an exact clip from a YouTube video without downloading the whole video.

```bash
python clip.py "https://youtu.be/pf5tXJZM1Rw" 2:00 2:30
```

A 30 second 1080p clip takes about 15 seconds. The file is H.264 + AAC in an `.mp4`, so it opens in Premiere, DaVinci Resolve, CapCut, WhatsApp and everything else.

## Why this exists

`yt-dlp --download-sections` looks like it does this already, but in practice it is unpredictable:

| Problem | What you see | What ytclip does |
|---|---|---|
| Cuts land on the nearest keyframe | Clip starts up to several seconds early, or opens with a frozen frame | Re-encodes the section so the cut is frame exact |
| Output is often VP9 or AV1 | Editors refuse the file or show a black video | Always writes H.264 + AAC |
| YouTube throttles ffmpeg to about 50 KB/s | A 30 second clip takes 4 minutes | Requests the stream in 5 MB pieces, which is not throttled |
| New stream links answer 403 for a second or two | Random "403 Forbidden" failures | Waits and retries the open |
| YouTube bot-blocks one of your two network addresses | "Sign in to confirm you're not a bot" | Tries IPv4 first, then the other route |
| Silent bad output | A file with no audio, or the wrong length | Checks every clip with ffprobe before calling it done |

## Install

You need three things on your PATH:

- [Python](https://www.python.org/downloads/) 3.9 or newer
- [yt-dlp](https://github.com/yt-dlp/yt-dlp#installation)
- [ffmpeg](https://ffmpeg.org/download.html) from 2025 or newer (it must include `ffprobe`)

On Windows:

```bash
winget install yt-dlp.yt-dlp
```

That one command installs yt-dlp and a matching ffmpeg. Then get this repo:

```bash
git clone https://github.com/Arin117Kaushik/ytclip.git
```

There is nothing else to install. The script uses only the Python standard library.

## Use

```
python clip.py URL START END [-o FOLDER_OR_FILE] [--res HEIGHT]
```

| Argument | Meaning | Example |
|---|---|---|
| `URL` | Any YouTube video link | `"https://youtu.be/pf5tXJZM1Rw"` |
| `START`, `END` | Seconds, `m:ss` or `h:mm:ss`, decimals allowed | `90`, `1:30`, `1:02:03.5` |
| `-o` | Folder to save into, or a full `.mp4` filename. Default is the current folder | `-o D:\Clips` or `-o intro.mp4` |
| `--res` | Maximum height. Default is `1080` | `--res 720` |

Put the URL in quotes. Without them the shell breaks links that contain `&`.

Examples:

```bash
python clip.py "https://youtu.be/pf5tXJZM1Rw" 2:00 2:30
```

```bash
python clip.py "https://youtu.be/pf5tXJZM1Rw" 0 12.5 --res 720 -o intro.mp4
```

When you give a folder, the file is named after the video and the timestamps, for example `Video title [2m00s-2m30s].mp4`.

On Windows you can also run `ytclip.cmd` the same way, which saves typing `python clip.py`.

## How it works

1. `yt-dlp` resolves the video's stream addresses. That is one small request and no video data.
2. `ffmpeg` opens those streams, seeks straight to `START`, reads only the bytes for the clip and re-encodes them.
3. `ffprobe` checks the result has video, has audio, is the right length and decodes cleanly.

If a step fails, the next route is tried automatically:

1. Clip over IPv4
2. Clip over the default network route
3. Update yt-dlp, then clip again
4. Download the full video with yt-dlp's own downloader and cut it locally

Route 4 is slow but nearly always works, so you still get your clip.

## Troubleshooting

**Every route fails with "Sign in to confirm you're not a bot".** YouTube has flagged your connection, usually after many downloads in a short time. Wait an hour, or switch network (a phone hotspot works).

**"Unrecognized option 'request_size'".** Your ffmpeg is too old. Update it. The clip will still download through route 4 in the meantime.

**It worked yesterday and fails today.** YouTube changes things often. Run `yt-dlp -U`. The script also does this for you on route 3.

## Test

```bash
python clip.py --self-test
```

## Notes

Only download videos you have the right to use. Clips are re-encoded at CRF 18, which is visually lossless for normal use.

MIT licensed.
