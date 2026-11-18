#!/usr/bin/env python3
"""
ClipBot: streamer VOD -> best moments -> vertical captioned clips -> YouTube Shorts.

Moment detection = loudest / most sudden audio spikes (hype, screaming, laughing).
"""
import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import requests

WORK = Path("work")
OUT = Path("out")
STATE = Path("state/processed.json")


# ---------- helpers ----------
def sh(cmd, **kw):
    print("+", " ".join(str(c) for c in cmd), flush=True)
    return subprocess.run(cmd, check=True, **kw)


def parse_ts(s):
    s = str(s).strip()
    if not s:
        return 0
    sec = 0.0
    for p in s.split(":"):
        sec = sec * 60 + float(p)
    return int(sec)


def tg(msg):
    tok = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not tok or not chat:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{tok}/sendMessage",
            data={"chat_id": chat, "text": msg[:4000], "disable_web_page_preview": True},
            timeout=20,
        )
    except Exception as e:
        print("telegram failed:", e)


def load_state():
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {}


def save_state(state):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, indent=2))



# ---------- affiliate block ----------
def affiliate_block():
    """Description footer with one affiliate link + a clear disclosure.
    Pick the link with AFFILIATE_KEY (a key from affiliates.json, default 'default').
    Turn off with AFFILIATE=false."""
    if os.environ.get("AFFILIATE", "true").lower() == "false":
        return ""
    try:
        links = json.loads(Path("affiliates.json").read_text())
    except Exception:
        return ""
    key = os.environ.get("AFFILIATE_KEY", "default")
    link = links.get(key) or links.get("default")
    if not link:
        return ""
    return f"\n\n(Affiliate link, I may earn a commission) {link}"


# ---------- 1. download ----------
def download(url, start, minutes):
    WORK.mkdir(exist_ok=True)
    for f in WORK.glob("source*"):
        f.unlink()
    cmd = [
        "yt-dlp",
        "-f", "bv*[height<=720]+ba/b[height<=720]/b",
        "--merge-output-format", "mp4",
        "-o", str(WORK / "source.%(ext)s"),
        "--no-playlist",
        "--retries", "5",
        "--fragment-retries", "10",
    ]
    if minutes > 0:
        cmd += ["--download-sections", f"*{start}-{start + minutes * 60}"]
    if Path("cookies.txt").exists():
        cmd += ["--cookies", "cookies.txt"]
    cmd.append(url)
    sh(cmd)
    files = sorted(WORK.glob("source*.mp4"))
    if not files:
        raise RuntimeError("download produced no mp4")
    if files[0].name != "source.mp4":
        files[0].rename(WORK / "source.mp4")
    return WORK / "source.mp4"


