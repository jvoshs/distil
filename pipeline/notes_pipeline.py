#!/usr/bin/env python3
"""
Share to Notes: turns links you share into tidy notes. Runs on GitHub Actions.

For each waiting link in your inbox (the Cloudflare Worker in worker/):
  1. Fetch the post's media with a fetcher plugin you provide (see examples/fetcher_example.py).
     Without a plugin, only direct links to video or image files work.
  2. Speech -> text with Whisper (Groq first, Cloudflare Workers AI as backup).
  3. Gemini reads the images and frames and writes the note (Cloudflare as backup).
  4. The note is saved as Markdown in your notes repo, one page per post, sorted by category,
     and sent to a Notion database if you set one up.

Settings come from environment variables. See docs/SETUP.md for the full list.

Test one link locally:  NOTES_DIR=./my-notes python pipeline/notes_pipeline.py --url "https://example.com/clip.mp4" --print
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import difflib
import hashlib
import importlib.util
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

def env(name: str, default: str = "") -> str:
    """An environment variable, treating a blank value (an unset GitHub variable) as missing."""
    return os.environ.get(name) or default


REPO_DIR = Path(os.environ.get("NOTES_DIR") or Path(__file__).resolve().parent.parent).resolve()


def load_settings_file() -> None:
    """Reads settings.env from your notes repo: one KEY=value per line, for settings that aren't secret.
    Keeping them in the private repo means they never show up in a public workflow log."""
    f = REPO_DIR / "settings.env"
    if not f.exists():
        return
    for line in f.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and not os.environ.get(k):
            os.environ[k] = v


load_settings_file()

VIDEO_EXT = {".mp4", ".mov", ".m4v", ".webm", ".mkv"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 "
              "(KHTML, like Gecko) Version/17.0 Safari/605.1.15")

log = logging.getLogger("notes")


class DailyLimitReached(Exception):
    """Cloudflare's free daily AI allowance is used up. It resets at midnight UTC."""


# --------------------------------------------------------------------------- settings

@dataclass
class Settings:
    inbox_url: str = env("INBOX_URL", "").rstrip("/")
    inbox_token: str = env("INBOX_TOKEN", "")
    cf_account: str = env("CF_ACCOUNT_ID", "")
    cf_token: str = env("CF_API_TOKEN", "")
    whisper_model: str = env("WHISPER_MODEL", "@cf/openai/whisper-large-v3-turbo")
    vision_model: str = env("VISION_MODEL", "@cf/meta/llama-3.2-11b-vision-instruct")
    text_model: str = env("TEXT_MODEL", "@cf/meta/llama-3.3-70b-instruct-fp8-fast")
    frames_per_video: int = int(env("FRAMES_PER_VIDEO", "4"))
    max_items: int = int(env("MAX_ITEMS", "30"))
    time_budget_min: int = int(env("TIME_BUDGET_MIN", "20"))
    image_max_px: int = 1024
    groq_key: str = env("GROQ_API_KEY", "")
    groq_whisper: str = env("GROQ_WHISPER_MODEL", "whisper-large-v3-turbo")
    gemini_key: str = env("GEMINI_API_KEY", "")
    gemini_model: str = env("GEMINI_MODEL", "")  # blank = newest Flash-Lite in your project
    gemini_frames: int = int(env("GEMINI_FRAMES", "8"))
    notion_token: str = env("NOTION_TOKEN", "")
    notion_ds: str = env("NOTION_DATA_SOURCE_ID", "")
    notion_hub: str = env("NOTION_HUB_ID", "")


# --------------------------------------------------------------------------- quiet logs
# Logs on a public repo can be read by anyone. In quiet mode, links, titles, creators and post IDs
# are replaced before anything is printed, so the logs only show counts and errors.

QUIET = env("QUIET_LOGS", "1" if os.environ.get("GITHUB_ACTIONS") == "true" else "0") != "0"
_HIDDEN: set[str] = set()
_URL_RE = re.compile(r"https?://[^\s'\")\]>]+")


def hide(*texts) -> None:
    """Never print these strings (titles, creators, IDs) while quiet mode is on."""
    for t in texts:
        t = str(t or "").strip()
        if len(t) >= 4:
            _HIDDEN.add(t)


def redact(text: str) -> str:
    if not QUIET or not text:
        return text
    text = _URL_RE.sub("<link>", text)
    for h in sorted(_HIDDEN, key=len, reverse=True):
        if h in text:
            text = text.replace(h, "<hidden>")
    return text


class _RedactingStream:
    def __init__(self, inner):
        self.inner = inner

    def write(self, s):
        return self.inner.write(redact(s))

    def __getattr__(self, name):
        return getattr(self.inner, name)


def quiet_output() -> None:
    if QUIET:
        sys.stdout = _RedactingStream(sys.stdout)
        sys.stderr = _RedactingStream(sys.stderr)


# --------------------------------------------------------------------------- helpers

def run(cmd: list[str], timeout: int = 300) -> subprocess.CompletedProcess:
    log.debug("run: %s", " ".join(cmd))
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, "", "timed out")


