# OpusMusicAPI: apne bot me API kaise use karein

Ye guide un developers ke liye hai jo apne **music bot** me is API se gaana mangwana chahte hain.

> Base URL aur API key apko manager bot (`/start` → Generate API) se milti hai.
> Bot me `/docs` bhejoge to chhota version wahi dikhega, aur `/test gaane ka naam` se turant check kar sakte ho.

---

## 1. Basic flow (3 step)

Har request me header lagao: `X-API-Key: <apki key>`

| Step | Request | Kya milta hai | Quota |
|---|---|---|---|
| 1. Search (optional) | `GET /v1/search?q=gaane ka naam` | `video_id`, `title`, `duration`, `uploader` | Free (sirf rate limit) |
| 2. Token | `GET /v1/download?url=VIDEO_ID&type=audio` | `download_token` (5 min valid) | Free |
| 3. Audio | `GET /v1/stream/VIDEO_ID` + header `X-Download-Token: <token>` | Audio file | **1 request** |

- `url` me video id ya poora YouTube link dono chalta hai.
- **Quota sirf step 3 me kat-ta hai.** Download fail ho ya server busy ho to quota wapas mil jaata hai.
- Ek step me chahiye to: `GET /download?url=VIDEO_ID&type=audio&api_key=KEY` seedha audio file de deta hai (purane bots ke liye).
- Apna usage dekhne ke liye: `GET /v1/me`

## 2. curl se test

```bash
BASE=https://your-api.onrender.com
KEY=opus_xxxxxxxxxxxxxxxxxxxxxxxx

# search
curl -s -H "X-API-Key: $KEY" "$BASE/v1/search?q=kesariya"

# token
curl -s -H "X-API-Key: $KEY" "$BASE/v1/download?url=dQw4w9WgXcQ&type=audio"

# audio (token upar se copy karo)
curl -H "X-Download-Token: TOKEN" "$BASE/v1/stream/dQw4w9WgXcQ" -o song.mp3 -D -
```

`-D -` se response headers dikhte hain:

| Header | Matlab |
|---|---|
| `X-Requests-Remaining` | Aaj ke kitne request bache (`unlimited` = master key) |
| `X-Daily-Limit` | Aapki daily limit |
| `X-Source` | `disk` / `telegram-stream` = cache se (fast), `download` = fresh download |

## 3. Python example (aiohttp)

```python
import aiohttp

BASE = "https://your-api.onrender.com"
KEY = "opus_xxxxxxxxxxxxxxxxxxxxxxxx"


class ApiError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


async def _json(sess, path, **params):
    async with sess.get(BASE + path, params=params) as r:
        data = await r.json(content_type=None)
        if r.status != 200:
            raise ApiError(data.get("code", "error"), data.get("message", "failed"))
        return data


async def fetch_song(name_or_id: str, out_path: str):
    """Gaana out_path me save karta hai. (video_id, title) return karta hai."""
    async with aiohttp.ClientSession(
        headers={"X-API-Key": KEY}, timeout=aiohttp.ClientTimeout(total=240)
    ) as sess:
        if len(name_or_id) == 11 or "youtu" in name_or_id:
            found = {"video_id": name_or_id, "title": name_or_id}
        else:
            found = await _json(sess, "/v1/search", q=name_or_id)

        tok = await _json(sess, "/v1/download", url=found["video_id"], type="audio")
        async with sess.get(
            f"{BASE}/v1/stream/{found['video_id']}",
            headers={"X-Download-Token": tok["download_token"]},
        ) as r:
            if r.status != 200:
                data = await r.json(content_type=None)
                raise ApiError(data.get("code", "error"), data.get("message", "failed"))
            with open(out_path, "wb") as f:
                async for chunk in r.content.iter_chunked(256 * 1024):
                    f.write(chunk)
        return found["video_id"], found["title"]
```

### Pyrogram bot me `/play`

```python
@app.on_message(filters.command("play"))
async def play(_, m):
    query = m.text.split(None, 1)[1]
    try:
        vid, title = await fetch_song(query, f"/tmp/{m.id}.mp3")
    except ApiError as e:
        return await m.reply(f"Error: {e}")
    await m.reply_audio(f"/tmp/{m.id}.mp3", title=title)
```