# ---------- 2. find moments ----------
def find_moments(src, clip_len, n, taken, with_scores=False):
    cmd = ["ffmpeg", "-v", "error", "-i", str(src), "-vn", "-ac", "1", "-ar", "16000", "-f", "s16le", "-"]
    raw = subprocess.run(cmd, check=True, stdout=subprocess.PIPE).stdout
    sr = 16000
    audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    secs = len(audio) // sr
    if secs < clip_len:
        if secs <= 5:
            return []
        return [(0, secs, 0.0)] if with_scores else [(0, secs)]
    rms = np.sqrt((audio[: secs * sr].reshape(secs, sr) ** 2).mean(axis=1)) + 1e-9
    db = 20 * np.log10(rms)
    smooth = np.convolve(db, np.ones(3) / 3, mode="same")
    baseline = np.convolve(db, np.ones(61) / 61, mode="same")
    score = smooth - baseline
    z = (score - score.mean()) / (score.std() + 1e-9)
    w = max(3, int(clip_len // 4))
    energy = np.convolve(z, np.ones(w) / w, mode="same")

    chosen, scores = [], []
    for t in np.argsort(-energy):
        s = max(0, int(t) - int(clip_len * 0.6))
        e = s + clip_len
        if e > secs:
            e = secs
            s = max(0, e - clip_len)
        if e - s < 10:
            continue
        if any(s < b and e > a for a, b in chosen + taken):
            continue
        chosen.append((s, e))
        scores.append(float(energy[t]))
        if len(chosen) >= n:
            break
    if with_scores:
        return [(s, e, sc) for (s, e), sc in zip(chosen, scores)]
    return chosen


# ---------- 3. captions ----------
_model = None


def make_srt(src_name, s, d, idx):
    """Returns (srt filename or None, transcript text)."""
    global _model
    try:
        from faster_whisper import WhisperModel

        if _model is None:
            _model = WhisperModel("base.en", device="cpu", compute_type="int8")
        wav = f"clip{idx}.wav"
        sh(["ffmpeg", "-y", "-v", "error", "-ss", str(s), "-t", str(d), "-i", src_name,
            "-vn", "-ac", "1", "-ar", "16000", wav], cwd=WORK)
        segments, _ = _model.transcribe(str(WORK / wav), word_timestamps=True, vad_filter=True)
        words = []
        for seg in segments:
            for w in (seg.words or []):
                words.append((w.start, w.end, w.word.strip()))
        if not words:
            return None, ""

        def ts(x):
            h, rem = divmod(x, 3600)
            m, sec = divmod(rem, 60)
            return f"{int(h):02d}:{int(m):02d}:{int(sec):02d},{int((sec % 1) * 1000):03d}"

        lines = []
        for i in range(0, len(words), 3):
            chunk = words[i:i + 3]
            lines.append(f"{len(lines) + 1}\n{ts(chunk[0][0])} --> {ts(chunk[-1][1])}\n"
                         f"{' '.join(c[2] for c in chunk).upper()}\n")
        name = f"clip{idx}.srt"
        (WORK / name).write_text("\n".join(lines), encoding="utf-8")
        return name, " ".join(w[2] for w in words)
    except Exception as e:
        print("captions skipped:", e)
        return None, ""


# ---------- 4. render ----------
def render(src_name, s, d, srt, out_name):
    fc = (
        "[0:v]split=2[a][b];"
        "[a]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=25:5[bg];"
        "[b]scale=1080:-2[fg];"
        "[bg][fg]overlay=(W-w)/2:(H-h)/2"
    )
    if srt:
        style = "FontName=DejaVu Sans,FontSize=10,Bold=1,Alignment=2,MarginV=55,Outline=2,Shadow=0"
        fc += f",subtitles={srt}:force_style='{style}'"
    fc += "[v]"
    sh([
        "ffmpeg", "-y", "-v", "error",
        "-ss", str(s), "-t", str(d), "-i", src_name,
        "-filter_complex", fc,
        "-map", "[v]", "-map", "0:a?",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "160k", "-r", "30", "-movflags", "+faststart",
        out_name,
    ], cwd=WORK)
    OUT.mkdir(exist_ok=True)
    final = OUT / out_name
    (WORK / out_name).replace(final)
    return final


# ---------- 5. upload ----------
def youtube_client():
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    creds = Credentials(
        None,
        refresh_token=os.environ["YT_REFRESH_TOKEN"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ["YT_CLIENT_ID"],
        client_secret=os.environ["YT_CLIENT_SECRET"],
        scopes=["https://www.googleapis.com/auth/youtube.upload"],
    )
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def upload(yt, path, title, desc, tags, privacy, publish_at):
    from googleapiclient.http import MediaFileUpload

    status = {"privacyStatus": "private" if publish_at else privacy, "selfDeclaredMadeForKids": False}
    if publish_at:
        status["publishAt"] = publish_at
    body = {
        "snippet": {"title": title[:100], "description": desc[:4900], "tags": tags, "categoryId": "20"},
        "status": status,
    }
    media = MediaFileUpload(str(path), mimetype="video/mp4", chunksize=8 * 1024 * 1024, resumable=True)
    req = yt.videos().insert(part="snippet,status", body=body, media_body=media)
    resp = None
    while resp is None:
        _, resp = req.next_chunk()
    return resp["id"]


def make_title(hook, streamer, topic):
    hook = re.sub(r"[^\w\s'!?,.-]", "", hook or "")
    hook = re.sub(r"\s+", " ", hook).strip(" .,-")
    words = hook.split()
    hook = " ".join(words[:7]) if len(words) >= 3 else f"Wild {topic} moment"
    return f"{hook} | {streamer} #shorts"[:100]


# ---------- main ----------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--streamer", required=True)
    ap.add_argument("--topic", default="GTA 6")
    ap.add_argument("--start", default="0")
    ap.add_argument("--minutes", type=int, default=60)
    ap.add_argument("--clips", type=int, default=5)
    ap.add_argument("--clip-seconds", type=int, default=40)
    ap.add_argument("--privacy", default="private")
    ap.add_argument("--gap-minutes", type=int, default=0)
    ap.add_argument("--captions", default="true")
    ap.add_argument("--no-upload", action="store_true")
    a = ap.parse_args()

    start = parse_ts(a.start)
    use_captions = str(a.captions).lower() == "true"
    state = load_state()
    st = state.setdefault(a.url, {"windows": [], "videos": []})

    tg(f"ClipBot started: {a.streamer} / {a.topic}")
    try:
        src = download(a.url, start, a.minutes)
        taken = [(x - start, y - start) for x, y in st["windows"]]
        moments = find_moments(src, a.clip_seconds, a.clips, taken)
        if not moments:
            raise RuntimeError("no moments found")
        print("moments:", moments)

        yt = None if a.no_upload else youtube_client()
        now = datetime.now(timezone.utc)
        done = []

        for i, (s, e) in enumerate(moments, 1):
            d = e - s
            srt, text = make_srt("source.mp4", s, d, i) if use_captions else (None, "")
            path = render("source.mp4", s, d, srt, f"clip{i}.mp4")
            st["windows"].append([s + start, e + start])
            if a.no_upload:
                done.append(f"clip{i}: rendered only")
                continue

            title = make_title(text, a.streamer, a.topic)
            desc = (
                f"Clip from {a.streamer}'s stream. Go support them!\n"
                f"Original: {a.url}\n\n#{a.topic.replace(' ', '')} #shorts #gaming"
                + affiliate_block()
            )
            tags = [a.topic, a.streamer, "shorts", "gaming", "clips"]
            publish_at = None
            if a.gap_minutes > 0:
                t = now + timedelta(minutes=a.gap_minutes * i)
                publish_at = t.strftime("%Y-%m-%dT%H:%M:%S.000Z")
            try:
                vid = upload(yt, path, title, desc, tags, a.privacy, publish_at)
                st["videos"].append(vid)
                done.append(f"https://youtu.be/{vid}  ({title})")
            except Exception as ex:
                done.append(f"clip{i} upload failed: {ex}")
                if "quotaExceeded" in str(ex):
                    break
            save_state(state)

        save_state(state)
        tg("ClipBot done:\n" + "\n".join(done))
        print("\n".join(done))
    except Exception as ex:
        save_state(state)
        tg(f"ClipBot failed: {ex}")
        raise


if __name__ == "__main__":
    sys.exit(main())
