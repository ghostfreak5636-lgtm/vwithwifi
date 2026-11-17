# ClipBot

Give it a streamer VOD link. It finds the loudest/funniest moments, cuts them into
vertical 9:16 clips with captions, and uploads them to your clips channel as Shorts.
Runs entirely on GitHub Actions, so nothing runs on your phone.

## Setup (once)

1. **Repo**: create a new GitHub repo (e.g. `clipbot`) and add these files.
   The workflow file must be at `.github/workflows/clips.yml`
   (on GitHub: Add file > Create new file, type that full path as the name, paste the contents).
2. **Google Cloud**: console.cloud.google.com
   - Enable **YouTube Data API v3**.
   - OAuth consent screen: set publishing status to **In production**
     (in "Testing" mode your refresh token dies after 7 days).
   - Credentials > Create OAuth client ID > type **Desktop app**.
     Copy the Client ID and Client Secret.
3. **Refresh token**: run `get_refresh_token.py` (Pydroid works). Log in and pick the
   **new clips channel** when Google asks. It prints your three secrets.
4. **GitHub secrets** (Settings > Secrets and variables > Actions):
   - `YT_CLIENT_ID`, `YT_CLIENT_SECRET`, `YT_REFRESH_TOKEN`
   - optional: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` (results get sent to you)
   - optional: `YT_COOKIES` (Netscape cookies.txt text, only if YouTube VODs get blocked)

## Use

Actions tab > **ClipBot** > Run workflow. Fill in the VOD URL and streamer name.
Defaults: scans 60 minutes, makes 5 clips of 40s, uploads as **private**.
Set `gap_minutes` (e.g. 30) to schedule clips apart instead.

## Live mode (auto-clip streamers while they play GTA 6)

Workflow: `.github/workflows/live.yml`. It runs every 30 minutes by itself.
Each run checks your streamer list; for anyone who is **live and playing the game**
it records ~20 minutes, picks the hypest moment, renders a vertical captioned clip
and uploads it. Nobody live = it exits in seconds.

1. Edit `streamers.json`: one entry per streamer with their channel names
   (`twitch`, `kick`, `youtube` handle; leave blank what they don't use).
   `game_keywords` is matched against the Twitch category/stream title (Kick and
   YouTube match on title/category/tags).
2. Create a free Twitch app at dev.twitch.tv/console (any redirect URL, e.g. http://localhost)
   and add secrets `TWITCH_CLIENT_ID` and `TWITCH_CLIENT_SECRET`.
3. Test first: Actions > **ClipBot Live** > Run workflow > tick **dry_run**.
   It renders clips without uploading (download them from the run's Artifacts).
4. Optional repo **Variables** (Settings > Secrets and variables > Actions > Variables):
   `UPLOAD_PRIVACY` (private/unlisted/public, default private), `DAILY_CAP` (default 6),
   `RECORD_MINUTES` (20), `MAX_PER_RUN` (2), `MIN_SCORE` (1.2, lower = more clips),
   `CLIP_SECONDS` (40), `TOPIC`.

Limits you should know about:
- **Quota is the bottleneck.** YouTube allows ~6 uploads/day per project by default.
  The bot spaces uploads evenly (about every 3h45m at 6/day) and uploads only the best
  moment it saw. To upload more, request a quota increase through the YouTube API
  Services audit form, then raise `DAILY_CAP`.
- **GitHub minutes.** Private repos get 2,000 free minutes/month, which two weeks of
  runs will exceed. Public repos are unlimited. Keep the repo public (secrets stay hidden).
- It records in 20-minute windows every 30 minutes, so some stream time is skipped.
- Kick blocks many cloud IPs, so Kick/YouTube detection can be unreliable. Twitch is solid.
- Scheduled runs can start a few minutes late, and GitHub pauses cron on repos with no
  activity for 60 days (not an issue for 2 weeks).

## Notes

- Twitch VOD links work best from GitHub's servers. YouTube links may need `YT_COOKIES`.
- Default YouTube API quota is about 6 uploads per day per Google Cloud project
  (each upload costs 1,600 of 10,000 units). If you reuse the same project as
  VODYANITSA, they share that quota.
- Uploads from an unaudited API project are locked to private until the project passes
  YouTube's API audit.
- Only clip streamers who allow it. The description credits the streamer and links the original.
- Clips already made from a VOD are tracked in `state/processed.json` so re-runs pick new moments.