def http(url: str, method: str = "GET", body=None, headers: dict | None = None, timeout: int = 120,
         raw: bytes | None = None) -> dict:
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("user-agent", "ShareToNotes/1.0")
    if raw is None:
        req.add_header("content-type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout) as res:
        text = res.read().decode() or "{}"
    return json.loads(text)


def download_file(url: str, dest: Path) -> Path:
    req = urllib.request.Request(url, headers={"user-agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=120) as res, open(dest, "wb") as f:
        shutil.copyfileobj(res, f)
    return dest


def slugify(text: str, max_len: int = 60) -> str:
    text = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE).strip()
    text = re.sub(r"[\s_]+", " ", text)
    return text[:max_len].strip() or "Untitled"


# Add your own with STRIP_PARAMS in settings.env (comma separated). Keep it the same as the Worker's STRIP_PARAMS.
TRACKING_PARAMS = {"fbclid", "gclid", "si", "ref", "ref_src", "feature", "mibextid", "share_id"} | {
    p.strip().lower() for p in env("STRIP_PARAMS").split(",") if p.strip()}


def normalise_url(url: str) -> str:
    """Same rules as the inbox Worker: https, no www, no tracking parameters, no trailing slash."""
    try:
        u = urllib.parse.urlsplit(url.strip())
    except ValueError:
        return url.strip()
    host = (u.hostname or "").lower()
    for prefix in ("www.", "m."):
        if host.startswith(prefix):
            host = host[len(prefix):]
    query = sorted((k, v) for k, v in urllib.parse.parse_qsl(u.query, keep_blank_values=True)
                   if not k.lower().startswith("utm_") and k.lower() not in TRACKING_PARAMS)
    path = u.path.rstrip("/") or ""
    return urllib.parse.urlunsplit(("https", host, path, urllib.parse.urlencode(query, safe="*"), ""))


def url_key(url: str) -> str:
    """A short ID for a link: the first 12 characters of the SHA-1 of the normalised link."""
    return hashlib.sha1(normalise_url(url).encode()).hexdigest()[:12]


def key_for(url: str) -> str:
    """The post ID used to name thumbnails and match notes. A fetcher plugin can supply its own."""
    plugin = load_fetcher()
    if plugin is not None and hasattr(plugin, "key"):
        try:
            k = str(plugin.key(url) or "").strip()
            if k:
                return k
        except Exception:
            pass
    return url_key(url)


def dedupe_texts(texts: list[str], threshold: float = 0.8) -> list[str]:
    kept: list[str] = []
    for t in texts:
        if t and all(difflib.SequenceMatcher(None, t.lower(), k.lower()).ratio() < threshold for k in kept):
            kept.append(t)
    return kept


# --------------------------------------------------------------------------- inbox

class Inbox:
    def __init__(self, s: Settings):
        self.base = s.inbox_url
        self.headers = {"authorization": f"Bearer {s.inbox_token}"}

    def pending(self, limit: int) -> list[dict]:
        return http(f"{self.base}/api/pending?limit={limit}", headers=self.headers).get("items", [])

    def done(self, item_id: int, **fields) -> None:
        http(f"{self.base}/api/done", "POST", {"id": item_id, "status": "done", **fields}, headers=self.headers)

    def failed(self, item_id: int, note: str) -> str:
        """Returns "pending" if it will be tried again, or "failed" once it has run out of tries."""
        res = http(f"{self.base}/api/done", "POST", {"id": item_id, "status": "failed", "note": note[:400]},
                   headers=self.headers)
        return str(res.get("status") or "")

    def release(self, item_id: int, note: str) -> None:
        http(f"{self.base}/api/release", "POST", {"id": item_id, "note": note}, headers=self.headers)


# --------------------------------------------------------------------------- fetching

@dataclass
class Post:
    url: str = ""
    shortcode: str = ""
    author: str = ""
    caption: str = ""
    posted: str = ""
    media: list[Path] = field(default_factory=list)
    source: str = ""
    cover_url: str = ""  # the post's cover image, when the fetcher gives one


_fetcher = None
_fetcher_loaded = False


def load_fetcher():
    """Loads your fetcher plugin, if there is one.
    It's looked for at FETCHER_PATH, or private/fetcher.py inside your notes folder."""
    global _fetcher, _fetcher_loaded
    if _fetcher_loaded:
        return _fetcher
    _fetcher_loaded = True
    path = Path(os.environ.get("FETCHER_PATH") or REPO_DIR / "private" / "fetcher.py")
    if not path.exists():
        log.info("No fetcher plugin found. Only direct links to media files will work.")
        return None
    sys.dont_write_bytecode = True  # no __pycache__ left in your notes repo
    spec = importlib.util.spec_from_file_location("user_fetcher", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not hasattr(module, "fetch"):
        raise RuntimeError("The fetcher plugin needs a fetch(url, workdir) function.")
    _fetcher = module
    log.info("Loaded fetcher plugin")
    return _fetcher


def load_fetcher_env() -> None:
    """FETCHER_ENV holds extra settings for your plugin, one KEY=value per line.
    It's one GitHub secret, so the public workflow never has to name your plugin's keys."""
    for line in env("FETCHER_ENV", "").splitlines():
        if "=" not in line or line.strip().startswith("#"):
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        if k and k not in os.environ:
            os.environ[k] = v
            if os.environ.get("GITHUB_ACTIONS") == "true" and len(v) >= 4:
                print(f"::add-mask::{v}", flush=True)


def post_from_dict(url: str, data: dict, source: str) -> Post:
    media = [Path(m) for m in (data.get("media") or []) if Path(m).exists()]
    post = Post(url=url, shortcode=str(data.get("shortcode") or data.get("id") or key_for(url)),
                author=str(data.get("author") or "").lstrip("@"), caption=str(data.get("caption") or ""),
                posted=str(data.get("posted") or "")[:10], media=media, source=str(data.get("source") or source),
                cover_url=str(data.get("cover_url") or ""))
    hide(post.shortcode, post.author)
    return post


def fetch_direct(url: str, workdir: Path) -> Post | None:
    """Built-in fetcher: a link straight to a video or image file."""
    path = urllib.parse.urlsplit(url).path
    ext = Path(path).suffix.lower()
    if ext not in VIDEO_EXT | IMAGE_EXT:
        return None
    dest = workdir / f"direct{ext}"
    download_file(url, dest)
    return post_from_dict(url, {"media": [dest]}, "direct link")


def fetch_post(url: str, workdir: Path, cover_only: bool = False) -> Post | None:
    """Gets a post's media and caption: your plugin first, then the built-in direct-link fetcher."""
    plugin = load_fetcher()
    if plugin is not None:
        fn = getattr(plugin, "fetch_cover", None) if cover_only else None
        data = (fn or plugin.fetch)(url, workdir)
        if data:
            return post_from_dict(url, data, "plugin")
    return fetch_direct(url, workdir)


# --------------------------------------------------------------------------- Workers AI

class WorkersAI:
    def __init__(self, s: Settings):
        self.base = f"https://api.cloudflare.com/client/v4/accounts/{s.cf_account}/ai/run/"
        self.headers = {"authorization": f"Bearer {s.cf_token}"}
        self.agreed: set[str] = set()

    def run(self, model: str, body: dict | None = None, raw: bytes | None = None, timeout: int = 180) -> dict:
        try:
            res = http(self.base + model, "POST", body, headers=self.headers, timeout=timeout, raw=raw)
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="ignore")[:600]
            low = detail.lower()
            if e.code == 429 or "neuron" in low or "daily" in low or "quota" in low or "capacity" in low and "free" in low:
                raise DailyLimitReached(detail) from e
            if "agree" in low and model not in self.agreed:
                # Meta's Llama vision model needs a one-off "agree" to its licence.
                self.agreed.add(model)
                http(self.base + model, "POST", {"prompt": "agree"}, headers=self.headers, timeout=60)
                return self.run(model, body, raw, timeout)
            raise RuntimeError(f"Workers AI {model} error {e.code}: {detail}") from e
        if not res.get("success", True):
            raise RuntimeError(f"Workers AI {model} failed: {res.get('errors')}")
        return res.get("result", res)


_groq_ok = True
GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"


def groq_transcribe(audio: Path, s: Settings) -> tuple[str, str]:
    """Free Whisper on Groq. Returns (text, language)."""
    boundary = f"----notes{int(time.time() * 1000)}"
    def field(name: str, value: str) -> bytes:
        return f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
    body = (field("model", s.groq_whisper) + field("response_format", "verbose_json") + field("temperature", "0")
            + f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="audio.mp3"\r\n'
              f"Content-Type: audio/mpeg\r\n\r\n".encode()
            + audio.read_bytes() + f"\r\n--{boundary}--\r\n".encode())
    req = urllib.request.Request(GROQ_URL, data=body, method="POST")
    req.add_header("authorization", f"Bearer {s.groq_key}")
    req.add_header("content-type", f"multipart/form-data; boundary={boundary}")
    req.add_header("user-agent", "ShareToNotes/1.0")
    with urllib.request.urlopen(req, timeout=180) as res:
        out = json.loads(res.read().decode() or "{}")
    return (out.get("text") or "").strip(), str(out.get("language") or "").lower()


def transcribe(video: Path, workdir: Path, s: Settings, ai: WorkersAI) -> str:
    global _groq_ok
    audio = workdir / (video.stem + ".mp3")
    res = run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(video), "-vn", "-ac", "1", "-ar", "16000",
               "-b:a", "48k", str(audio)])
    if res.returncode != 0 or not audio.exists() or audio.stat().st_size < 3000:
        return ""  # no audio track
    if s.groq_key and _groq_ok:
        try:
            text, lang = groq_transcribe(audio, s)
            bump("groq_seconds", max(10.0, video_duration(audio)))
            if len(text.split()) < 6:  # music-only videos give a few junk words
                return ""
            return f"[{lang}] {text}" if lang and lang not in ("en", "english") else text
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="ignore")[:200]
            if e.code == 429:
                _groq_ok = False
                log.warning("Groq's free speech allowance is used up for now. Using Cloudflare.")
            else:
                log.warning("Groq speech failed (%s %s). Using Cloudflare for this one.", e.code, detail)
        except Exception as e:
            log.warning("Groq speech failed (%s). Using Cloudflare for this one.", str(e)[:150])
    data = audio.read_bytes()
    try:
        out = ai.run(s.whisper_model, {"audio": base64.b64encode(data).decode(), "vad_filter": True})
    except DailyLimitReached:
        raise
    except Exception as e:
        log.info("Turbo Whisper failed (%s). Trying the basic Whisper model.", str(e)[:120])
        out = ai.run("@cf/openai/whisper", raw=data)
    text = (out.get("text") or "").strip()
    if len(text.split()) < 6:  # music-only videos give a few junk words
        return ""
    info = out.get("transcription_info") or {}
    lang = info.get("language") if isinstance(info, dict) else ""
    return f"[{lang}] {text}" if lang and lang != "en" else text


def video_duration(video: Path) -> float:
    res = run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(video)])
    try:
        return float(res.stdout.strip())
    except ValueError:
        return 0.0


def extract_frames(video: Path, workdir: Path, count: int) -> list[Path]:
    duration = video_duration(video)
    fdir = workdir / f"frames_{video.stem}"
    fdir.mkdir(exist_ok=True)
    times = [duration * (i + 0.5) / count for i in range(count)] if duration > 0 else [0.5]
    frames = []
    for i, t in enumerate(times, 1):
        out = fdir / f"{i:02d}.jpg"
        run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{t:.2f}", "-i", str(video), "-frames:v", "1",
             "-vf", "scale='min(1080,iw)':-2", "-q:v", "3", str(out)])
        if out.exists():
            frames.append(out)
    return frames


def shrink(image: Path, workdir: Path, max_px: int) -> Path:
    out = workdir / f"small_{image.parent.name}_{image.stem}.jpg"
    run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(image), "-vf",
         f"scale='if(gt(iw,ih),min({max_px},iw),-2)':'if(gt(iw,ih),-2,min({max_px},ih))'", "-q:v", "4", str(out)])
    return out if out.exists() else image


VISION_PROMPT = (
    "You are reading one frame or slide from a saved social media post or video.\n"
    "1. Write out ALL readable text exactly as it appears, keeping line breaks and lists. Include titles, "
    "authors, dates, names, numbers and links, even small print. "
    "Ignore the app's own interface (like counts, usernames in the UI, buttons).\n"
    "2. Only if the image shows a chart, table, diagram, screenshot of an app or website, or a product, "
    "add ONE short line starting with 'Visual:' saying what information it shows.\n"
    "Never describe people, clothing, rooms, furniture, scenery, backgrounds or the mood of the image.\n"
    "If there is no meaningful text or information, reply with exactly: NONE"
)


BACKUP_VISION_MODEL = "@cf/google/gemma-3-12b-it"
_vision_format: dict[str, int] = {}  # remembers which request shape worked for each model


def vision_bodies(img: bytes) -> list[dict]:
    """Workers AI vision models accept images in a few different shapes. Try them in order."""
    b64 = base64.b64encode(img).decode()
    data_url = f"data:image/jpeg;base64,{b64}"
    return [
        # 1. OpenAI-style message with the image inside it
        {"messages": [{"role": "user", "content": [
            {"type": "text", "text": VISION_PROMPT},
            {"type": "image_url", "image_url": {"url": data_url}},
        ]}], "max_tokens": 700, "temperature": 0.1},
        # 2. Plain prompt plus raw image bytes
        {"prompt": VISION_PROMPT, "image": list(img), "max_tokens": 700, "temperature": 0.1},
        # 3. System + user messages plus raw image bytes
        {"messages": [{"role": "system", "content": "You read text from images accurately."},
                      {"role": "user", "content": VISION_PROMPT}],
         "image": list(img), "max_tokens": 700, "temperature": 0.1},
    ]


def ask_vision(model: str, img: bytes, ai: WorkersAI) -> dict:
    bodies = vision_bodies(img)
    order = list(range(len(bodies)))
    if model in _vision_format:  # use the shape that worked last time first
        order.remove(_vision_format[model])
        order.insert(0, _vision_format[model])
    last: Exception | None = None
    for i in order:
        try:
            out = ai.run(model, bodies[i])
            _vision_format[model] = i
            return out
        except DailyLimitReached:
            raise
        except RuntimeError as e:
            last = e
            log.info("Vision format %d not accepted by %s: %s", i + 1, model, str(e)[:160])
    raise RuntimeError(f"{model} didn't accept the image: {last}")


def read_image(image: Path, workdir: Path, s: Settings, ai: WorkersAI) -> str:
    small = shrink(image, workdir, s.image_max_px)
    img = small.read_bytes()
    try:
        out = ask_vision(s.vision_model, img, ai)
    except DailyLimitReached:
        raise
    except RuntimeError as e:
        if s.vision_model == BACKUP_VISION_MODEL:
            raise
        log.warning("Main vision model failed, trying %s (%s)", BACKUP_VISION_MODEL, str(e)[:160])
        out = ask_vision(BACKUP_VISION_MODEL, img, ai)
    text = str(out.get("response") or out.get("description") or "").strip()
    return "" if text.upper().startswith("NONE") and len(text) < 12 else text


# --------------------------------------------------------------------------- extraction + note

@dataclass
class Extraction:
    transcript_parts: list[str] = field(default_factory=list)
    slides: list[str] = field(default_factory=list)
    on_screen: list[str] = field(default_factory=list)
    kind: str = "post"


def safe_read(image: Path, workdir: Path, s: Settings, ai: WorkersAI) -> str:
    """A slide the AI can't read shouldn't sink the whole note."""
    try:
        return read_image(image, workdir, s, ai)
    except DailyLimitReached:
        raise
    except Exception as e:
        log.warning("Skipped reading %s: %s", image.name, str(e)[:200])
        return ""


