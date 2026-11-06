"""
Example fetcher plugin for Share to Notes.

Copy this to private/fetcher.py in your PRIVATE notes repo and fill in fetch().
The engine imports it at the start of each run.

Rules for the plugin:
  - fetch(url, workdir) must return a dict, or None if it can't handle this link.
    Returning None lets the engine try its built-in fetcher (direct links to media files).
  - Save any downloaded files inside workdir. It's deleted after each post.
  - Use logging, not print. Both are filtered in quiet mode, but logging keeps the format tidy.
  - Settings and keys come from the FETCHER_ENV secret (one KEY=value per line), which the engine
    loads into os.environ before calling you.
  - Extra Python packages go in private/requirements.txt next to this file.

Only fetch content you have the right to access, and follow the terms of the sites you use.
"""

from __future__ import annotations

import logging
import os
import urllib.request
from pathlib import Path

log = logging.getLogger("notes.fetcher")


def key(url: str) -> str:
    """Optional. A stable ID for this post, used to name thumbnails and match notes.
    Return "" to let the engine use a hash of the link."""
    return ""


def fetch(url: str, workdir: Path) -> dict | None:
    """Get the post's media and text. Return None if this plugin doesn't handle the link."""
    if not url.startswith("https://media.example.com/"):
        return None

    # Example: a site with an official API that returns a JSON description of a post.
    # api_key = os.environ.get("EXAMPLE_API_KEY", "")
    # info = call_the_official_api(url, api_key)

    video = workdir / "01.mp4"
    req = urllib.request.Request(url, headers={"user-agent": "ShareToNotes/1.0"})
    with urllib.request.urlopen(req, timeout=120) as res, open(video, "wb") as f:
        f.write(res.read())

    return {
        "media": [video],          # list of paths: videos and/or images, in slide order
        "caption": "",             # the post's text, if any
        "author": "",              # creator's handle, without @
        "posted": "",              # YYYY-MM-DD, if known
        "shortcode": "",           # optional stable ID (same as key(url))
        "cover_url": "",           # optional link to a cover image for the thumbnail
    }


def fetch_cover(url: str, workdir: Path) -> dict | None:
    """Optional. Used to add thumbnails to older notes. Can return just {"cover_url": ...} or media."""
    return fetch(url, workdir)
