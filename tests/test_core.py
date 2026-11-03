"""Offline tests for the pure logic and the download engine (fake yt-dlp).

Run:  python -m tests.test_core
MongoDB / Telegram are NOT touched here.
"""
import asyncio
import os
import re
import stat
import sys
import tempfile
import time

TMP = tempfile.mkdtemp(prefix="opustest_")
FAKE_BIN = os.path.join(TMP, "bin")
os.makedirs(FAKE_BIN)
os.environ.update({
    "CACHE_DIR": os.path.join(TMP, "saved"),
    "MIN_FREE_MB": "0",
    "MAX_CONCURRENT_DOWNLOADS": "2",
    "MAX_QUEUE": "2",
    "QUEUE_WAIT_SECONDS": "5",
    "DOWNLOAD_TIMEOUT": "2",
    "KEY_ENC_SECRET": "test-secret",
    "TOKEN_SECRET": "test-token-secret",
    "RESET_TZ": "Asia/Kolkata",
    "FAKE_DIR": TMP,
})
os.environ["PATH"] = FAKE_BIN + os.pathsep + os.environ["PATH"]
os.makedirs(os.environ["CACHE_DIR"])

FAKE = '''#!/usr/bin/env python3
import os, sys, time, glob
a = sys.argv[1:]
out = a[a.index("-o") + 1]
vid = a[-1].split("v=")[-1]
d = os.environ["FAKE_DIR"]
open(os.path.join(d, "calls.log"), "a").write(vid + "\\n")
marker = os.path.join(d, "run_" + vid)
open(marker, "w").close()
n = len(glob.glob(os.path.join(d, "run_*")))
mx = os.path.join(d, "max")
try:  # the fake itself must not crash when several copies race on this file
    cur = int(open(mx).read() or 0) if os.path.exists(mx) else 0
    open(mx, "w").write(str(max(cur, n)))
except Exception:
    pass
try:
    if vid.startswith("FAIL"):
        sys.stderr.write("ERROR: Private video via http://user:secret@1.2.3.4:80")
        sys.exit(1)
    time.sleep(5 if vid.startswith("SLOW") else 0.5)
    open(out.replace("%(ext)s", "mp3"), "wb").write(b"ID3" + b"x" * 2048)
finally:
    os.remove(marker)
'''
p = os.path.join(FAKE_BIN, "yt-dlp")
open(p, "w").write(FAKE)
os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from OpusApi import config, downloader, security, utils  # noqa: E402