def extract(post: Post, workdir: Path, s: Settings, ai: WorkersAI, audio: bool = True,
            visuals: bool = True) -> Extraction:
    ex = Extraction()
    multi = len(post.media) > 1
    if multi:
        ex.kind = "carousel"
    elif post.media and post.media[0].suffix.lower() in VIDEO_EXT:
        ex.kind = VIDEO_LABEL
    elif post.media:
        ex.kind = "image"
    for n, media in enumerate(post.media, 1):
        label = f"Slide {n}" if multi else ""
        if media.suffix.lower() in VIDEO_EXT:
            if audio:
                log.info("Transcribing %s", media.name)
                speech = transcribe(media, workdir, s, ai)
                if speech:
                    ex.transcript_parts.append(f"{label}: {speech}" if label else speech)
            if not visuals:
                continue
            log.info("Reading on-screen text from %d frames", s.frames_per_video)
            texts = dedupe_texts([safe_read(f, workdir, s, ai) for f in extract_frames(media, workdir, s.frames_per_video)])
            if label and texts:
                ex.slides.append(f"{label} (video): " + "\n".join(texts))
            else:
                ex.on_screen.extend(texts)
        elif visuals:
            log.info("Reading %s", media.name)
            text = safe_read(media, workdir, s, ai)
            if text:
                ex.slides.append(f"{label}:\n{text}" if label else text)
    return ex


DEFAULT_CATEGORIES = {
    "Money & Investing": ("💰", "personal finance, budgeting, saving, investing, shares, crypto, tax, property"),
    "Career & Study": ("🎓", "jobs, CVs, interviews, internships, networking, studying, exams"),
    "Business & Startups": ("🚀", "entrepreneurship, startups, marketing, sales, side hustles, business strategy"),
    "Tech & AI": ("🤖", "AI tools, apps, coding, software, gadgets"),
    "Fitness & Health": ("💪", "workouts, gym, running, sport, sleep, nutrition science, wellbeing"),
    "Food & Recipes": ("🍳", "recipes, cooking, meal prep, restaurants, drinks"),
    "Style & Beauty": ("👔", "fashion, outfits, skincare, hair, photos and posing"),
    "Travel": ("✈️", "destinations, travel hacks, flights, packing"),
    "Productivity & Mindset": ("🧠", "habits, motivation, discipline, time management, communication, life advice"),
    "Learning": ("📚", "science, history, language, how things work, general knowledge"),
    "Other": ("📌", "anything that fits none of the above"),
}


def load_categories() -> dict[str, tuple[str, str]]:
    """Your own categories can go in categories.json in the notes folder:
    {"Category name": {"icon": "💰", "hint": "what belongs here"}, ...}. "Other" is always added."""
    f = REPO_DIR / "categories.json"
    cats = dict(DEFAULT_CATEGORIES)
    if f.exists():
        try:
            raw = json.loads(f.read_text(encoding="utf-8"))
            cats = {str(k): (str((v or {}).get("icon") or "📌"), str((v or {}).get("hint") or ""))
                    for k, v in raw.items() if k != "_comment"}
        except Exception as e:
            log.warning("Couldn't read categories.json (%s). Using the default categories.", str(e)[:120])
    cats.setdefault("Other", ("📌", "anything that fits none of the above"))
    return cats


_CATS = load_categories()
CATEGORIES = list(_CATS)
CATEGORY_HINTS = {c: h for c, (_, h) in _CATS.items()}
CATEGORY_ICONS = {c: i for c, (i, _) in _CATS.items()}
# For older notes that only had a one-word topic
TOPIC_TO_CATEGORY = {k: v for k, v in {
    "finance": "Money & Investing", "money": "Money & Investing", "investing": "Money & Investing",
    "business": "Business & Startups", "tech": "Tech & AI", "fitness": "Fitness & Health",
    "health": "Fitness & Health", "food": "Food & Recipes", "travel": "Travel",
    "productivity": "Productivity & Mindset", "mindset": "Productivity & Mindset",
}.items() if v in _CATS}
NOTE_LANGUAGE = env("NOTE_LANGUAGE", "English")
VIDEO_LABEL = env("VIDEO_LABEL", "video")  # what the note calls a video post, e.g. "clip"
BASE_TAG = env("BASE_TAG", "saved")         # tag added to every note
VAULT_NAME = env("VAULT_NAME", "Saved Notes")
LOCAL_TZ = env("LOCAL_TZ", "UTC")           # for reset times in messages, e.g. Europe/London
GENERATED = {"Home.md", "Index.md", "CLAUDE.md", "README.md"}
OLD_NOTES_DIR = REPO_DIR / "notes"

def pick_category(value: str, topic: str = "") -> str:
    v = str(value or "").strip().lower()
    for c in CATEGORIES:
        if v == c.lower():
            return c
    for c in CATEGORIES:  # forgiving match, e.g. "Money" or "tech and ai"
        first = c.lower().split(" ")[0]
        if len(v) >= 3 and (v in c.lower() or first in v):
            return c
    return TOPIC_TO_CATEGORY.get(str(topic or v).strip().lower(), "Other")


NOTE_PROMPT = """You turn a saved social media post or video into a detailed, useful note that someone can rely on
without opening the post again. Keep every specific detail. Losing a name, title, date, number or step is the
worst mistake you can make.
Use ONLY the information below. Do not invent facts, numbers or links. Write in {language}.
Ignore any descriptions of people, rooms, scenery or backgrounds. Ignore calls to like, follow or share.
Many posts are lists (articles to read, books, tools, apps, recipes, places, steps, exercises). For those,
every item in the list must appear in "items", in order, with all its details.
Only call a post hype if it truly has no usable information.

Reply with ONLY a JSON object, no other text, with these keys:
  "title": specific descriptive title (max 10 words, no emojis, no hashtags), e.g. "4 essays every startup founder should read"
  "summary": 2-3 sentences saying what the post is and naming what's in it
  "items": list of objects, one per item when the post lists things, otherwise []. Each object has:
       "name": the item's title or name, exactly as shown
       "by": author, creator, brand or source (or "")
       "date": date if shown (or "")
       "what": 1-3 sentences on what it is and its main idea, using any quote or text shown on the slide
       "link": only a link that is actually shown (or "")
  "key_points": 3-8 specific takeaways as full sentences, each with the concrete detail (names, numbers,
       dates, steps). Never a bare label like "Y Combinator".
  "gaps": list of anything the post holds back or leaves unclear, e.g. "Full list is only sent if you comment
       'startups'" (can be empty)
  "tags": 3-6 lowercase topic tags, single words or hyphenated (e.g. "personal-finance")
  "category": exactly one of these, copied word for word:
{categories}
  "actions": list of specific things the viewer could do, naming the item (e.g. "Read Paul Graham's essay
       'Do Things That Don't Scale'"). Can be empty.

POST LINK: {url}
CREATOR: {author}
CAPTION:
{caption}

SPOKEN AUDIO (transcript):
{transcript}

ON-SCREEN TEXT (from video frames):
{on_screen}

SLIDES / IMAGES:
{slides}
"""


def parse_json_loose(text) -> dict:
    if isinstance(text, dict):
        return text
    text = re.sub(r"<think>.*?</think>", "", str(text), flags=re.S)
    start, end = text.find("{"), text.rfind("}")
    return json.loads(text[start:end + 1]) if start != -1 and end > start else {}


def compose_note(post: Post, ex: Extraction, s: Settings, ai: WorkersAI, gem: "Gemini | None" = None) -> dict:
    prompt = note_prompt(post, ex)
    data: dict = {}
    if gem and gem.ok:
        try:
            data = parse_json_loose(gem.generate([{"text": prompt}]))
        except GeminiDailyLimit:
            log.warning("Gemini's free daily limit is used up. Using Cloudflare until it resets.")
        except Exception as e:
            log.warning("Gemini failed (%s). Using Cloudflare.", str(e)[:200])
    if not data:
        try:
            out = ai.run(s.text_model, {"messages": [{"role": "user", "content": prompt}], "max_tokens": 2500,
                                        "temperature": 0.1})
            data = parse_json_loose(out.get("response"))
        except DailyLimitReached:
            raise
        except Exception as e:
            log.warning("Could not write a summary (%s). Saving the raw text only.", str(e)[:200])
    return normalise_note(data, post, ex)


def normalise_note(data: dict, post: Post, ex: Extraction) -> dict:
    first_line = post.caption.strip().splitlines()[0] if post.caption.strip() else ""
    data.setdefault("title", first_line[:60] or f"Saved {ex.kind} {post.shortcode}")
    for key, default in (("summary", ""), ("key_points", []), ("tags", []), ("actions", []), ("items", []),
                         ("gaps", [])):
        data.setdefault(key, default)
    for key in ("key_points", "tags", "actions", "gaps"):
        if isinstance(data[key], str):
            data[key] = [data[key]]
    items = []
    for it in data["items"] if isinstance(data["items"], list) else []:
        if isinstance(it, dict) and str(it.get("name", "")).strip():
            items.append({k: " ".join(str(it.get(k) or "").split()) for k in ("name", "by", "date", "what", "link")})
        elif isinstance(it, str) and it.strip():
            items.append({"name": it.strip(), "by": "", "date": "", "what": "", "link": ""})
    data["items"] = items
    data["title"] = re.sub(r"[#\[\]|^]", "", str(data["title"])).strip() or f"Saved {ex.kind}"
    hide(data["title"])
    data["category"] = pick_category(data.get("category", ""), data.get("topic", ""))
    return data


class GeminiDailyLimit(Exception):
    pass


