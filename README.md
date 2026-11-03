# OpusMusicAPI

Public YouTube **audio** API + Telegram **key-manager bot**, built to run on free hosting.

* **API** (`OpusApi/`, FastAPI): API-key auth, 200 requests/day per user, 28-day keys, yt-dlp engine with strict concurrency limits.
* **Cache**: songs are archived in a **private Telegram channel**; MongoDB stores message IDs and metadata. The local disk cache handles repeat requests quickly.
* **Users/keys/usage**: **MongoDB** (nothing important lives in memory or on disk).
* **Bot** (`OpusBot/`, Pyrogram): `/start` → Generate API → join channels → key card (Revoke / Renew / Report Bug). Owner tools: `/broadcast`, `/users`, `/revoke`, `/setlimit`.

## Quick start

```bash
cp .env.example .env        # fill it in
pip install -r requirements.txt
uvicorn OpusApi.main:app --port 8000      # API
python -m OpusBot                         # key-manager bot (separate process)
python -m tests.test_core                 # offline tests
```
Or set `RUN_BOT_WITH_API=true` to run the bot inside the API process (one free service; needs more RAM).
Requires `ffmpeg`, `yt-dlp` and `deno` on the server (the Dockerfile installs them).

## Using the API

```
GET {BASE_URL}/v1/download?url=<video id or link>&type=audio     Header: X-API-Key: <key>
  -> {"download_token": "...", "expires_in": 300}
GET {BASE_URL}/v1/stream/<video_id>?token=<download_token>
  -> the audio file (headers X-Requests-Remaining, X-Daily-Limit)
GET {BASE_URL}/v1/me                                             Header: X-API-Key
```
Paths without `/v1` still work. Errors are JSON: `{"status":"error","code":"daily_limit","message":"..."}`
(codes: `invalid_key`, `key_expired`, `rate_limited`, `daily_limit`, `busy`, `download_failed`, ...).

* One request is charged when `/stream` starts and refunded if the download fails or the server is busy.
* The quota resets at midnight in `RESET_TZ` (default Asia/Kolkata).
* `GET /stats` (needs `ADMIN_KEY`) shows queue, disk/Telegram cache hits, Telegram archive usage, and proxy health.

## Important notes

1. **Telegram cache setup**: create a private channel used only for cached audio. Add the Telegram account represented by `TELEGRAM_SESSION` as an administrator with permission to post messages. Set `TELEGRAM_CACHE_CHANNEL` to its numeric channel ID (usually starts with `-100`).
2. **Session security**: `TELEGRAM_SESSION` is a powerful user login credential. Keep it secret, store it only in hosting environment variables, and never commit it to GitHub. Generate it with `python scripts/generate_telegram_session.py` after setting `API_ID` and `API_HASH`. The session account must be able to access the private channel.
3. **Back up `KEY_ENC_SECRET`.** If it is lost, existing keys still work but the bot cannot display them (users must Revoke → Generate New).
4. MongoDB Atlas free: allow `0.0.0.0/0` in Network Access (Render IPs change) and use a strong DB password.
5. Rotate any proxy credentials that were ever committed in the old version.
6. Free-tier safety knobs: `MAX_CONCURRENT_DOWNLOADS`, `MAX_QUEUE`, `DOWNLOAD_TIMEOUT`, `RATE_PER_MIN`, `ALLOW_VIDEO`.
7. Bot setup: add the bot as **admin** in every `FORCE_CHANNELS` channel (needed to check membership). Texts are in `OpusBot/en.json`.
