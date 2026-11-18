#!/usr/bin/env python3
"""
ClipBot LIVE mode.

Runs on a schedule (GitHub Actions cron). Each run:
  1. Checks every streamer in streamers.json on Twitch / Kick / YouTube.
  2. If they are LIVE and the game matches (GTA 6 keywords), records ~N minutes.
  3. Finds the best moments, renders vertical captioned clips, uploads the best ones.

Upload pacing is automatic: the YouTube API quota (about 6 uploads/day by default)
is spread over the day instead of being burned in the first hour.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

import clipper as C

LIVE_STATE = Path("state/live.json")
CONFIG = Path("streamers.json")
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36"


# ---------- state ----------
def load_live_state():
    try:
        return json.loads(LIVE_STATE.read_text())
    except Exception:
        return {}


def save_live_state(s):
    LIVE_STATE.parent.mkdir(parents=True, exist_ok=True)
    LIVE_STATE.write_text(json.dumps(s, indent=2))


def quota_day():
    # YouTube quota resets at midnight Pacific time
    return datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d")


# ---------- game matching ----------
def matches(keywords, *texts):
    blob = " ".join(t or "" for t in texts).lower()
    return any(k.lower() in blob for k in keywords)


# ---------- live checks (each returns dict or None) ----------
_twitch_token = None


def twitch_headers():
    global _twitch_token
    cid = os.environ.get("TWITCH_CLIENT_ID")
    sec = os.environ.get("TWITCH_CLIENT_SECRET")
    if not cid or not sec:
        return None
    if _twitch_token is None:
        r = requests.post(
            "https://id.twitch.tv/oauth2/token",
            data={"client_id": cid, "client_secret": sec, "grant_type": "client_credentials"},
            timeout=20,
        )
        r.raise_for_status()
        _twitch_token = r.json()["access_token"]
    return {"Client-Id": cid, "Authorization": f"Bearer {_twitch_token}"}


def twitch_live(logins, keywords):
    """Batch check via Helix API. Returns {login: info} for live + matching."""
    out = {}
    if not logins:
        return out
    hdr = twitch_headers()
    if not hdr:
        print("Twitch secrets missing, skipping Twitch")
        return out
    for i in range(0, len(logins), 90):
        batch = logins[i:i + 90]
        r = requests.get(
            "https://api.twitch.tv/helix/streams",
            params=[("user_login", l) for l in batch] + [("first", 100)],
            headers=hdr,
            timeout=20,
        )
        r.raise_for_status()
        for s in r.json().get("data", []):
            if s.get("type") != "live":
                continue
            if matches(keywords, s.get("game_name"), s.get("title")):
                login = s["user_login"].lower()
                out[login] = {
                    "platform": "twitch",
                    "url": f"https://www.twitch.tv/{login}",
                    "title": s.get("title", ""),
                    "game": s.get("game_name", ""),
                }
    return out


def twitch_discover(disc, skip, last_clipped, want):
    """Pick live streamers by game + viewer range (for warm-up before launch).
    skip = names already chosen. Rotates: anyone clipped within cooldown_hours is passed over."""
    out = {}
    hdr = twitch_headers()
    if not hdr or want <= 0:
        return out
    lo = disc.get("min_viewers", 300)
    hi = disc.get("max_viewers", 20000)
    block = {b.lower() for b in disc.get("blocklist", [])}
    cooldown = disc.get("cooldown_hours", 6) * 3600
    cands = []
    for gname in disc.get("games", []):
        try:
            r = requests.get("https://api.twitch.tv/helix/games", params={"name": gname}, headers=hdr, timeout=20)
            data = r.json().get("data", []) if r.ok else []
            if not data:
                print(f"discover: game '{gname}' not on Twitch (yet)")
                continue
            r = requests.get(
                "https://api.twitch.tv/helix/streams",
                params={"game_id": data[0]["id"], "first": 100, "language": disc.get("language", "en")},
                headers=hdr, timeout=20,
            )
            r.raise_for_status()
            for s in r.json().get("data", []):
                login = s["user_login"].lower()
                if s.get("type") != "live" or login in block or login in skip:
                    continue
                if not (lo <= s.get("viewer_count", 0) <= hi):
                    continue
                if time.time() - last_clipped.get(login, 0) < cooldown:
                    continue
                cands.append((s.get("viewer_count", 0), login, s))
        except Exception as e:
            print(f"discover '{gname}' failed:", e)
    cands.sort(key=lambda x: -x[0])
    for _, login, s in cands:
        if login in out:
            continue
        out[login] = {
            "platform": "twitch",
            "url": f"https://www.twitch.tv/{login}",
            "title": s.get("title", ""),
            "game": s.get("game_name", ""),
        }
        if len(out) >= want:
            break
    return out


def kick_live(slug, keywords):
    try:
        r = requests.get(f"https://kick.com/api/v2/channels/{slug}", headers={"User-Agent": UA}, timeout=20)
        if r.status_code != 200:
            print(f"kick {slug}: HTTP {r.status_code} (Kick often blocks cloud IPs)")
            return None
        ls = r.json().get("livestream")
        if not ls:
            return None
        cats = " ".join(c.get("name", "") for c in ls.get("categories", []))
        if matches(keywords, cats, ls.get("session_title")):
            return {"platform": "kick", "url": f"https://kick.com/{slug}", "title": ls.get("session_title", "")}
    except Exception as e:
        print(f"kick {slug} check failed:", e)
    return None


def youtube_live(handle, keywords):
    handle = handle if handle.startswith("@") else "@" + handle
    url = f"https://www.youtube.com/{handle}/live"
    cmd = ["yt-dlp", "--dump-json", "--no-warnings", "--skip-download", url]
    if Path("cookies.txt").exists():
        cmd[1:1] = ["--cookies", "cookies.txt"]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if p.returncode != 0 or not p.stdout.strip():
            return None
        info = json.loads(p.stdout.splitlines()[0])
        if not info.get("is_live"):
            return None
        if matches(keywords, info.get("title"), info.get("description", "")[:300], " ".join(info.get("tags") or [])):
            return {"platform": "youtube", "url": info.get("webpage_url") or url, "title": info.get("title", "")}
    except Exception as e:
        print(f"youtube {handle} check failed:", e)
    return None


def norm_streamers(items):
    """Accept plain names ("Lacy", "Dasgasd3m (Dom)") or full dicts. Name -> Twitch login."""
    out = []
    for s in items:
        if isinstance(s, str):
            n = re.sub(r"\(.*?\)", "", s).strip().lower().replace(" ", "")
            if n:
                out.append({"name": n, "twitch": n})
        elif isinstance(s, dict) and s.get("name"):
            out.append(s)
    return out


def warn_unknown_logins(logins):
    """Print which names are not real Twitch channels (typo / wrong login)."""
    hdr = twitch_headers()
    if not hdr or not logins:
        return
    try:
        r = requests.get("https://api.twitch.tv/helix/users", params=[("login", l) for l in logins[:100]],
                         headers=hdr, timeout=20)
        known = {u["login"].lower() for u in r.json().get("data", [])}
        bad = [l for l in logins if l.lower() not in known]
        if bad:
            print("WARNING: not found on Twitch (check spelling, use the name in twitch.tv/<name>):", bad)
    except Exception as e:
        print("login check failed:", e)


def find_live(cfg, state=None, max_streams=4):
    keywords = cfg.get("game_keywords", ["gta 6"])
    streamers = norm_streamers(cfg.get("streamers", []))
    twitch_map = {s["twitch"].lower(): s["name"] for s in streamers if s.get("twitch")}
    warn_unknown_logins(list(twitch_map))
    live = {}
    try:
        for login, info in twitch_live(list(twitch_map), keywords).items():
            live[twitch_map[login]] = info
    except Exception as e:
        print("twitch check failed:", e)
    for s in streamers:
        if s["name"] in live:
            continue
        info = None
        if s.get("kick"):
            info = kick_live(s["kick"], keywords)
        if not info and s.get("youtube"):
            info = youtube_live(s["youtube"], keywords)
        if info:
            live[s["name"]] = info
    disc = cfg.get("discover", {})
    if disc.get("enabled") and len(live) < max_streams:
        found = twitch_discover(
            disc, {n.lower() for n in live}, (state or {}).get("streamer_last", {}), max_streams - len(live)
        )
        for n, info in found.items():
            print(f"discovered: {n} ({info.get('game')})")
            live[n] = info
    return live


# ---------- recording ----------
def start_recording(name, info, minutes):
    C.WORK.mkdir(exist_ok=True)
    out = C.WORK / f"live_{name}.ts"
    if out.exists():
        out.unlink()
    secs = minutes * 60
    hh, rem = divmod(secs, 3600)
    mm, ss = divmod(rem, 60)
    cmd = [
        "streamlink", info["url"], "720p60,720p,best",
        "-o", str(out),
        "--hls-duration", f"{hh:02d}:{mm:02d}:{ss:02d}",
        "--retry-streams", "5", "--retry-max", "3",
        "--stream-timeout", "120",
    ]
    if info["platform"] == "twitch":
        cmd += ["--twitch-disable-ads"]
    print("+", " ".join(cmd), flush=True)
    log = open(C.WORK / f"live_{name}.log", "w")
    proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT)
    return proc, out


# ---------- main ----------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--record-minutes", type=int, default=int(os.environ.get("RECORD_MINUTES", "20")))
    ap.add_argument("--clip-seconds", type=int, default=int(os.environ.get("CLIP_SECONDS", "40")))
    ap.add_argument("--daily-cap", type=int, default=int(os.environ.get("DAILY_CAP", "6")))
    ap.add_argument("--max-per-run", type=int, default=int(os.environ.get("MAX_PER_RUN", "2")))
    ap.add_argument("--min-score", type=float, default=float(os.environ.get("MIN_SCORE", "1.2")))
    ap.add_argument("--max-streams", type=int, default=int(os.environ.get("MAX_STREAMS", "4")))
    ap.add_argument("--privacy", default=os.environ.get("UPLOAD_PRIVACY", "private"))
    ap.add_argument("--topic", default=os.environ.get("TOPIC", "GTA 6"))
    ap.add_argument("--captions", default=os.environ.get("CAPTIONS", "true"))
    ap.add_argument("--schedule", default=os.environ.get("AUTO_SCHEDULE", "true"),
                    help="true = upload now but publish at spaced-out times")
    ap.add_argument("--schedule-gap-hours", type=float, default=float(os.environ.get("SCHEDULE_GAP_HOURS", "0")),
                    help="hours between scheduled publishes (0 = 24 / daily cap, e.g. 4h at 6 per day)")
    ap.add_argument("--lead-minutes", type=int, default=int(os.environ.get("SCHEDULE_LEAD_MINUTES", "30")))
    ap.add_argument("--dry-run", action="store_true", help="detect + record + render, no upload")
    a = ap.parse_args()

    if os.environ.get("AUTO_RUN", "true").lower() == "false" and os.environ.get("GITHUB_EVENT_NAME") == "schedule":
        print("AUTO_RUN is false: automatic runs are paused. (Manual runs still work.)")
        return
    use_schedule = str(a.schedule).lower() == "true"
    gap = int((a.schedule_gap_hours or 24 / max(a.daily_cap, 1)) * 60)  # minutes

    cfg = json.loads(CONFIG.read_text())
    state = load_live_state()
    today = quota_day()
    uploads = state.setdefault("uploads", {})
    uploads = {today: uploads.get(today, 0)}
    state["uploads"] = uploads
    state.setdefault("streamer_last", {})

    remaining = a.daily_cap - uploads[today]
    min_gap = max(0, (24 * 60 / max(a.daily_cap, 1)) - 15)  # minutes between uploads
    last_up = state.get("last_upload_ts", 0)
    since = (time.time() - last_up) / 60
    if not a.dry_run:
        if remaining <= 0:
            print(f"Daily upload cap reached ({a.daily_cap}). Skipping.")
            return
        if not use_schedule and since < min_gap:
            print(f"Pacing: {since:.0f} min since last upload, need {min_gap:.0f}. Skipping.")
            return

    live = find_live(cfg, state, a.max_streams)
    if not live:
        print("Nobody live and playing the game right now.")
        return
    names = list(live)[: a.max_streams]
    print("LIVE + playing:", names)
    C.tg(f"ClipBot: recording {', '.join(names)} ({a.record_minutes} min)")

    procs = {n: start_recording(n, live[n], a.record_minutes) for n in names}
    deadline = time.time() + a.record_minutes * 60 + 180
    for n, (p, _) in procs.items():
        try:
            p.wait(timeout=max(5, deadline - time.time()))
        except subprocess.TimeoutExpired:
            p.terminate()
            try:
                p.wait(timeout=15)
            except Exception:
                p.kill()

    # find candidates across all streamers
    cands = []
    for n, (_, path) in procs.items():
        if not path.exists() or path.stat().st_size < 2_000_000:
            print(f"{n}: nothing recorded")
            continue
        try:
            ms = C.find_moments(path, a.clip_seconds, 3, [], with_scores=True)
        except Exception as e:
            print(f"{n}: analysis failed: {e}")
            continue
        for s, e, sc in ms:
            cands.append({"name": n, "path": path, "s": s, "e": e, "score": sc})
    cands = [c for c in cands if c["score"] >= a.min_score]
    cands.sort(key=lambda c: -c["score"])
    print("candidates:", [(c["name"], c["s"], round(c["score"], 2)) for c in cands])

    limit = a.max_per_run if a.dry_run else min(a.max_per_run, remaining)
    picked, seen = [], set()
    for c in cands:  # at most one clip per streamer per run, so the channel stays varied
        if c["name"] in seen:
            continue
        seen.add(c["name"])
        picked.append(c)
        if len(picked) >= limit:
            break

    if not picked:
        C.tg("ClipBot: recorded but nothing hyped enough to clip.")
        return

    yt = None if a.dry_run else C.youtube_client()
    use_caps = str(a.captions).lower() == "true"
    results = []
    stamp = datetime.now(timezone.utc).strftime("%m%d%H%M")
    for i, c in enumerate(picked, 1):
        name, info = c["name"], live[c["name"]]
        d = c["e"] - c["s"]
        tag = f"{name}_{stamp}_{i}"
        src_name = c["path"].name
        srt, text = C.make_srt(src_name, c["s"], d, tag) if use_caps else (None, "")
        try:
            out = C.render(src_name, c["s"], d, srt, f"{tag}.mp4")
        except Exception as e:
            results.append(f"{name}: render failed: {e}")
            continue
        if a.dry_run:
            results.append(f"{name}: rendered {out} (score {c['score']:.1f})")
            continue
        game = info.get("game") or a.topic
        title = C.make_title(text, name, game)
        gtag = re.sub(r"[^A-Za-z0-9]", "", game)
        desc = (
            f"Live moment from {name}'s stream. Go watch and follow them: {info['url']}\n\n"
            f"#{gtag} #shorts #gaming #{re.sub(r'[^A-Za-z0-9]', '', name)}"
            + C.affiliate_block()
        )
        tags = [game, name, "shorts", "gaming", "clips", "live"]
        try:
            publish_at, when = None, "now"
            if use_schedule:
                slot = max(time.time() + a.lead_minutes * 60, state.get("last_publish_ts", 0) + gap * 60)
                state["last_publish_ts"] = slot
                dt = datetime.fromtimestamp(slot, timezone.utc)
                publish_at = dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")
                when = dt.astimezone(ZoneInfo("Africa/Johannesburg")).strftime("%a %H:%M SAST")
            vid = C.upload(yt, out, title, desc, tags, a.privacy, publish_at)
            uploads[today] += 1
            state["last_upload_ts"] = time.time()
            state["streamer_last"][name] = time.time()
            results.append(f"https://youtu.be/{vid}  ({title})  publishes: {when}")
        except Exception as e:
            results.append(f"{name}: upload failed: {e}")
            if "quotaExceeded" in str(e):
                uploads[today] = a.daily_cap
                break
        save_live_state(state)

    save_live_state(state)
    C.tg("ClipBot live:\n" + "\n".join(results))
    print("\n".join(results))


if __name__ == "__main__":
    sys.exit(main())