class Gemini:
    """Google Gemini (free tier). One call per post reads the slides and writes the note."""
    BASE = "https://generativelanguage.googleapis.com/v1beta"

    def __init__(self, s: Settings):
        self.key = s.gemini_key
        self.model = s.gemini_model
        self.ok = bool(self.key)

    def _headers(self) -> dict:
        return {"x-goog-api-key": self.key}

    def pick_model(self) -> str:
        if self.model:
            return self.model
        try:
            res = http(f"{self.BASE}/models?pageSize=200", headers=self._headers(), timeout=30)
            names = [m["name"].split("/")[-1] for m in res.get("models", [])
                     if "generateContent" in (m.get("supportedGenerationMethods") or [])]
        except Exception as e:
            log.warning("Couldn't list Gemini models (%s)", str(e)[:150])
            names = []
        lite = [n for n in names if "flash-lite" in n
                and not any(x in n for x in ("tts", "image", "audio", "live", "embedding"))]
        def version(n: str) -> float:
            m = re.search(r"(\d+(?:\.\d+)?)", n)
            return float(m.group(1)) if m else 0.0
        stable = [n for n in lite if not any(x in n for x in ("preview", "exp"))]
        pool = sorted(stable or lite, key=lambda n: (version(n), "latest" not in n), reverse=True)
        self.model = pool[0] if pool else "gemini-2.5-flash-lite"
        log.info("Using Gemini model %s", self.model)
        return self.model

    def generate(self, parts: list[dict]) -> str:
        url = f"{self.BASE}/models/{self.pick_model()}:generateContent"
        body = {"contents": [{"role": "user", "parts": parts}],
                "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json",
                                     "maxOutputTokens": 8192}}
        for attempt in range(4):
            try:
                res = http(url, "POST", body, headers=self._headers(), timeout=240)
            except urllib.error.HTTPError as e:
                detail = e.read().decode(errors="ignore")
                if e.code == 429 and ("PerDay" in detail or "per day" in detail.lower()):
                    self.ok = False
                    raise GeminiDailyLimit(detail[:300]) from e
                if e.code in (429, 500, 502, 503, 504) and attempt < 3:
                    wait = 20 * (attempt + 1)
                    log.info("Gemini busy (%s). Waiting %ss.", e.code, wait)
                    time.sleep(wait)
                    continue
                raise RuntimeError(f"Gemini error {e.code}: {detail[:300]}") from e
            bump("gemini")
            cands = res.get("candidates") or []
            if not cands:
                raise RuntimeError(f"Gemini returned nothing: {str(res.get('promptFeedback'))[:200]}")
            return "".join(p.get("text", "") for p in (cands[0].get("content") or {}).get("parts", []))
        raise RuntimeError("Gemini kept failing")


GEMINI_EXTRA = """
The images are attached below, each with a label in square brackets like [Slide 2] or [Frame 3].
Read every image carefully, including small print. Also include this key in the JSON:
  "extracted_text": a list with one object per labelled image, in the same order:
       {{"label": "<the label exactly as given>", "text": "<all readable text on that image, exactly as
       written, keeping line breaks. Add one short 'Visual:' line only for charts, tables, diagrams or app
       screenshots. Never describe people, rooms or scenery. Use an empty string if there is no text.>"}}
"""


def gather_images(post: Post, workdir: Path, s: Settings) -> list[tuple[str, Path, int]]:
    """(label, image, slide number) for every slide, and several frames from each video."""
    multi = len(post.media) > 1
    out: list[tuple[str, Path, int]] = []
    for n, media in enumerate(post.media, 1):
        if media.suffix.lower() in VIDEO_EXT:
            frames = extract_frames(media, workdir, s.gemini_frames)
            for i, f in enumerate(frames, 1):
                out.append((f"Slide {n} frame {i}" if multi else f"Frame {i}", shrink(f, workdir, s.image_max_px), n))
        else:
            out.append((f"Slide {n}" if multi else "Image", shrink(media, workdir, s.image_max_px), n))
    return out[:40]


def note_prompt(post: Post, ex: Extraction, images_attached: bool = False) -> str:
    seen = "(see the labelled images attached below)"
    return NOTE_PROMPT.format(
        language=NOTE_LANGUAGE,
        categories="\n".join(f'     - "{c}" ({CATEGORY_HINTS[c]})' for c in CATEGORIES),
        url=post.url or "(none)",
        author=post.author or "(unknown)",
        caption=(post.caption or "(none)")[:3000],
        transcript="\n".join(ex.transcript_parts)[:8000] or "(none)",
        on_screen=seen if images_attached else ("\n---\n".join(ex.on_screen)[:4000] or "(none)"),
        slides=seen if images_attached else ("\n---\n".join(ex.slides)[:8000] or "(none)"),
    ) + (GEMINI_EXTRA if images_attached else "")