## 4. Error codes

Error hamesha JSON me aata hai: `{"status":"error","code":"daily_limit","message":"..."}`

| code | HTTP | Matlab / kya karein |
|---|---|---|
| `missing_key`, `invalid_key` | 401 | Key galat ya revoke ho gayi. Manager bot se nayi lo |
| `key_expired` | 401 | Key 28 din baad expire. Bot me **Renew** dabao |
| `rate_limited` | 429 | Ek minute me bahut requests. `Retry-After` header tak ruko |
| `daily_limit` | 429 | Aaj ka quota khatam. Midnight (IST) pe reset |
| `invalid_url`, `invalid_type` | 400 | `url` / `type` galat hai (`audio` ya `video`) |
| `token_expired`, `invalid_token`, `token_mismatch` | 401 | Naya token lo (`/v1/download`) |
| `not_found` | 404 | Search me kuch nahi mila |
| `busy` | 503 | Server vyast. 10 sec baad retry karo (quota wapas mil jaata hai) |
| `download_failed` | 502 | YouTube se nahi utra (private/blocked video). Quota wapas |
| `range_not_satisfiable` | 416 | Range header file ke bahar hai |

## 5. Acchi aadatein

- **Search ka result apne paas yaad rakho** (naam → `video_id`). Phir search dobara nahi karna padega.
- Ek hi gaana baar-baar mangoge to wo **cache se turant** milega (`X-Source: telegram-stream`).
- **Har `/v1/stream` call 1 request kaat-ti hai.** Player me Range/seek ke liye bhi alag call count hoti hai, isliye bot me file ek baar download karke Telegram pe bhejo.
- `busy` aur `rate_limited` pe turant dobara mat maaro, `Retry-After` ka intezaar karo.
- API key kabhi public repo ya group me mat daalo.

## 6. Cache aur direct stream (owner ke liye)

- Naya gaana pehli baar YouTube se utarta hai, user ko milta hai, aur background me **private Telegram channel** me save ho jaata hai (index MongoDB me).
- Dobara maangne pe agar gaana disk pe nahi hai to wo **Telegram channel se seedha user tak stream** hota hai, pehle server pe download nahi hota. Isme **Range (seek/resume)** bhi chalta hai (`206 Partial Content`).
- Disk pe jo file hai wo sabse tez hai. Koi dikkat aaye to API khud purane tareeke (download) pe wapas aa jaati hai.
- Band karna ho to env me `TELEGRAM_STREAM=false`.
- Server shift karte waqt `MONGO_URI`, `TELEGRAM_CACHE_CHANNEL`, `TELEGRAM_SESSION`, `API_ID`, `API_HASH`, `KEY_ENC_SECRET` wahi rakho, cache bana rahega.

## 7. Master key (owner ke liye unlimited quota)

- Env me `MASTER_KEY` set karo (**kam se kam 24 akshar**, lamba random string; chhota hoga to ignore ho jaata hai).
- Is key se: **daily limit nahi, per-minute limit nahi, quota nahi kat-ta.** Sab endpoints same hain, bas header me `X-API-Key: <MASTER_KEY>` do.
- `OWNER_IDS` wale log bot me `/test` karte hain to automatically master key use hoti hai (unlimited). Baaki sab users apni khud ki key/quota se test karte hain.
- Master key kisi ko mat do. Leak ho jaye to env me badal ke redeploy karo, purane master tokens bhi turant band ho jaate hain.
- Kisi ek user ki limit badhani ho to master key ki jagah `/setlimit user_id limit` use karo.

## 8. Bot commands

| Command | Kaun | Kaam |
|---|---|---|
| `/start` | Sab | Key banao, dekho, renew/revoke |
| `/test gaane ka naam` | Sab | Gaana search + download karke bot me bhejta hai. **User ki apni key aur quota (1 request) use hota hai**, aur gaana cache me save hota hai |
| `/docs` | Sab | API ka chhota guide |
| `/broadcast`, `/users`, `/revoke`, `/setlimit` | Sirf owner | Management |