def test_video_id():
    assert utils.extract_video_id("dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert utils.extract_video_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=5") == "dQw4w9WgXcQ"
    assert utils.extract_video_id("https://youtu.be/dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert utils.extract_video_id("https://youtube.com/shorts/dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    for bad in ("", "--exec=rm -rf /", "-abc", "short", "a" * 12, "https://evil.com/x", None):
        assert utils.extract_video_id(bad) is None, bad


def test_proxy_parse():
    assert utils.parse_proxy("1.2.3.4:80:u:p") == "http://u:p@1.2.3.4:80"
    assert utils.parse_proxy("1.2.3.4:80") == "http://1.2.3.4:80"
    assert utils.parse_proxy("socks5://x:1") == "socks5://x:1"
    assert utils.parse_proxy("garbage") is None


def test_shard_is_stable_and_balanced():
    assert utils.shard_index("dQw4w9WgXcQ", 2) == utils.shard_index("dQw4w9WgXcQ", 2)
    import random, string
    random.seed(1)
    ids = ["".join(random.choices(string.ascii_letters + string.digits, k=11)) for _ in range(2000)]
    c = sum(utils.shard_index(i, 2) for i in ids)
    assert 850 < c < 1150, c


def test_keys_and_tokens():
    k = security.generate_api_key()
    assert k.startswith("opus_") and len(security.hash_key(k)) == 64
    enc = security.encrypt_key(k)
    assert enc != k and security.decrypt_key(enc) == k
    assert security.decrypt_key("garbage") is None
    t = security.sign_token(42, "dQw4w9WgXcQ", "audio")
    c, err = security.verify_token(t)
    assert err is None and c["tg_id"] == 42 and c["video_id"] == "dQw4w9WgXcQ" and c["type"] == "audio"
    assert security.verify_token(t[:-2] + "xx")[1] == "invalid"
    assert security.verify_token("nonsense")[1] == "invalid"
    assert security.verify_token(security.sign_token(1, "a" * 11, "audio", ttl=-5))[1] == "expired"


def test_daily_reset_clock():
    from datetime import datetime, timezone
    # 18:29 UTC = 23:59 IST -> resets in ~1 min; 18:31 UTC is already the next IST day
    a = datetime(2026, 10, 5, 18, 29, tzinfo=timezone.utc)
    b = datetime(2026, 10, 5, 18, 31, tzinfo=timezone.utc)
    assert utils.today_str(a) == "2026-10-05" and utils.today_str(b) == "2026-10-06"
    assert utils.seconds_until_reset(a) == 60


def test_bot_strings_exist():
    import json
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    data = json.load(open(os.path.join(root, "OpusBot", "en.json"), encoding="utf-8"))
    code = ""
    for f in ("handlers.py", "keyboards.py"):
        code += open(os.path.join(root, "OpusBot", f), encoding="utf-8").read()
    used_t = set(re.findall(r'\bT\(\s*"([a-z_]+)"', code))
    used_b = set(re.findall(r'\bB\(\s*"([a-z_]+)"', code))
    for k in used_t:
        if k.endswith("_"):  # dynamic prefix such as "key_state_" + state
            continue
        assert k in data["texts"], "missing text: " + k
    for k in used_b:
        assert k in data["buttons"], "missing button: " + k
    for st in ("active", "expired", "revoked", "none"):
        assert "key_state_" + st in data["texts"]
    from OpusBot.strings import T, esc
    assert "&lt;b&gt;" in esc("<b>") and "Hey <b>A &amp; B</b>" in T("start", name=esc("A & B"))


def _calls():
    path = os.path.join(TMP, "calls.log")
    return open(path).read().split() if os.path.exists(path) else []


async def _engine():
    dl = downloader
    # 1. concurrency cap + bounded queue: 5 different songs at once
    ids = [f"song{i:07d}" for i in range(5)]
    res = await asyncio.gather(*(dl.get_media(i, "audio") for i in ids), return_exceptions=True)
    busy = [r for r in res if isinstance(r, dl.Busy)]
    ok = [r for r in res if not isinstance(r, Exception)]
    assert len(busy) == 1 and len(ok) == 4, (len(busy), len(ok), res)
    assert int(open(os.path.join(TMP, "max")).read()) <= 2, "more than 2 downloads ran at once"
    assert all(r.source == "download" and r.persist for r in ok)

    # 2. second request is a disk hit, no yt-dlp call
    before = len(_calls())
    m = await dl.get_media(ok[0].path.split(os.sep)[-1].split(".")[0], "audio")
    assert m.source == "disk" and len(_calls()) == before

    # 3. same song requested 3x at once -> only ONE download
    before = len(_calls())
    r = await asyncio.gather(*(dl.get_media("sameSong001", "audio") for _ in range(3)))
    assert len(_calls()) - before == 1 and sorted(x.source for x in r) == ["disk", "disk", "download"]

    # 4. failure: generic message, no proxy credentials, no leftovers
    try:
        await dl.get_media("FAILvideo01", "audio")
        raise AssertionError("expected DownloadError")
    except dl.DownloadError as e:
        assert "secret" not in str(e) and "Private" not in str(e)

    # 5. timeout kills the process and cleans up
    t0 = time.time()
    try:
        await dl.get_media("SLOWvideo01", "audio")
        raise AssertionError("expected DownloadError")
    except dl.DownloadError:
        assert time.time() - t0 < 4.5
    leftovers = [f for f in os.listdir(config.CACHE_DIR) if ".tmp." in f]
    assert not leftovers, leftovers

    # 6. video: temp file, flagged for deletion, never persisted
    v = await dl.get_media("videoTest01", "video")
    assert v.delete_after and not v.persist and os.path.exists(v.path)
    os.remove(v.path)

    # 7. locks table does not leak
    assert dl._locks == {}, dl._locks
    # 8. LRU cleanup never touches non-media files
    open(os.path.join(config.CACHE_DIR, "__init__.py"), "w").close()
    config.MIN_FREE_BYTES = 10 ** 18
    dl.free_up_space()
    assert os.path.exists(os.path.join(config.CACHE_DIR, "__init__.py"))
    assert not [f for f in os.listdir(config.CACHE_DIR) if f.endswith(".mp3")]


def test_engine():
    asyncio.run(_engine())


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("PASS", name)
            except Exception as e:
                failed += 1
                import traceback
                traceback.print_exc()
                print("FAIL", name, e)
    sys.exit(1 if failed else 0)