def gemini_note(post: Post, ex: Extraction, s: Settings, gem: "Gemini", images) -> dict | None:
    """Reads the images and writes the note in one Gemini call. Fills ex with the text it read."""
    parts: list[dict] = [{"text": note_prompt(post, ex, images_attached=bool(images))}]
    for label, img, _ in images:
        parts.append({"text": f"[{label}]"})
        parts.append({"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(img.read_bytes()).decode()}})
    try:
        data = parse_json_loose(gem.generate(parts))
    except GeminiDailyLimit:
        log.warning("Gemini's free daily limit is used up. Using Cloudflare until it resets (%s).", local_reset(pacific=True))
        return None
    except Exception as e:
        log.warning("Gemini failed (%s). Using Cloudflare for this post.", str(e)[:200])
        return None
    if not data:
        return None
    read = {}
    for row in data.get("extracted_text") or []:
        if isinstance(row, dict):
            read[str(row.get("label", "")).strip()] = str(row.get("text") or "").strip()
    multi = len(post.media) > 1
    by_slide: dict[int, list[str]] = {}
    for label, _, n in images:
        text = read.get(label, "")
        if text:
            by_slide.setdefault(n, []).append(text)
    for n in sorted(by_slide):
        texts = dedupe_texts(by_slide[n])
        if multi:
            ex.slides.append(f"Slide {n}:\n" + "\n".join(texts))
        else:
            ex.on_screen.extend(texts) if post.media[0].suffix.lower() in VIDEO_EXT else ex.slides.extend(texts)
    data["_via"] = "gemini"
    return normalise_note(data, post, ex)


def yaml_str(value: str) -> str:
    return json.dumps(str(value), ensure_ascii=False)  # a JSON string is valid YAML


def callout(title: str, body: str) -> str:
    lines = body.strip().splitlines() or [""]
    return f"> [!quote]- {title}\n" + "\n".join("> " + ln for ln in lines) + "\n"


def clean_tags(tags) -> list[str]:
    out = [BASE_TAG] + [re.sub(r"[^a-z0-9-]", "", str(t).lower().replace(" ", "-")) for t in tags]
    return [t for i, t in enumerate(out) if t and t not in out[:i]]


THUMBS_DIR = REPO_DIR / "Thumbnails"
STATE_FILE = Path(env("RUNNER_TEMP", tempfile.gettempdir())) / "notes_written.json"
WRITTEN: set[str] = set()  # repo-relative paths this run created or changed


def mark_written(path: Path) -> None:
    WRITTEN.add(str(path.relative_to(REPO_DIR)))


USAGE_FILE = REPO_DIR / "usage.json"
USAGE: dict = {"gemini": {}, "groq_seconds": {}, "posts": {}}  # this run's additions only
GEMINI_DAILY = int(env("GEMINI_DAILY", "500"))
GROQ_DAILY_SECONDS = int(env("GROQ_DAILY_SECONDS", "28800"))
GITHUB_MONTHLY_MINUTES = int(env("GITHUB_MONTHLY_MINUTES", "2000"))  # private repos on the free plan


def local_reset(pacific: bool = False) -> str:
    """When a daily allowance resets, in your time zone. Gemini resets at midnight US Pacific, the rest at midnight UTC."""
    from zoneinfo import ZoneInfo
    try:
        tz = ZoneInfo(LOCAL_TZ)
    except Exception:
        tz = dt.timezone.utc
    if pacific:
        la = ZoneInfo("America/Los_Angeles")
        nxt = dt.datetime.combine(dt.datetime.now(la).date() + dt.timedelta(days=1), dt.time(0), la)
    else:
        nxt = dt.datetime.combine(dt.datetime.now(dt.timezone.utc).date() + dt.timedelta(days=1), dt.time(0), dt.timezone.utc)
    return nxt.astimezone(tz).strftime("%-I%p").lower() + ("" if LOCAL_TZ != "UTC" else " UTC")


def pacific_day() -> str:
    """Gemini's day runs on US Pacific time."""
    from zoneinfo import ZoneInfo
    return dt.datetime.now(ZoneInfo("America/Los_Angeles")).date().isoformat()


def utc_day() -> str:
    return dt.datetime.now(dt.timezone.utc).date().isoformat()


def bump(kind: str, amount: float = 1, day: str | None = None) -> None:
    day = day or (pacific_day() if kind == "gemini" else utc_day())
    USAGE[kind][day] = USAGE[kind].get(day, 0) + amount


def merged_usage(base: dict, extra: dict) -> dict:
    out = {k: dict(base.get(k, {})) for k in ("gemini", "groq_seconds", "posts")}
    for k in out:
        for day, n in extra.get(k, {}).items():
            out[k][day] = out[k].get(day, 0) + n
        out[k] = dict(sorted(out[k].items())[-10:])  # keep the last 10 days
    out["github_runs"] = {**base.get("github_runs", {}), **extra.get("github_runs", {})}
    return out


USAGE_DELTA = STATE_FILE.with_name("notes_usage_delta.json")


def save_state() -> None:
    try:
        USAGE_DELTA.write_text(json.dumps(USAGE))
    except Exception:
        pass
    try:
        old = set(json.loads(STATE_FILE.read_text())) if STATE_FILE.exists() else set()
        STATE_FILE.write_text(json.dumps(sorted(old | WRITTEN)))
    except Exception as e:
        log.warning("Couldn't save run state: %s", e)
THUMB_WIDTH = 480


def thumb_name(shortcode: str) -> str:
    return f"{shortcode}.jpg" if shortcode else ""


def thumb_exists(shortcode: str) -> bool:
    return bool(shortcode) and (THUMBS_DIR / thumb_name(shortcode)).exists()


def resize_to_thumb(src: Path, shortcode: str, seek: float | None = None) -> bool:
    """Makes a small JPEG cover in Thumbnails/. Works for images and videos."""
    THUMBS_DIR.mkdir(parents=True, exist_ok=True)
    out = THUMBS_DIR / thumb_name(shortcode)
    cmd = ["ffmpeg", "-y", "-loglevel", "error"]
    if seek is not None:
        cmd += ["-ss", f"{seek:.2f}"]
    cmd += ["-i", str(src), "-frames:v", "1", "-vf", f"scale='min({THUMB_WIDTH},iw)':-2", "-q:v", "5", str(out)]
    run(cmd)
    ok = out.exists() and out.stat().st_size > 1000
    if ok:
        mark_written(out)
    return ok


def make_thumbnail(post: Post, workdir: Path) -> str:
    """Cover image for the note: the post's cover if we have it, else the first slide or a frame of the video."""
    if not post.shortcode:
        return ""
    if post.cover_url:
        try:
            cover = download_file(post.cover_url, workdir / "cover.jpg")
            if resize_to_thumb(cover, post.shortcode):
                return thumb_name(post.shortcode)
        except Exception as e:
            log.info("Couldn't download the cover (%s). Using the media instead.", str(e)[:120])
    if post.media:
        first = post.media[0]
        if first.suffix.lower() in VIDEO_EXT:
            dur = video_duration(first)
            seek = min(1.0, dur * 0.2) if dur > 0 else 0.0
            ok = resize_to_thumb(first, post.shortcode, seek)
        else:
            ok = resize_to_thumb(first, post.shortcode)
        if ok:
            return thumb_name(post.shortcode)
    return ""


def thumb_embed(name: str) -> str:
    return f"![[Thumbnails/{name}|320]]" if name else ""


def render_markdown(post: Post, ex: Extraction, note: dict, thumb: str = "") -> str:
    summary = " ".join(str(note.get("summary", "")).split())
    lines = [
        "---",
        f"title: {yaml_str(note['title'])}",
        f"category: {yaml_str(note['category'])}",
        f"summary: {yaml_str(summary)}",
        f"source: {yaml_str(post.url) if post.url else 'null'}",
        f"creator: {yaml_str('@' + post.author) if post.author else 'null'}",
        f"shortcode: {yaml_str(post.shortcode) if post.shortcode else 'null'}",
        f"type: {ex.kind}",
        f"thumbnail: {yaml_str('Thumbnails/' + thumb) if thumb else 'null'}",
        f"posted: {post.posted or 'null'}",
        f"processed: {dt.date.today().isoformat()}",
        "tags:",
        *[f"  - {t}" for t in clean_tags(note["tags"])],
        "---",
        f"# {note['title']}",
        "",
    ]
    if thumb:
        lines += [thumb_embed(thumb), ""]
    lines += [
        f"Category: [[{note['category']}]]" + (f" · Creator: @{post.author}" if post.author else ""),
        "",
    ]
    if summary:
        lines += ["## Summary", summary, ""]
    if note.get("items"):
        lines += ["## What's in it", ""]
        for i, it in enumerate(note["items"], 1):
            head = f"**{it['name']}**"
            meta = ", ".join(x for x in (it.get("by"), it.get("date")) if x)
            if meta:
                head += f" ({meta})"
            lines.append(f"{i}. {head}" + (f": {it['what']}" if it.get("what") else ""))
            if it.get("link"):
                lines.append(f"   Link: {it['link']}")
        lines.append("")
    if note.get("key_points"):
        lines += ["## Key points", *[f"- {str(p).strip()}" for p in note["key_points"]], ""]
    if note.get("actions"):
        lines += ["## Things to try", *[f"- [ ] {str(a).strip()}" for a in note["actions"]], ""]
    if note.get("gaps"):
        lines += ["## Missing or unclear", *[f"- {str(g).strip()}" for g in note["gaps"]], ""]
    if post.url:
        lines += ["## Source", f"[Open original]({post.url})", ""]
    lines += ["## Full extracted text", ""]
    raw_before = len(lines)
    if post.caption:
        lines.append(callout("Caption", post.caption))
    if ex.transcript_parts:
        lines.append(callout("Transcript", "\n\n".join(ex.transcript_parts)))
    if ex.on_screen:
        lines.append(callout("On-screen text", "\n\n---\n\n".join(ex.on_screen)))
    if ex.slides:
        lines.append(callout("Slides", "\n\n---\n\n".join(ex.slides)))
    if len(lines) == raw_before:
        lines.append("_No text could be extracted. Open the original to check._\n")
    return "\n".join(lines)


# --------------------------------------------------------------------------- vault files

def frontmatter(text: str) -> dict:
    """Reads the simple frontmatter these notes use."""
    m = re.match(r"^---\n(.*?)\n---", text, flags=re.S)
    if not m:
        return {}
    out: dict = {}
    for line in m.group(1).splitlines():
        kv = re.match(r"^([A-Za-z_]+): ?(.*)$", line)
        if not kv:
            continue
        key, val = kv.group(1), kv.group(2).strip()
        if val in ("", "null"):
            out[key] = ""
            continue
        if val.startswith('"'):
            try:
                val = json.loads(val)
            except Exception:
                val = val.strip('"')
        out[key] = val
    return out


def note_files() -> list[Path]:
    """Every note in the category folders (not the index pages)."""
    files = []
    for c in CATEGORIES:
        folder = REPO_DIR / c
        if folder.is_dir():
            files += [p for p in folder.glob("*.md") if p.stem != c]
    if QUIET:
        hide(*(p.stem for p in files))
    return files


def find_by_shortcode(shortcode: str) -> list[Path]:
    if not shortcode:
        return []
    needle = f"shortcode: {yaml_str(shortcode)}"
    return [p for p in note_files() if needle in p.read_text(encoding="utf-8", errors="ignore")[:2000]]


def find_by_source(url: str) -> list[Path]:
    """Notes whose source link matches this one, after normalising both."""
    want = normalise_url(url)
    out = []
    for p in note_files():
        src = frontmatter(p.read_text(encoding="utf-8", errors="ignore")[:3000]).get("source", "")
        if src and normalise_url(src) == want:
            out.append(p)
    return out


def safe_filename(title: str) -> str:
    name = re.sub(r'[\\/:*?"<>|#^\[\]]', "", title)
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name[:80].strip() or "Untitled"


def save_note(markdown: str, title: str, shortcode: str, category: str) -> Path:
    for old in find_by_shortcode(shortcode):  # processed again: replace the old page
        old.unlink()
    folder = REPO_DIR / category
    folder.mkdir(parents=True, exist_ok=True)
    name = safe_filename(title)
    if name in CATEGORIES or f"{name}.md" in GENERATED:
        name += " (note)"
    path = folder / f"{name}.md"
    if path.exists() or any(p.stem == name for p in note_files()):  # same title as another note
        path = folder / f"{name} ({shortcode or int(time.time())}).md"
    path.write_text(markdown, encoding="utf-8")
    mark_written(path)
    return path


def migrate_old_notes() -> int:
    """Moves notes from the old notes/ folder into category folders."""
    if not OLD_NOTES_DIR.is_dir():
        return 0
    moved = 0
    for p in OLD_NOTES_DIR.glob("*.md"):
        if p.name == "README.md":
            p.unlink()
            continue
        text = p.read_text(encoding="utf-8", errors="ignore")
        fm = frontmatter(text)
        category = pick_category(fm.get("category", ""), fm.get("topic", ""))
        shortcode = fm.get("shortcode") or (re.search(r"\(([A-Za-z0-9_-]+)\)$", p.stem) or [None, ""])[1]
        extra = f"category: {yaml_str(category)}\n" if "category:" not in text[:1500] else ""
        if shortcode and "shortcode:" not in text[:1500]:
            extra += f"shortcode: {yaml_str(shortcode)}\n"
        if extra:
            text = text.replace("---\n", "---\n" + extra, 1)
        title = fm.get("title") or p.stem
        text = text.replace(f"# {title}\n", f"# {title}\n\nCategory: [[{category}]]\n", 1) if "Category: [[" not in text else text
        save_note(text, title, shortcode, category)
        p.unlink()
        moved += 1
    shutil.rmtree(OLD_NOTES_DIR, ignore_errors=True)
    if moved:
        log.info("Moved %d older note(s) into category folders", moved)
    return moved


def collect() -> dict[str, list[dict]]:
    by_cat: dict[str, list[dict]] = {c: [] for c in CATEGORIES}
    for p in note_files():
        fm = frontmatter(p.read_text(encoding="utf-8", errors="ignore"))
        cat = p.parent.name
        by_cat[cat].append({
            "link": p.stem, "title": fm.get("title") or p.stem, "summary": fm.get("summary", ""),
            "creator": fm.get("creator", ""), "processed": fm.get("processed", ""), "type": fm.get("type", ""),
            "category": cat,
        })
    for items in by_cat.values():
        items.sort(key=lambda n: (n["processed"], n["title"]), reverse=True)
    return by_cat


def link(n: dict) -> str:
    return f"[[{n['link']}|{n['title']}]]" if n["link"] != n["title"] else f"[[{n['link']}]]"


def build_indexes() -> None:
    """Home page, one page per category, and a full index. All made automatically, don't edit them."""
    by_cat = collect()
    total = sum(len(v) for v in by_cat.values())
    auto = "_This page updates automatically. Edits here get replaced._"

    for cat, items in by_cat.items():
        if not items:
            continue
        lines = ["---", f"category: {yaml_str(cat)}", "type: category-index", "---", f"# {cat}", "",
                 f"{len(items)} saved note{'s' if len(items) != 1 else ''}. Newest first. [[Home|Back to Home]]", ""]
        for n in items:
            lines.append(f"- {link(n)}" + (f" ({n['creator']})" if n["creator"] else ""))
            if n["summary"]:
                lines.append(f"  {n['summary']}")
        lines += ["", auto, ""]
        (REPO_DIR / cat / f"{cat}.md").write_text("\n".join(lines), encoding="utf-8")

    latest = sorted((n for items in by_cat.values() for n in items),
                    key=lambda n: (n["processed"], n["title"]), reverse=True)[:15]
    home = [f"# {VAULT_NAME}", "", f"{total} notes from saved posts.", "", "## Categories", ""]
    home += [f"- [[{c}]] ({len(by_cat[c])})" for c in CATEGORIES if by_cat[c]]
    home += ["", "## Latest", ""]
    home += [f"- {link(n)} · [[{n['category']}]]" for n in latest]
    home += ["", "Everything in one list: [[Index]]", "", auto, ""]
    (REPO_DIR / "Home.md").write_text("\n".join(home), encoding="utf-8")

    index = ["# Index", "", "Every saved post with a one-line summary, grouped by category.", ""]
    for c in CATEGORIES:
        if not by_cat[c]:
            continue
        index += [f"## {c}", ""]
        for n in by_cat[c]:
            index.append(f"- {link(n)}: {n['summary'] or '(no summary)'}")
        index.append("")
    index += [auto, ""]
    (REPO_DIR / "Index.md").write_text("\n".join(index), encoding="utf-8")

    # Remove index pages for categories that are now empty
    for c in CATEGORIES:
        page = REPO_DIR / c / f"{c}.md"
        if page.exists() and not by_cat[c]:
            page.unlink()
            try:
                page.parent.rmdir()
            except OSError:
                pass


# --------------------------------------------------------------------------- rewriting old notes

def callouts(text: str) -> dict[str, str]:
    """Reads the collapsed raw-text blocks back out of a note."""
    out: dict[str, str] = {}
    current = None
    for line in text.splitlines():
        m = re.match(r"^> \[!quote\]-? (.+)$", line)
        if m:
            current = m.group(1).strip()
            out[current] = ""
        elif current and line.startswith(">"):
            out[current] += line[2:] if line.startswith("> ") else line[1:]
            out[current] += "\n"
        else:
            current = None
    return {k: v.strip() for k, v in out.items()}


def rewrite_notes(target: str, s: Settings, ai: WorkersAI, gem: "Gemini | None" = None) -> int:
    """Re-writes notes from the text they already saved. No new download.
    Given a link with no note yet, it processes that post from scratch."""
    target = target.strip()
    is_link = target.startswith("http")
    code = key_for(target) if is_link else target
    hide(target, code)
    if code.lower() == "all":
        files = note_files()
    else:
        files = find_by_shortcode(code) or (find_by_source(target) if is_link else [])
    if not files:
        if is_link:
            log.info("No note yet for %s, so making one from scratch", target)
            post, ex, note, md = process_url(target, s, ai, gem)
            path = save_note(md, note["title"], post.shortcode or code, note["category"])
            log.info("Saved %s/%s", note["category"], path.name)
            return 1
        log.warning("No note found for %s", target)
        return 0
    done = 0
    for p in files:
        text = p.read_text(encoding="utf-8", errors="ignore")
        fm = frontmatter(text)
        raw = callouts(text)
        post = Post(url=fm.get("source", ""), shortcode=fm.get("shortcode", ""),
                    author=fm.get("creator", "").lstrip("@"), caption=raw.get("Caption", ""),
                    posted=fm.get("posted", ""), source="rewrite")
        ex = Extraction(kind=fm.get("type") or "post")
        if raw.get("Transcript"):
            ex.transcript_parts = [raw["Transcript"]]
        if raw.get("On-screen text"):
            ex.on_screen = [t for t in raw["On-screen text"].split("\n\n---\n\n") if t.strip()]
        if raw.get("Slides"):
            ex.slides = [t for t in raw["Slides"].split("\n\n---\n\n") if t.strip()]
        try:
            note = compose_note(post, ex, s, ai, gem)
        except DailyLimitReached:
            log.warning("Daily AI allowance used up. Run the rewrite again after %s.", local_reset())
            break
        if not note.get("summary") and not note.get("key_points"):
            log.warning("Kept the old version of %s (the AI didn't return a note)", p.name)
            continue
        md = render_markdown(post, ex, note, thumb_name(post.shortcode) if thumb_exists(post.shortcode) else "")
        p.unlink()
        new = save_note(md, note["title"], post.shortcode, note["category"])
        log.info("Rewrote %s -> %s/%s", p.name, note["category"], new.name)
        done += 1
        time.sleep(1)
    return done


def fetch_cover_only(url: str, workdir: Path, s: Settings) -> Post | None:
    """Gets just the cover image for an older note. Uses the plugin's fetch_cover if it has one."""
    return fetch_post(url, workdir, cover_only=True)


def add_thumbnails(s: Settings, limit: int = 1000) -> int:
    """Adds a cover image to notes that don't have one yet."""
    added = 0
    for p in note_files():
        if added >= limit:
            break
        text = p.read_text(encoding="utf-8", errors="ignore")
        if "![[Thumbnails/" in text:
            continue
        fm = frontmatter(text)
        code, url = fm.get("shortcode", ""), fm.get("source", "")
        if not code or not url:
            continue
        if not thumb_exists(code):
            with tempfile.TemporaryDirectory(prefix="thumb_") as tmp:
                try:
                    post = fetch_cover_only(url, Path(tmp), s)
                    if post is None or not make_thumbnail(post, Path(tmp)):
                        log.info("No cover found for %s", p.name)
                        continue
                except Exception as e:
                    log.warning("Cover failed for %s: %s", p.name, str(e)[:150])
                    continue
        name = thumb_name(code)
        title_line = re.search(r"^# .*$", text, flags=re.M)
        if not title_line:
            continue
        i = title_line.end()
        text = text[:i] + "\n\n" + thumb_embed(name) + text[i:]
        if re.search(r"^thumbnail:", text, flags=re.M):
            text = re.sub(r"^thumbnail:.*$", f"thumbnail: {yaml_str('Thumbnails/' + name)}", text, count=1, flags=re.M)
        else:
            text = text.replace("\ntype:", f"\nthumbnail: {yaml_str('Thumbnails/' + name)}\ntype:", 1)
        p.write_text(text, encoding="utf-8")
        mark_written(p)
        log.info("Added cover to %s", p.name)
        added += 1
    return added


def git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(REPO_DIR), *args], capture_output=True, text=True)


def replay_on_remote() -> int:
    """Another run pushed first. Start from its version and put this run's notes back on top."""
    paths = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else []
    keep: dict[str, bytes] = {}
    for rel in paths:
        f = REPO_DIR / rel
        if f.exists():
            keep[rel] = f.read_bytes()
    if git("fetch", "-q", "origin", "main").returncode != 0:
        log.error("git fetch failed")
        return 1
    git("reset", "-q", "--hard", "origin/main")
    migrate_old_notes()
    if "usage.json" in keep:  # add this run's counts to the other run's, don't overwrite them
        keep.pop("usage.json")
        theirs = json.loads(USAGE_FILE.read_text()) if USAGE_FILE.exists() else {}
        delta = json.loads(USAGE_DELTA.read_text()) if USAGE_DELTA.exists() else {}
        USAGE_FILE.write_text(json.dumps(merged_usage(theirs, delta), indent=1))
    for rel, data in keep.items():
        dest = REPO_DIR / rel
        if dest.suffix == ".md":
            code = frontmatter(data.decode("utf-8", "ignore")).get("shortcode", "")
            for other in find_by_shortcode(code):  # the other run's copy of the same post
                if other != dest:
                    other.unlink()
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
    build_indexes()
    log.info("Replayed %d file(s) on top of the latest notes", len(keep))
    return 0


# --------------------------------------------------------------------------- Notion

class Notion:
    """Puts each note in your Notion database. The notes repo keeps the Markdown backup."""
    API = "https://api.notion.com/v1"
    VERSION = "2025-09-03"

    def __init__(self, s: Settings):
        self.token, self.ds = s.notion_token, s.notion_ds.replace("collection://", "")
        self._codes: dict[str, str] | None = None

    def call(self, method: str, path: str, body=None, raw: bytes | None = None, ctype: str = "") -> dict:
        req = urllib.request.Request(self.API + path, method=method,
                                     data=raw if raw is not None else (json.dumps(body).encode() if body is not None else None))
        req.add_header("authorization", f"Bearer {self.token}")
        req.add_header("notion-version", self.VERSION)
        req.add_header("content-type", ctype or "application/json")
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=60) as res:
                    time.sleep(0.35)  # Notion allows about 3 requests a second
                    return json.loads(res.read().decode() or "{}")
            except urllib.error.HTTPError as e:
                detail = e.read().decode(errors="ignore")
                if e.code in (429, 500, 502, 503, 504) and attempt < 3:
                    time.sleep(float(e.headers.get("retry-after") or 2 * (attempt + 1)))
                    continue
                raise RuntimeError(f"Notion {e.code}: {detail[:400]}") from e
        return {}

    def existing(self) -> dict[str, str]:
        """shortcode -> page id for every page already in the database."""
        if self._codes is None:
            self._codes, cursor = {}, None
            while True:
                body = {"page_size": 100, **({"start_cursor": cursor} if cursor else {})}
                res = self.call("POST", f"/data_sources/{self.ds}/query", body)
                for pg in res.get("results", []):
                    rt = (pg.get("properties", {}).get("Shortcode", {}) or {}).get("rich_text") or []
                    code = "".join(t.get("plain_text") or (t.get("text") or {}).get("content", "") for t in rt).strip()
                    if code:
                        self._codes[code] = pg["id"]
                if not res.get("has_more"):
                    break
                cursor = res.get("next_cursor")
        return self._codes

    def upload_image(self, path: Path) -> str:
        up = self.call("POST", "/file_uploads", {"mode": "single_part", "filename": path.name, "content_type": "image/jpeg"})
        boundary = f"----notes{int(time.time() * 1000)}"
        body = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{path.name}"\r\n'
                f"Content-Type: image/jpeg\r\n\r\n").encode() + path.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
        self.call("POST", f"/file_uploads/{up['id']}/send", raw=body, ctype=f"multipart/form-data; boundary={boundary}")
        return up["id"]


def rich(text: str, bold: bool = False) -> list[dict]:
    """Plain text to Notion rich text, keeping **bold** and links. Splits long text into 2,000-character pieces."""
    out: list[dict] = []
    pattern = re.compile(r"\*\*(.+?)\*\*|\[([^\]]+)\]\((https?://[^)\s]+)\)|(https?://[^\s)]+)")
    pos = 0
    def add(t: str, b: bool = False, url: str = ""):
        for i in range(0, len(t), 1900):
            piece = {"type": "text", "text": {"content": t[i:i + 1900]}}
            if url:
                piece["text"]["link"] = {"url": url}
            if b or bold:
                piece["annotations"] = {"bold": True}
            out.append(piece)
    for m in pattern.finditer(text):
        if m.start() > pos:
            add(text[pos:m.start()])
        if m.group(1):
            add(m.group(1), b=True)
        elif m.group(2):
            add(m.group(2), url=m.group(3))
        else:
            add(m.group(4), url=m.group(4))
        pos = m.end()
    if pos < len(text):
        add(text[pos:])
    return out[:100] or [{"type": "text", "text": {"content": ""}}]


def block(kind: str, text: str = "", **extra) -> dict:
    return {"object": "block", "type": kind, kind: {"rich_text": rich(text), **extra}}


def note_to_blocks(text: str, embed: bool = True) -> list[dict]:
    """Turns one of our Markdown notes into Notion blocks."""
    fm = frontmatter(text)
    body = re.sub(r"^---\n.*?\n---\n?", "", text, count=1, flags=re.S)
    raw = callouts(body)
    body = body.split("## Full extracted text")[0]
    blocks: list[dict] = []
    if fm.get("summary"):
        blocks.append({"object": "block", "type": "callout", "callout": {
            "rich_text": rich(fm["summary"]), "icon": {"type": "emoji", "emoji": "💡"}, "color": "gray_background"}})
    section = ""
    for line in body.splitlines():
        line = line.rstrip()
        if not line or line.startswith("# ") or line.startswith("![[") or line.startswith("Category: [["):
            continue
        if line.startswith("## "):
            section = line[3:].strip()
            if section in ("Summary",):
                continue
            blocks.append(block("heading_2", section))
            continue
        if section == "Summary":
            continue
        if section == "Source":
            url = fm.get("source", "")
            if url and embed:
                blocks.append({"object": "block", "type": "embed", "embed": {"url": url}})
            if url:
                blocks.append(block("paragraph", f"[Open original]({url})"))
            section = "_done_source"
            continue
        if section == "_done_source":
            continue
        if line.startswith("- [ ] ") or line.startswith("- [x] "):
            blocks.append(block("to_do", line[6:], checked=line.startswith("- [x]")))
        elif line.startswith("- "):
            blocks.append(block("bulleted_list_item", line[2:]))
        elif re.match(r"^\d+\. ", line):
            blocks.append(block("numbered_list_item", re.sub(r"^\d+\. ", "", line)))
        elif line.strip().startswith("Link: ") and blocks:
            blocks.append(block("paragraph", "↳ " + line.strip()[6:]))
        else:
            blocks.append(block("paragraph", line))
    if raw:
        blocks.append({"object": "block", "type": "divider", "divider": {}})
        blocks.append(block("heading_3", "Full extracted text"))
        for title, content in raw.items():
            kids = [block("paragraph", content[i:i + 1900]) for i in range(0, len(content), 1900)][:90]
            blocks.append({"object": "block", "type": "toggle", "toggle": {"rich_text": rich(title, bold=True), "children": kids}})
    return blocks


def notion_properties(fm: dict, category: str, title: str) -> dict:
    tags = []
    m = re.search(r"^tags:\n((?:  - .*\n?)+)", fm.get("_raw", ""), flags=re.M)
    if m:
        tags = [t.strip()[2:].strip() for t in m.group(1).splitlines() if t.strip().startswith("- ")]
    tags = [t.replace(",", " ")[:90] for t in tags if t and t not in (BASE_TAG, "saved")][:10]
    def date(v: str):
        return {"date": {"start": v}} if re.match(r"^\d{4}-\d{2}-\d{2}$", v or "") else {"date": None}
    kind = (fm.get("type") or "post").capitalize()
    props = {
        "Name": {"title": rich(title)[:1]},
        "Category": {"select": {"name": category}},
        "Summary": {"rich_text": rich(fm.get("summary", ""))[:5]},
        "Tags": {"multi_select": [{"name": t} for t in tags]},
        "Creator": {"rich_text": rich(fm.get("creator", ""))[:1]},
        "Type": {"select": {"name": kind if kind.lower() in ("video", "carousel", "image", VIDEO_LABEL.lower()) else "Post"}},
        "Source": {"url": fm.get("source") or None},
        "Posted": date(fm.get("posted", "")),
        "Saved": date(fm.get("processed", "")),
        "Shortcode": {"rich_text": rich(fm.get("shortcode", ""))[:1]},
        "Status": {"select": {"name": "Ready"}},
    }
    return props


def sync_to_notion(nt: Notion, path: Path, replace: bool = True) -> bool:
    """Creates (or replaces) this note's page in Notion. Keeps the Read tick if it was replaced."""
    text = path.read_text(encoding="utf-8", errors="ignore")
    fm = frontmatter(text)
    fm["_raw"] = text
    code = fm.get("shortcode", "")
    if not code:
        return False
    title = fm.get("title") or path.stem
    hide(title, code, fm.get("creator", "").lstrip("@"))
    category = path.parent.name if path.parent.name in CATEGORIES else pick_category(fm.get("category", ""))
    old_id = nt.existing().get(code)
    if old_id and not replace:
        return False
    was_read = False
    if old_id:
        try:
            old = nt.call("GET", f"/pages/{old_id}")
            was_read = bool(old.get("properties", {}).get("Read", {}).get("checkbox"))
            nt.call("PATCH", f"/pages/{old_id}", {"in_trash": True})
        except Exception as e:
            log.warning("Couldn't replace the old Notion page for %s: %s", path.name, str(e)[:150])
    props = notion_properties(fm, category, title)
    props["Read"] = {"checkbox": was_read}
    page: dict = {"parent": {"type": "data_source_id", "data_source_id": nt.ds}, "properties": props,
                  "icon": {"type": "emoji", "emoji": CATEGORY_ICONS.get(category, "📌")}}
    thumb = THUMBS_DIR / thumb_name(code)
    if thumb.exists():
        try:
            page["cover"] = {"type": "file_upload", "file_upload": {"id": nt.upload_image(thumb)}}
        except Exception as e:
            log.warning("Cover upload to Notion failed: %s", str(e)[:150])
    for embed in (True, False):
        blocks = note_to_blocks(text, embed=embed)
        page["children"] = blocks[:100]
        try:
            created = nt.call("POST", "/pages", page)
            break
        except RuntimeError as e:
            if embed and "embed" in str(e).lower():
                continue  # embed refused, use a plain link instead
            raise
    for i in range(100, len(blocks), 100):
        nt.call("PATCH", f"/blocks/{created['id']}/children", {"children": blocks[i:i + 100]})
    nt.existing()[code] = created["id"]
    log.info("Notion: %s %s", "updated" if old_id else "added", title)
    return True


def notion_failed(s: Settings, url: str, reason: str) -> None:
    """Adds a red "Failed" card so a post that couldn't be processed is never missed.
    If it works later (Retry on the inbox page), the real note replaces this card."""
    if not (s.notion_token and s.notion_ds):
        return
    nt = Notion(s)
    code = key_for(url)
    hide(code)
    if code and code in nt.existing():
        return  # already has a card
    nt.call("POST", "/pages", {
        "parent": {"type": "data_source_id", "data_source_id": nt.ds},
        "icon": {"type": "emoji", "emoji": "⚠️"},
        "properties": {
            "Name": {"title": rich(f"Couldn't process this post")},
            "Status": {"select": {"name": "Failed"}},
            "Category": {"select": {"name": "Other"}},
            "Summary": {"rich_text": rich(reason[:300])},
            "Source": {"url": url},
            "Shortcode": {"rich_text": rich(code)},
            "Saved": {"date": {"start": dt.date.today().isoformat()}},
        },
        "children": [
            block("paragraph", f"Reason: {reason[:1500]}"),
            block("paragraph", f"[Open original]({url})"),
            block("paragraph", "To try again, tap Retry next to this link on your inbox page. "
                               "If the post is private or deleted, you can delete this card."),
        ],
    })
    log.info("Notion: added a Failed card for %s", url)


def github_repo_is_public() -> bool:
    token, repo = env("GITHUB_TOKEN", ""), env("GITHUB_REPOSITORY", "")
    if not repo:
        return False
    try:
        res = http(f"https://api.github.com/repos/{repo}", headers={
            "accept": "application/vnd.github+json", **({"authorization": f"Bearer {token}"} if token else {})}, timeout=30)
        return res.get("private") is False
    except Exception:
        return False


def github_minutes_this_month(known: dict) -> tuple[float, dict]:
    """Adds up this month's run time from GitHub. Each run is rounded up to the minute, like GitHub bills."""
    token, repo = env("GITHUB_TOKEN", ""), env("GITHUB_REPOSITORY", "")
    if not (token and repo):
        return -1, known
    month = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m")
    runs = {k: v for k, v in known.items() if v.get("month") == month}
    page = 1
    while page <= 20:
        try:
            res = http(f"https://api.github.com/repos/{repo}/actions/runs?per_page=100&page={page}&created=>={month}-01",
                       headers={"authorization": f"Bearer {token}", "accept": "application/vnd.github+json"})
        except Exception as e:
            log.warning("Couldn't read GitHub run times: %s", str(e)[:150])
            break
        items = res.get("workflow_runs", [])
        for r in items:
            start, end = r.get("run_started_at"), r.get("updated_at")
            if not start or not end:
                continue
            secs = (dt.datetime.fromisoformat(end.replace("Z", "+00:00"))
                    - dt.datetime.fromisoformat(start.replace("Z", "+00:00"))).total_seconds()
            if r.get("status") != "completed":
                secs = max(secs, 90)  # this run, still going
            runs[str(r["id"])] = {"month": month, "min": max(1, -(-int(secs) // 60))}
        if len(items) < 100:
            break
        page += 1
    return float(sum(v["min"] for v in runs.values())), runs


def update_allowance(s: Settings) -> None:
    """Saves today's usage to usage.json, sends it to the inbox's Allowance card,
    and refreshes a 📟 callout on your Notion hub page if you have one."""
    from zoneinfo import ZoneInfo
    base = json.loads(USAGE_FILE.read_text()) if USAGE_FILE.exists() else {}
    usage = merged_usage(base, USAGE)
    gh_unlimited = github_repo_is_public()  # public repos get unlimited Actions minutes
    if gh_unlimited:
        gh_used = -1
    else:
        gh_used, usage["github_runs"] = github_minutes_this_month(usage.get("github_runs", {}))
    USAGE_FILE.write_text(json.dumps(usage, indent=1))
    mark_written(USAGE_FILE)
    # Send the numbers to the inbox, which draws the Allowance card
    if s.inbox_token:
        try:
            http(f"{s.inbox_url}/api/usage", "POST", {
                "updated": dt.datetime.now(dt.timezone.utc).isoformat(),
                "gh_used": gh_used if gh_used >= 0 else None, "gh_total": GITHUB_MONTHLY_MINUTES,
                "gh_unlimited": gh_unlimited,
                "gh_month": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m"),
                "gemini_used": int(usage["gemini"].get(pacific_day(), 0)), "gemini_total": GEMINI_DAILY,
                "gemini_day": pacific_day(),
                "groq_seconds": usage["groq_seconds"].get(utc_day(), 0), "groq_total_seconds": GROQ_DAILY_SECONDS,
                "groq_day": utc_day(),
                "posts_today": int(usage["posts"].get(utc_day(), 0)), "posts_day": utc_day(),
            }, headers={"authorization": f"Bearer {s.inbox_token}"}, timeout=30)
            log.info("Sent usage to the Allowance card")
        except Exception as e:
            log.warning("Couldn't update the Allowance card: %s", str(e)[:150])
    if not (s.notion_token and s.notion_hub):
        return
    gem_left = max(0, GEMINI_DAILY - int(usage["gemini"].get(pacific_day(), 0)))
    groq_left = max(0, GROQ_DAILY_SECONDS - usage["groq_seconds"].get(utc_day(), 0)) / 60
    posts_today = int(usage["posts"].get(utc_day(), 0))
    try:
        now_local = dt.datetime.now(ZoneInfo(LOCAL_TZ))
    except Exception:
        now_local = dt.datetime.now(dt.timezone.utc)
    lines = [f"Allowance (updated {now_local.strftime('%-I:%M%p').lower()}, {now_local.strftime('%-d %b')})"]
    if gh_unlimited:
        lines.append(f"You can send about **{gem_left} more posts today**. Gemini's daily cap is the main limit.")
        lines.append("GitHub: unlimited minutes (public repo).")
    elif gh_used >= 0:
        gh_left = max(0, GITHUB_MONTHLY_MINUTES - gh_used)
        nxt = (now_local.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
        days_left = max(1, (nxt.date() - now_local.date()).days)
        today = int(min(gem_left, gh_left))
        lines.append(f"You can send about **{today} more posts today**, or about **{int(gh_left // days_left)} a day** to last until {nxt.strftime('%-d %b')}.")
        lines.append(f"GitHub: {int(gh_left):,} of {GITHUB_MONTHLY_MINUTES:,} minutes left this month. This is the main limit.")
    lines.append(f"Gemini: {gem_left} of {GEMINI_DAILY} notes left today. Resets at {local_reset(pacific=True)}.")
    lines.append(f"Groq: about {int(groq_left)} of {GROQ_DAILY_SECONDS // 60} audio minutes left today.")
    lines.append(f"Processed today: {posts_today}. Updates after every run.")
    nt = Notion(s)
    hub = s.notion_hub.replace("-", "")
    try:
        kids = nt.call("GET", f"/blocks/{hub}/children?page_size=50").get("results", [])
    except Exception as e:
        log.warning("Couldn't open the Notion hub page (add your Notion connection to it): %s", str(e)[:150])
        return
    for b in kids:
        if b.get("type") == "callout" and (b["callout"].get("icon") or {}).get("emoji") == "📟":
            rt = rich(lines[0], bold=True) + [{"type": "text", "text": {"content": "\n"}}]
            for i, line in enumerate(lines[1:]):
                rt += rich(line) + ([{"type": "text", "text": {"content": "\n"}}] if i < len(lines) - 2 else [])
            nt.call("PATCH", f"/blocks/{b['id']}", {"callout": {"rich_text": rt}})
            log.info("Updated the Allowance box")
            return
    log.info("No Allowance box found on the hub page")


def sync_notion(s: Settings, changed: set[str], backfill: int = 25) -> int:
    """Sends notes changed in this run to Notion, plus a few older notes Notion doesn't have yet."""
    if not (s.notion_token and s.notion_ds):
        return 0
    nt = Notion(s)
    done = 0
    try:
        have = nt.existing()
    except Exception as e:
        log.warning("Couldn't reach Notion (%s). Is your Notion connection added to the database?", str(e)[:200])
        return 0
    notes = {str(p.relative_to(REPO_DIR)): p for p in note_files()}
    for rel in sorted(changed):
        if rel in notes:
            try:
                done += sync_to_notion(nt, notes[rel], replace=True)
            except Exception as e:
                log.warning("Notion sync failed for %s: %s", rel, str(e)[:200])
    missing = [p for rel, p in notes.items() if rel not in changed
               and frontmatter(p.read_text(encoding="utf-8", errors="ignore")[:3000]).get("shortcode") not in have]
    for p in missing[:backfill]:
        try:
            done += sync_to_notion(nt, p, replace=False)
        except Exception as e:
            log.warning("Notion sync failed for %s: %s", p.name, str(e)[:200])
    return done


# --------------------------------------------------------------------------- main

def process_url(url: str, s: Settings, ai: WorkersAI, gem: "Gemini | None" = None) -> tuple[Post, Extraction, dict, str]:
    hide(url, key_for(url))
    with tempfile.TemporaryDirectory(prefix="notes_") as tmp:
        workdir = Path(tmp)
        post = fetch_post(url, workdir)
        if post is None or not (post.media or post.caption):
            raise RuntimeError("Couldn't fetch this post. It may be private or deleted, "
                               "or your fetcher plugin doesn't handle this kind of link.")
        log.info("Fetched with %s: %d media file(s)", post.source, len(post.media))
        ex = extract(post, workdir, s, ai, visuals=False)  # speech first (Groq, else Cloudflare)
        note = None
        if gem and gem.ok and post.media:
            images = gather_images(post, workdir, s)
            log.info("Reading %d image(s) and writing the note with Gemini", len(images))
            note = gemini_note(post, ex, s, gem, images)
        if note is None:
            visual = extract(post, workdir, s, ai, audio=False)
            ex.slides, ex.on_screen = visual.slides, visual.on_screen
            note = compose_note(post, ex, s, ai, gem)
        thumb = ""
        try:
            thumb = make_thumbnail(post, workdir)
        except Exception as e:
            log.warning("No thumbnail: %s", str(e)[:150])
        return post, ex, note, render_markdown(post, ex, note, thumb)


def rewrite_from_event() -> str:
    """The rewrite input, read from the event file instead of an environment variable,
    so a link typed into "Run workflow" never shows in the public log."""
    path = env("GITHUB_EVENT_PATH", "")
    if not path or not Path(path).exists():
        return ""
    try:
        event = json.loads(Path(path).read_text())
    except Exception:
        return ""
    value = (event.get("inputs") or {}).get("rewrite") or (event.get("client_payload") or {}).get("rewrite") or ""
    return str(value).strip()


def main() -> int:
    parser = argparse.ArgumentParser(description="Turn shared links into notes.")
    parser.add_argument("--url", help="Process one link without the inbox")
    parser.add_argument("--print", dest="print_only", action="store_true")
    parser.add_argument("--rewrite", help='Rewrite notes from their saved text: "all", or a post link or code')
    parser.add_argument("--reindex", action="store_true", help="Only rebuild Home, Index and category pages")
    parser.add_argument("--replay", action="store_true", help="Re-apply this run's notes on top of the latest repo")
    args = parser.parse_args()
    quiet_output()  # before logging starts, so every line goes through the filter
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    load_fetcher_env()
    if not args.rewrite:
        args.rewrite = rewrite_from_event()

    if args.replay:
        return replay_on_remote()
    import atexit
    atexit.register(save_state)  # so the save step knows which files this run made
    def to_notion():
        try:
            n = sync_notion(Settings(), set(WRITTEN))
            if n:
                log.info("Notion: %d page(s) sent", n)
        except Exception as e:
            log.warning("Notion sync skipped: %s", str(e)[:200])
        try:
            update_allowance(Settings())
        except Exception as e:
            log.warning("Allowance update skipped: %s", str(e)[:200])
    atexit.register(to_notion)  # runs before save_state (atexit is last-in, first-out)

    if args.reindex:
        migrate_old_notes()
        build_indexes()
        return 0

    s = Settings()
    missing = [k for k, v in {"CF_ACCOUNT_ID": s.cf_account, "CF_API_TOKEN": s.cf_token}.items() if not v]
    if not args.url and not args.rewrite and not (s.inbox_token and s.inbox_url):
        missing += [k for k, v in {"INBOX_URL": s.inbox_url, "INBOX_TOKEN": s.inbox_token}.items() if not v]
    if missing:
        log.error("Missing settings: %s. Add them as GitHub secrets.", ", ".join(missing))
        return 1
    ai = WorkersAI(s)
    gem = Gemini(s) if s.gemini_key else None
    log.info("Speech: %s. Notes: %s.", "Groq" if s.groq_key else "Cloudflare",
             "Gemini (Cloudflare as backup)" if gem else "Cloudflare")

    if args.rewrite and args.rewrite.strip().lower() == "notion":
        migrate_old_notes()
        WRITTEN.update(str(p.relative_to(REPO_DIR)) for p in note_files())  # sync step resends all of these
        log.info("Resending %d note(s) to Notion", len(WRITTEN))
        return 0

    if args.rewrite and args.rewrite.strip().lower() == "thumbnails":
        migrate_old_notes()
        n = add_thumbnails(s)
        build_indexes()
        log.info("Added %d cover image(s).", n)
        return 0

    if args.rewrite:
        migrate_old_notes()
        n = rewrite_notes(args.rewrite, s, ai, gem)
        build_indexes()
        log.info("Rewrote %d note(s).", n)
        return 0

    if args.url:
        post, ex, note, md = process_url(args.url, s, ai, gem)
        if args.print_only:
            print(md)
        else:
            migrate_old_notes()
            print(redact(str(save_note(md, note["title"], post.shortcode, note["category"]))))
            build_indexes()
        return 0

    changed = migrate_old_notes() > 0
    inbox = Inbox(s)
    deadline = time.time() + s.time_budget_min * 60
    done = 0
    while done < s.max_items and time.time() < deadline:
        items = inbox.pending(1)
        if not items:
            break
        item = items[0]
        log.info("Processing %s", item["url"])
        try:
            post, ex, note, md = process_url(item["url"], s, ai, gem)
            path = save_note(md, note["title"], post.shortcode or item.get("shortcode", ""), note["category"])
            # The inbox page only tracks status. The note itself lives in the notes repo and Notion.
            inbox.done(item["id"], title=note["title"], topic=note["category"],
                       note_path=str(path.relative_to(REPO_DIR)), note=f"{note['category']} / {path.name}")
            log.info("Saved %s", path.name)
            bump("posts")
            done += 1
        except DailyLimitReached:
            log.warning("Cloudflare's free daily AI allowance is used up. The rest will run after %s.", local_reset())
            inbox.release(item["id"], "Waiting for tomorrow's free AI allowance")
            break
        except Exception as e:
            log.exception("Failed: %s", item["url"])
            try:
                if inbox.failed(item["id"], str(e)) == "failed":  # out of tries
                    notion_failed(s, item["url"], str(e))
            except Exception as e2:
                log.warning("Couldn't record the failure: %s", str(e2)[:150])
        time.sleep(3)
    try:
        changed = add_thumbnails(s, limit=5) > 0 or changed  # slowly fills in covers for older notes
    except Exception as e:
        log.warning("Cover backfill skipped: %s", str(e)[:150])
    if done or changed:
        build_indexes()
    log.info("Finished. %d note(s) written.", done)
    return 0


if __name__ == "__main__":
    sys.exit(main())
