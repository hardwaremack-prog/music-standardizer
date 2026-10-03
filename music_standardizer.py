#!/usr/bin/env python3
"""
music_standardizer.py — one file that cleans up a messy music library:
fixes filenames, tags, album art, and lets you preview a track before renaming it.

Built for libraries full of downloader junk like:
    YTDown.com_YouTube_AC-DC-Moneytalks-Official-HD-Video_Media_2lqdErI9uss_009_128k.mp3

GUI (recommended) — just run it with no arguments:
    pip install mutagen tkinterdnd2 pygame pillow
    python music_standardizer.py

    (tkinterdnd2 enables drag & drop, pygame enables in-app mp3 playback, and
    pillow enables the album art preview. The app still runs without any of
    them — those features just quietly disable themselves and tell you why.)

    1. Click "Choose Folder…" (or drag files/folders straight onto the window).
    2. Every row is a proposed Artist / Title / Track / new filename.
    3. Double-click any cell in the Artist / Title / Track column to fix a bad guess.
       Rows in yellow are low-confidence guesses worth a look.
    4. Click a row to preview its album art and play it back on the right.
    5. Turn on "Look up unclear tracks online" and/or "Fetch album art" if you
       want low-confidence guesses corrected and cover art pulled from iTunes.
    6. Click "Preview (dry run)" to see exactly what would happen, or
       "Apply" to actually rename the files and write the tags.
    7. "Clear" wipes the current list (doesn't touch your files).

CLI (for scripting / headless use) — pass "scan" or "apply" as the first argument:
    pip install mutagen

    # Step 1: scan a folder (recursively) and write a review sheet
    python music_standardizer.py scan "/path/to/Music" -o review.csv

    # ...open review.csv, fix any wrong guesses in the artist/title/track columns...

    # Step 2: apply the (edited) csv — renames files + writes tags
    python music_standardizer.py apply review.csv

    # Preview what apply would do without touching anything:
    python music_standardizer.py apply review.csv --dry-run

    # Also fetch & embed album art, and/or look up low-confidence rows online:
    python music_standardizer.py apply review.csv --art --online-lookup

CUSTOMIZING ARTIST NAMES
-------------------------
Hyphenated/space-run-together artist names (AC/DC, Jay-Z, T-Pain, etc.) get
mangled by naive splitting. Edit ARTIST_WHITELIST below (or pass --whitelist
yourfile.txt to the CLI, format: raw-as-it-appears|Canonical Name, one per
line) to teach it your library's tricky names.

ALBUM ART & ONLINE LOOKUP
---------------------------
Both use iTunes' free public search API (no key needed). --art embeds cover
art into the file's own tags. --online-lookup searches low-confidence guesses
(e.g. "AC DC Moneytalks") to correct artist/title before renaming. Files that
already have embedded art are skipped by default (--force-art to overwrite).
Both need internet access.

BUILDING A STANDALONE EXE
---------------------------
    pip install pyinstaller mutagen tkinterdnd2 pygame pillow
    pyinstaller --onefile --windowed --name MusicStandardizer --collect-all tkinterdnd2 --hidden-import PIL._tkinter_finder music_standardizer.py
"""


import argparse
import csv
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from pathlib import Path

try:
    from mutagen import File as MutagenFile
    from mutagen.easyid3 import EasyID3
    from mutagen.id3 import ID3, ID3NoHeaderError, APIC
    from mutagen.mp3 import MP3
    from mutagen.flac import FLAC, Picture
    from mutagen.easymp4 import EasyMP4
    from mutagen.mp4 import MP4, MP4Cover
except ImportError:
    print("Missing dependency. Run:  pip install mutagen", file=sys.stderr)
    sys.exit(1)

AUDIO_EXTS = {".mp3", ".flac", ".m4a", ".mp4", ".ogg", ".wav", ".wma"}


# --- default whitelist for names that break on naive hyphen-splitting ---
# format: "raw_form_lowercase": "Canonical Display Form"
ARTIST_WHITELIST = {
    "ac-dc": "AC/DC",
    "jay-z": "JAY-Z",
    "t-pain": "T-Pain",
    "kool-and-the-gang": "Kool & The Gang",
    "twenty-one-pilots": "Twenty One Pilots",
    "wu-tang-clan": "Wu-Tang Clan",
    "salt-n-pepa": "Salt-N-Pepa",
    "florence-and-the-machine": "Florence + the Machine",
    # classic rock / pop staples that commonly show up in download-site filenames
    # with spaces instead of punctuation (e.g. "Aerosmith Sweet Emotion.mp3")
    "aerosmith": "Aerosmith",
    "alex-warren": "Alex Warren",
    "april-wine": "April Wine",
    "billy-joel": "Billy Joel",
    "bon-jovi": "Bon Jovi",
    "creedence-clearwater-revival": "Creedence Clearwater Revival",
    "dire-straits": "Dire Straits",
    "guess-who": "The Guess Who",
    "the-guess-who": "The Guess Who",
    # common two-artist duet credits that appear as one run-together string
    "bill-medley-jennifer-warnes": "Bill Medley & Jennifer Warnes",
    "dolly-parton-kenny-rogers": "Dolly Parton & Kenny Rogers",
    "glen-campbell-carl-jackson": "Glen Campbell & Carl Jackson",
}

# --- junk tokens commonly injected by downloader sites (case-insensitive) ---
JUNK_SITE_TOKENS = {
    "ytdown.com", "ytdown", "youtube", "y2mate", "savefrom.net", "savefrom",
    "snaptube", "videoder", "ytmp3", "mp3juices", "ssyoutube", "media",
    "yt1s.com", "yt1s", "onlinevideoconverter", "downloader",
}

# --- descriptor words that belong to the *video*, not the song ---
JUNK_DESCRIPTOR_WORDS = {
    "official", "video", "audio", "hd", "hq", "4k", "lyrics", "lyric",
    "visualizer", "mv", "live", "remastered", "full", "version", "clip",
    "explicit", "clean", "topic", "records", "vevo", "music", "song",
    "cover", "singer",
}

# words that shouldn't be capitalized mid-title (unless first word)
LOWERCASE_WORDS = {
    "a", "an", "the", "of", "and", "or", "but", "in", "on", "at", "to",
    "for", "feat", "ft", "vs", "with",
}

BITRATE_RE = re.compile(r"^\d{2,4}k(bps)?$", re.IGNORECASE)
VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{10,12}$")
TRACK_NUM_RE = re.compile(r"^\d{1,3}$")
INVALID_FS_CHARS = re.compile(r'[<>:"/\\|?*]')


def load_whitelist(path=None):
    wl = dict(ARTIST_WHITELIST)
    if path:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or "|" not in line:
                    continue
                raw, canon = line.split("|", 1)
                wl[raw.strip().lower()] = canon.strip()
    return wl


def looks_like_video_id(token):
    if not VIDEO_ID_RE.match(token):
        return False
    has_upper = any(c.isupper() for c in token)
    has_lower = any(c.islower() for c in token)
    has_digit = any(c.isdigit() for c in token)
    return has_upper and has_lower and has_digit


def protect_whitelisted(segment, whitelist):
    """Replace whitelisted artist names with a placeholder so they survive
    naive hyphen-splitting, returning (protected_segment, mapping).

    Matches regardless of whether the source filename uses hyphens, spaces,
    or underscores between words — "AC DC", "AC-DC", and "AC_DC" all match
    a whitelist entry written as "ac-dc"."""
    mapping = {}
    for i, (raw, canon) in enumerate(sorted(whitelist.items(), key=lambda kv: -len(kv[0]))):
        parts = [re.escape(p) for p in re.split(r"[\s_\-]+", raw) if p]
        if not parts:
            continue
        pattern = re.compile(r"\b" + r"[\s_\-]*".join(parts) + r"\b", re.IGNORECASE)
        match = pattern.search(segment)
        if match:
            placeholder = f"\x00{i}\x00"
            mapping[placeholder] = canon
            segment = segment[:match.start()] + placeholder + segment[match.end():]
    return segment, mapping


def smart_titlecase(text):
    words = text.split(" ")
    out = []
    for i, w in enumerate(words):
        if not w:
            continue
        if i > 0 and w.lower() in LOWERCASE_WORDS:
            out.append(w.lower())
        elif w.isupper() and len(w) <= 4:
            out.append(w)  # keep short acronyms like DJ, MC, HD as-is
        else:
            out.append(w[:1].upper() + w[1:].lower() if w.islower() or w.isupper() else w)
    return " ".join(out)


def clean_stem(stem, whitelist):
    """Parse a messy filename stem into (artist_guess, title_guess, track_guess, confidence)."""
    tokens = re.split(r"[_]+", stem)
    track_guess = ""
    meaningful_tokens = []

    for tok in tokens:
        t = tok.strip()
        if not t:
            continue
        low = t.lower()
        if low in JUNK_SITE_TOKENS:
            continue
        if BITRATE_RE.match(t):
            continue
        if looks_like_video_id(t):
            continue
        if TRACK_NUM_RE.match(t) and not track_guess:
            track_guess = str(int(t))
            continue
        meaningful_tokens.append(t)

    if not meaningful_tokens:
        return "", smart_titlecase(stem.replace("_", " ").replace("-", " ")), track_guess, "low"

    # Join remaining tokens with a space, then work on hyphens within them
    joined = " ".join(meaningful_tokens)
    protected, mapping = protect_whitelisted(joined, whitelist)

    # Split on hyphens AND on whitelist placeholders — a placeholder is itself a
    # boundary even with no hyphen nearby, since matching it already told us
    # exactly where the artist name ends (e.g. "AC DC Moneytalks", space-only,
    # still needs a split right after the "AC DC" match).
    if mapping:
        placeholder_alt = "|".join(re.escape(p) for p in mapping)
        delim_pattern = re.compile(r"-|(" + placeholder_alt + r")")
    else:
        delim_pattern = re.compile(r"-")
    raw_parts = [p for p in delim_pattern.split(protected) if p and p.strip()]

    # restore placeholders; filter junk descriptor words at the word level (not
    # just whole-part equality) so they're caught whether joined by hyphens or spaces
    clean_parts = []
    artist_from_whitelist = None
    for p in raw_parts:
        p = p.strip()
        if p in mapping:
            artist_from_whitelist = mapping[p]
            clean_parts.append(mapping[p])
            continue
        words = [w for w in p.split(" ") if w.strip() and w.lower() not in JUNK_DESCRIPTOR_WORDS]
        cleaned = " ".join(words).strip()
        if cleaned:
            clean_parts.append(cleaned)

    confidence = "low"
    if artist_from_whitelist:
        artist = artist_from_whitelist
        title_parts = [p for p in clean_parts if p != artist_from_whitelist]
        title = smart_titlecase(" ".join(title_parts)) if title_parts else ""
        confidence = "high"
    elif " - " in joined or len(clean_parts) == 1:
        # fallback: no strong signal, treat first part as artist if multiple parts exist
        if len(clean_parts) >= 2:
            artist = smart_titlecase(clean_parts[0])
            title = smart_titlecase(" ".join(clean_parts[1:]))
        else:
            artist = ""
            title = smart_titlecase(clean_parts[0]) if clean_parts else ""
    else:
        artist = smart_titlecase(clean_parts[0]) if clean_parts else ""
        title = smart_titlecase(" ".join(clean_parts[1:])) if len(clean_parts) > 1 else ""

    # Never "discover" our own placeholder text as a real artist name — if a
    # previously-renamed file (e.g. "Unknown Artist - Song.mp3", using this
    # tool's own filler for tracks it couldn't identify) gets rescanned, treat
    # that leftover placeholder the same as if no artist had been found at all.
    if artist.strip().lower() == "unknown artist":
        artist = ""
        confidence = "low"

    return artist, title, track_guess, confidence


def has_album_art(path):
    """Return True if the file already has embedded cover art."""
    path = Path(path)
    ext = path.suffix.lower()
    try:
        if ext == ".mp3":
            audio = ID3(path)
            return any(k.startswith("APIC") for k in audio.keys())
        elif ext == ".flac":
            audio = FLAC(path)
            return len(audio.pictures) > 0
        elif ext in (".m4a", ".mp4"):
            audio = MP4(path)
            return bool(audio.get("covr"))
    except Exception:
        return False
    return False


class _ItunesRateLimiter:
    """Keeps us under iTunes Search API's undocumented-but-real limit of
    ~20 requests/minute per IP (confirmed via Apple's own performance-partners
    docs — described there as "approximately" and "subject to change").

    We throttle to a conservative ceiling (default 15/min, comfortably under
    20) using a sliding 60s window, and block/sleep on the calling thread
    when the ceiling is hit rather than firing anyway and eating a 429. This
    is shared by every search request the app makes (art lookups, online
    metadata correction), regardless of which worker thread calls it, since
    the limit is per-IP, not per-feature.
    """

    def __init__(self, max_per_minute=15, backoff_seconds=20):
        self.max_per_minute = max_per_minute
        self.backoff_seconds = backoff_seconds
        self._timestamps = deque()
        self._cooldown_until = 0.0
        self._lock = threading.Lock()

    def wait_for_slot(self, status_cb=None):
        """Block until it's safe to make another request. status_cb, if
        given, is called with a short human string while we're waiting so
        the GUI can show *why* things paused instead of looking frozen."""
        while True:
            with self._lock:
                now = time.monotonic()
                while self._timestamps and now - self._timestamps[0] >= 60:
                    self._timestamps.popleft()

                if now < self._cooldown_until:
                    sleep_for = self._cooldown_until - now
                    reason = "server said we're over the limit"
                elif len(self._timestamps) >= self.max_per_minute:
                    sleep_for = 60 - (now - self._timestamps[0]) + 0.1
                    reason = "pacing to stay under the limit"
                else:
                    self._timestamps.append(now)
                    return

            if status_cb:
                try:
                    status_cb(f"iTunes lookup rate limit reached ({reason}) — waiting {sleep_for:.0f}s…")
                except Exception:
                    pass
            time.sleep(min(sleep_for, 2))  # re-check periodically rather than one long sleep

    def note_429(self):
        """Called when the server itself says we're over the limit (HTTP 429),
        which means our own bookkeeping was optimistic (e.g. another process,
        or a previous run, also hit this IP) — pause new requests for a fixed
        cooldown rather than assuming our sliding window alone will fix it."""
        with self._lock:
            self._cooldown_until = time.monotonic() + self.backoff_seconds


_itunes_limiter = _ItunesRateLimiter(max_per_minute=15)


def _itunes_get_json(url, timeout=8, status_cb=None, max_retries=1):
    """GET a JSON response from an iTunes API URL, honoring the shared rate
    limiter and retrying once (after a cooldown) on HTTP 429. Returns the
    parsed dict, or None on any failure (no internet, no match, still
    rate-limited after the retry, etc.) — callers treat None as best-effort
    unavailable, never as a hard error."""
    attempt = 0
    while True:
        _itunes_limiter.wait_for_slot(status_cb=status_cb)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < max_retries:
                _itunes_limiter.note_429()
                attempt += 1
                continue
            return None
        except Exception:
            return None


def fetch_album_art(artist, title, timeout=8, status_cb=None):
    """Look up cover art on iTunes' free public search API.

    Returns (image_bytes, mime_type) or (None, None) if nothing was found
    or the request failed (no internet, rate-limited, etc.) — callers should
    treat a None result as "couldn't get art" rather than an error. Search
    requests are throttled to stay under iTunes' rate limit; status_cb (if
    given) is called with a short status string if we have to pause for it.
    """
    query = f"{artist} {title}".strip()
    if not query:
        return None, None

    search_url = "https://itunes.apple.com/search?" + urllib.parse.urlencode({
        "term": query,
        "media": "music",
        "entity": "song",
        "limit": 1,
    })

    data = _itunes_get_json(search_url, timeout=timeout, status_cb=status_cb)
    if data is None:
        return None, None

    results = data.get("results") or []
    if not results:
        return None, None

    art_url = results[0].get("artworkUrl100", "")
    if not art_url:
        return None, None

    # iTunes thumbnails are named like ".../100x100bb.jpg" — bump to a larger size.
    art_url = re.sub(r"\d+x\d+bb", "600x600bb", art_url)

    # The actual image download hits Apple's CDN (mzstatic), not the
    # rate-limited /search endpoint, so it isn't throttled here.
    try:
        img_req = urllib.request.Request(art_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(img_req, timeout=timeout) as resp:
            image_bytes = resp.read()
    except Exception:
        return None, None

    if not image_bytes:
        return None, None

    mime = "image/png" if image_bytes[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
    return image_bytes, mime


def lookup_track_online(query, timeout=8, status_cb=None):
    """Search iTunes' free public API for a raw, possibly messy query string
    (e.g. "AC DC Moneytalks") and return the best match's real artist/title,
    plus its cover art in the same request since we're already there.

    Returns (artist, title, image_bytes, mime) — any of which may be None if
    that part wasn't available — or (None, None, None, None) on no match /
    request failure. Callers should treat a None result as "couldn't resolve
    it" rather than an error; this is a best-effort correction, not a source
    of truth. Search requests are throttled to stay under iTunes' rate limit;
    status_cb (if given) is called with a short status string if we have to
    pause for it.
    """
    query = (query or "").strip()
    if not query:
        return None, None, None, None

    search_url = "https://itunes.apple.com/search?" + urllib.parse.urlencode({
        "term": query,
        "media": "music",
        "entity": "song",
        "limit": 1,
    })

    data = _itunes_get_json(search_url, timeout=timeout, status_cb=status_cb)
    if data is None:
        return None, None, None, None

    results = data.get("results") or []
    if not results:
        return None, None, None, None

    r = results[0]
    artist = (r.get("artistName") or "").strip() or None
    title = (r.get("trackName") or "").strip() or None
    art_url = r.get("artworkUrl100", "")

    image_bytes, mime = None, None
    if art_url:
        art_url = re.sub(r"\d+x\d+bb", "600x600bb", art_url)
        try:
            img_req = urllib.request.Request(art_url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(img_req, timeout=timeout) as resp:
                image_bytes = resp.read()
            if image_bytes:
                mime = "image/png" if image_bytes[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
            else:
                image_bytes = None
        except Exception:
            image_bytes, mime = None, None

    return artist, title, image_bytes, mime


def embed_album_art(path, image_bytes, mime="image/jpeg"):
    """Embed cover art bytes into the file's own tags. Raises on failure."""
    path = Path(path)
    ext = path.suffix.lower()

    if ext == ".mp3":
        try:
            audio = ID3(path)
        except ID3NoHeaderError:
            audio = ID3()
        audio.delall("APIC")
        audio.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=image_bytes))
        audio.save(path)

    elif ext == ".flac":
        audio = FLAC(path)
        audio.clear_pictures()
        pic = Picture()
        pic.data = image_bytes
        pic.type = 3
        pic.mime = mime
        audio.add_picture(pic)
        audio.save()

    elif ext in (".m4a", ".mp4"):
        audio = MP4(path)
        fmt = MP4Cover.FORMAT_PNG if mime == "image/png" else MP4Cover.FORMAT_JPEG
        audio["covr"] = [MP4Cover(image_bytes, imageformat=fmt)]
        audio.save()

    else:
        raise ValueError(f"Album art embedding isn't supported for {ext} files")


def get_album_art_bytes(path):
    """Return (image_bytes, mime) for a file's embedded cover art, or (None, None) if none."""
    path = Path(path)
    ext = path.suffix.lower()
    try:
        if ext == ".mp3":
            audio = ID3(path)
            apics = audio.getall("APIC")
            if apics:
                return apics[0].data, apics[0].mime
        elif ext == ".flac":
            audio = FLAC(path)
            if audio.pictures:
                pic = audio.pictures[0]
                return pic.data, pic.mime
        elif ext in (".m4a", ".mp4"):
            audio = MP4(path)
            covr = audio.get("covr")
            if covr:
                cover = covr[0]
                mime = "image/png" if cover.imageformat == MP4Cover.FORMAT_PNG else "image/jpeg"
                return bytes(cover), mime
    except Exception:
        pass
    return None, None


def read_existing_tags(path):
    """Return (artist, title, track) from existing tags, or ('','','') if none/unreadable."""
    ext = path.suffix.lower()
    try:
        if ext == ".mp3":
            audio = EasyID3(path)
        elif ext == ".flac":
            audio = FLAC(path)
        elif ext in (".m4a", ".mp4"):
            audio = EasyMP4(path)
        else:
            audio = MutagenFile(path, easy=True)
        if audio is None:
            return "", "", ""
        artist = (audio.get("artist") or [""])[0]
        title = (audio.get("title") or [""])[0]
        track = (audio.get("tracknumber") or [""])[0].split("/")[0]
        # Some taggers/rippers write literal "Unknown Artist" for unidentified
        # tracks — treat that the same as no artist tag at all, so it doesn't
        # get "trusted" as a real, confidently-tagged artist name.
        if artist.strip().lower() == "unknown artist":
            artist = ""
        return artist, title, track
    except Exception:
        return "", "", ""


def sanitize_filename(name):
    name = name.replace("/", "-")
    return INVALID_FS_CHARS.sub("", name).strip()


def build_proposed_filename(artist, title, track, ext, pattern="{artist} - {title}"):
    core = pattern.format(
        artist=artist or "",
        title=title or "Unknown Title",
        track=track.zfill(2) if track else "00",
    )
    # An empty {artist} leaves behind stray separators from the pattern
    # (" - Song Title", "05 -  - Song Title") — clean those up rather than
    # papering over an unknown artist with a fake "Unknown Artist" name.
    core = re.sub(r"^\s*-\s*", "", core)
    core = re.sub(r"\s*-\s*-\s*", " - ", core)
    core = re.sub(r"\s*-\s*$", "", core)
    core = core.strip()
    return sanitize_filename(core) + ext.lower()


def find_audio_files(paths):
    """Expand a mix of file and folder paths into a sorted, de-duplicated list of audio files.
    Folders are scanned recursively; individual audio files are taken as-is; anything else is
    ignored. Anything inside a folder literally named "Duplicates" is skipped — that's this
    tool's own holding area for files already moved aside by Remove Duplicates, not part of
    the active library, and re-scanning it can otherwise nest a Duplicates folder inside itself."""
    found = set()
    for p in paths:
        p = Path(p)
        if p.is_dir():
            found.update(
                f for f in p.rglob("*")
                if f.suffix.lower() in AUDIO_EXTS and "duplicates" not in {part.lower() for part in f.relative_to(p).parts[:-1]}
            )
        elif p.is_file() and p.suffix.lower() in AUDIO_EXTS:
            found.add(p)
    return sorted(found)


def build_rows_for_files(files, whitelist=None, pattern="{artist} - {title}", progress_cb=None):
    """Build proposal dicts for an explicit list of audio file Paths."""
    whitelist = whitelist if whitelist is not None else dict(ARTIST_WHITELIST)
    rows = []

    for i, path in enumerate(files):
        if progress_cb:
            progress_cb(i, len(files), path)

        tag_artist, tag_title, tag_track = read_existing_tags(path)
        guess_artist, guess_title, guess_track, confidence = clean_stem(path.stem, whitelist)

        final_artist = tag_artist.strip() or guess_artist
        final_title = tag_title.strip() or guess_title
        final_track = tag_track.strip() or guess_track
        if tag_artist.strip() and tag_title.strip():
            confidence = "high (from existing tags)"

        proposed_filename = build_proposed_filename(final_artist, final_title, final_track, path.suffix, pattern)

        rows.append({
            "original_path": str(path),
            "existing_tag_artist": tag_artist,
            "existing_tag_title": tag_title,
            "existing_tag_track": tag_track,
            "artist": final_artist,
            "title": final_title,
            "track": final_track,
            "confidence": confidence,
            "proposed_filename": proposed_filename,
            "has_art": "yes" if has_album_art(path) else "no",
        })

    if progress_cb and files:
        progress_cb(len(files), len(files), None)

    return rows


def scan_folder(folder, whitelist=None, pattern="{artist} - {title}", progress_cb=None):
    """Scan `folder` recursively for audio files and return a list of proposal dicts.

    whitelist: dict of {raw_lower: canonical} — defaults to ARTIST_WHITELIST if None.
    progress_cb: optional callable(current_index, total, path) called per file, useful for GUIs.
    """
    files = find_audio_files([folder])
    return build_rows_for_files(files, whitelist, pattern, progress_cb)


def scan_paths(paths, whitelist=None, pattern="{artist} - {title}", progress_cb=None):
    """Like scan_folder, but accepts a mixed list of file and/or folder paths.
    Used for drag-and-drop, where the user may drop individual files and folders together."""
    files = find_audio_files(paths)
    return build_rows_for_files(files, whitelist, pattern, progress_cb)


def apply_row(row, dry_run=False, pattern="{artist} - {title}", fetch_art=False, force_art=False, improve_metadata=False, status_cb=None):
    """Apply one proposal dict: write tags + rename (+ optionally fetch/embed album art,
    and/or look up low-confidence guesses online to correct artist/title).

    improve_metadata: when True, rows whose confidence is "low" get their raw guess
    string searched against iTunes' free API; a match overrides artist/title before
    tags are written and the file is renamed. If that same lookup also returns cover
    art and fetch_art is on, it's reused instead of firing a second art-only request.

    Returns a status dict, including result["art"] and result["metadata"]:
        None                    — step not requested / not applicable to this row
        "skipped (has art)"     — art: already had embedded art, left alone
        "added"                 — art: fetched and embedded successfully
        "corrected"             — metadata: artist/title were overridden by a match
        "not found"             — lookup succeeded but no match
        "error: ..."            — network or embed failure
        "skipped (preview)"     — dry run; no network call was made
    """
    src = Path(row["original_path"])
    result = {
        "original_path": str(src), "renamed": False, "tagged": False,
        "error": None, "dest": None, "art": None, "metadata": None,
    }

    if not src.exists():
        result["error"] = "file missing"
        return result

    artist = row.get("artist", "").strip()
    title = row.get("title", "").strip()
    track = row.get("track", "").strip()
    is_low_confidence = row.get("confidence", "").startswith("low")

    if dry_run:
        new_name = row.get("proposed_filename", "").strip() or build_proposed_filename(
            artist, title, track, src.suffix, pattern
        )
        result["dest"] = str(src.parent / new_name)
        if fetch_art:
            result["art"] = "skipped (preview)"
        if improve_metadata and is_low_confidence:
            result["metadata"] = "skipped (preview)"
        return result

    looked_up_art_bytes, looked_up_art_mime = None, None

    if improve_metadata and is_low_confidence:
        query = f"{artist} {title}".strip() or src.stem
        try:
            found_artist, found_title, img_bytes, img_mime = lookup_track_online(query, status_cb=status_cb)
            if found_artist and found_title:
                artist, title = found_artist, found_title
                result["metadata"] = "corrected"
                looked_up_art_bytes, looked_up_art_mime = img_bytes, img_mime
            else:
                result["metadata"] = "not found"
        except Exception as e:
            result["metadata"] = f"error: {e}"

    new_name = build_proposed_filename(artist, title, track, src.suffix, pattern)
    dest = src.parent / new_name
    result["dest"] = str(dest)
    result["artist"] = artist
    result["title"] = title

    try:
        _write_tags(src, artist, title, track)
        result["tagged"] = True
    except Exception as e:
        result["error"] = f"tag error: {e}"

    if fetch_art:
        try:
            if not force_art and has_album_art(src):
                result["art"] = "skipped (has art)"
            elif looked_up_art_bytes:
                embed_album_art(src, looked_up_art_bytes, looked_up_art_mime)
                result["art"] = "added"
            else:
                image_bytes, mime = fetch_album_art(artist, title, status_cb=status_cb)
                if image_bytes:
                    embed_album_art(src, image_bytes, mime)
                    result["art"] = "added"
                else:
                    result["art"] = "not found"
        except Exception as e:
            result["art"] = f"error: {e}"

    if dest != src:
        try:
            if dest.exists():
                result["error"] = (result["error"] + "; " if result["error"] else "") + "rename skipped: target exists"
            else:
                src.rename(dest)
                result["renamed"] = True
        except Exception as e:
            result["error"] = (result["error"] + "; " if result["error"] else "") + f"rename error: {e}"

    return result


def apply_art_only(row, force_art=False, improve_metadata=False, status_cb=None):
    """Fetch & embed cover art for one file WITHOUT touching its filename or any
    other tags — for people who just want art without a full rename+retag pass.

    improve_metadata: when True and the row is low-confidence, also try iTunes'
    text search (same as the online-lookup feature) to find art, since a plain
    artist/title art lookup is unlikely to succeed for a row that's low-confidence
    in the first place. This never changes the row's artist/title, only the art.

    Returns a status dict with result["art"] — see apply_row's docstring for the
    possible values (this reuses the same vocabulary: "added", "not found", etc.)
    """
    src = Path(row["original_path"])
    result = {"original_path": str(src), "art": None, "error": None}

    if not src.exists():
        result["error"] = "file missing"
        return result

    artist = row.get("artist", "").strip()
    title = row.get("title", "").strip()
    is_low_confidence = row.get("confidence", "").startswith("low")

    try:
        if not force_art and has_album_art(src):
            result["art"] = "skipped (has art)"
            return result

        image_bytes, mime = None, None
        if improve_metadata and is_low_confidence:
            query = f"{artist} {title}".strip() or src.stem
            _, _, image_bytes, mime = lookup_track_online(query, status_cb=status_cb)

        if not image_bytes:
            image_bytes, mime = fetch_album_art(artist, title, status_cb=status_cb)

        if image_bytes:
            embed_album_art(src, image_bytes, mime)
            result["art"] = "added"
        else:
            result["art"] = "not found"
    except Exception as e:
        result["art"] = f"error: {e}"

    return result


def cmd_scan(args):
    root = Path(args.folder)
    if not root.exists():
        print(f"Folder not found: {root}", file=sys.stderr)
        sys.exit(1)

    whitelist = load_whitelist(args.whitelist)
    rows = scan_folder(args.folder, whitelist, args.pattern)

    if not rows:
        print("No audio files found.")
        return

    with open(args.output, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    low_conf = sum(1 for r in rows if r["confidence"] == "low")
    print(f"Scanned {len(rows)} files -> wrote {args.output}")
    print(f"  {low_conf} file(s) are low-confidence guesses — worth a manual check in the csv.")
    print("Open the csv, fix the 'artist' / 'title' / 'track' columns as needed, then run:")
    print(f"  python music_standardizer.py apply {args.output}")


def cmd_apply(args):
    csv_path = Path(args.csv)
    if not csv_path.exists():
        print(f"CSV not found: {csv_path}", file=sys.stderr)
        sys.exit(1)

    if args.force_art:
        args.art = True

    with open(csv_path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    renamed, tagged, skipped = 0, 0, 0
    art_added, art_skipped, art_not_found, art_errors = 0, 0, 0, 0
    meta_corrected, meta_not_found, meta_errors = 0, 0, 0

    def _cli_status_cb(text):
        print(f"  {text}", file=sys.stderr)

    for row in rows:
        result = apply_row(row, dry_run=args.dry_run, fetch_art=args.art, force_art=args.force_art,
                            improve_metadata=args.online_lookup,
                            status_cb=None if args.dry_run else _cli_status_cb)
        src_name = Path(result["original_path"]).name
        dest_name = Path(result["dest"]).name if result["dest"] else "?"

        if args.dry_run:
            notes = []
            if args.art:
                notes.append("would fetch art")
            if args.online_lookup and row.get("confidence", "").startswith("low"):
                notes.append("would look up online")
            note = f"  [{', '.join(notes)}]" if notes else ""
            print(f"  [dry-run] {src_name}  ->  {dest_name}{note}")
            continue

        if result["error"] and "missing" in result["error"]:
            print(f"  [skip] missing file: {src_name}")
            skipped += 1
            continue
        if result["tagged"]:
            tagged += 1
        if result["renamed"]:
            renamed += 1
        if result["error"]:
            print(f"  [{src_name}] {result['error']}")

        if args.online_lookup and result["metadata"]:
            if result["metadata"] == "corrected":
                meta_corrected += 1
                print(f"  [{src_name}] corrected to: {result['artist']} — {result['title']}")
            elif result["metadata"] == "not found":
                meta_not_found += 1
            elif result["metadata"].startswith("error"):
                meta_errors += 1
                print(f"  [{src_name}] lookup {result['metadata']}")

        if args.art and result["art"]:
            if result["art"] == "added":
                art_added += 1
            elif result["art"].startswith("skipped"):
                art_skipped += 1
            elif result["art"] == "not found":
                art_not_found += 1
                print(f"  [{src_name}] no album art found")
            elif result["art"].startswith("error"):
                art_errors += 1
                print(f"  [{src_name}] art {result['art']}")

    if args.dry_run:
        print(f"\nDry run complete. {len(rows)} row(s) previewed, nothing was changed.")
    else:
        print(f"\nDone. Renamed {renamed} file(s), tagged {tagged} file(s), skipped {skipped}.")
        if args.art:
            print(f"Album art: {art_added} added, {art_skipped} already had art, "
                  f"{art_not_found} not found, {art_errors} error(s).")
        if args.online_lookup:
            print(f"Online lookup: {meta_corrected} corrected, {meta_not_found} not found, {meta_errors} error(s).")


def _write_tags(path, artist, title, track):
    ext = path.suffix.lower()
    if ext == ".mp3":
        try:
            audio = EasyID3(path)
        except ID3NoHeaderError:
            audio = MP3(path)
            audio.add_tags()
            audio = EasyID3(path)
    elif ext == ".flac":
        audio = FLAC(path)
    elif ext in (".m4a", ".mp4"):
        audio = EasyMP4(path)
    else:
        audio = MutagenFile(path, easy=True)
        if audio is None:
            return

    if artist:
        audio["artist"] = artist
    if title:
        audio["title"] = title
    if track:
        audio["tracknumber"] = track
    audio.save()


def cli_main():
    parser = argparse.ArgumentParser(description="Standardize a messy music library's filenames and tags.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_scan = sub.add_parser("scan", help="Scan a folder and write a review.csv of proposed changes")
    p_scan.add_argument("folder", help="Path to your music folder (scanned recursively)")
    p_scan.add_argument("-o", "--output", default="review.csv", help="Where to write the review csv")
    p_scan.add_argument("--pattern", default="{artist} - {title}",
                         help="Filename pattern. Available: {artist} {title} {track}. "
                              "e.g. '{track} - {artist} - {title}'")
    p_scan.add_argument("--whitelist", help="Optional extra whitelist file (raw|Canonical per line)")
    p_scan.set_defaults(func=cmd_scan)

    p_apply = sub.add_parser("apply", help="Apply a (possibly edited) review.csv: rename + tag files")
    p_apply.add_argument("csv", help="Path to the review csv (from the scan step)")
    p_apply.add_argument("--dry-run", action="store_true", help="Preview changes without touching files")
    p_apply.add_argument("--art", action="store_true", help="Also fetch & embed album art from iTunes (needs internet)")
    p_apply.add_argument("--force-art", action="store_true", help="Re-fetch art even for files that already have it (implies --art)")
    p_apply.add_argument("--online-lookup", action="store_true", help="Look up low-confidence rows on iTunes to correct artist/title (needs internet)")
    p_apply.set_defaults(func=cmd_apply)

    args = parser.parse_args()
    args.func(args)


# =============================================================================
# GUI (Tkinter) — everything below builds on the functions above.
# `core` is just an alias for this same module, kept so the GUI code below
# can call core.scan_folder(...), core.apply_row(...) etc. exactly as it
# would if this were still split into two files.
# =============================================================================
core = sys.modules[__name__]


import io
import random
import shutil
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import ttk, filedialog, messagebox

DND_AVAILABLE = False
_BaseWindow = tk.Tk
try:
    from tkinterdnd2 import TkinterDnD, DND_FILES
    # Importing the python module can succeed even if the native tkdnd Tcl
    # extension it depends on isn't actually available (e.g. missing data
    # files in a frozen/bundled build) — that failure only shows up when Tcl
    # tries to load it. So actually construct a throwaway window to confirm
    # it really works before committing to use it as our base class.
    _probe = TkinterDnD.Tk()
    _probe.withdraw()
    _probe.destroy()
    DND_AVAILABLE = True
    _BaseWindow = TkinterDnD.Tk
except Exception:
    DND_AVAILABLE = False
    _BaseWindow = tk.Tk

PLAYBACK_AVAILABLE = True
try:
    import pygame
    pygame.mixer.init()
except Exception:
    PLAYBACK_AVAILABLE = False

PIL_AVAILABLE = True
try:
    from PIL import Image, ImageTk
except ImportError:
    PIL_AVAILABLE = False

EDITABLE_COLS = {"artist", "title", "track"}
COLUMNS = [
    ("filename", "Original Filename", 220),
    ("artist", "Artist", 120),
    ("title", "Title", 160),
    ("track", "Track", 50),
    ("has_art", "Has Art", 60),
    ("confidence", "Confidence", 120),
    ("duplicate", "Duplicate", 90),
    ("proposed_filename", "New Filename", 220),
]

class App(_BaseWindow):
    def __init__(self):
        super().__init__()
        self.title("Music Library Standardizer")
        self.geometry("1380x680")

        self.folder = tk.StringVar(value="")
        self.pattern = tk.StringVar(value="{artist} - {title}")
        self.fetch_art = tk.BooleanVar(value=False)
        self.force_art = tk.BooleanVar(value=False)
        self.online_lookup = tk.BooleanVar(value=False)
        self.volume = tk.DoubleVar(value=0.8)
        self.edit_artist_var = tk.StringVar()
        self.edit_title_var = tk.StringVar()
        self.edit_track_var = tk.StringVar()
        self.rows = []  # list of proposal dicts (same shape as core.scan_folder output)
        self.row_id_to_index = {}  # treeview item id -> index into self.rows

        self.selected_path = None
        self.selected_item_id = None
        self.currently_playing = None
        self.art_image_ref = None  # keep a reference so Tk doesn't garbage-collect the preview image
        self._vis_bar_count = 14
        self._vis_heights = [3] * self._vis_bar_count
        self.sort_column = None
        self.sort_reverse = False

        self._build_top_bar()
        self._build_main_area()
        self._build_bottom_bar()
        self._animate_visualizer()

    # ---------- UI construction ----------

    def _build_top_bar(self):
        bar = ttk.Frame(self, padding=8)
        bar.pack(fill="x")

        ttk.Button(bar, text="Choose Folder…", command=self.choose_folder).pack(side="left")
        ttk.Button(bar, text="Clear", command=self.clear_all).pack(side="left", padx=(6, 0))
        self.folder_label = ttk.Label(bar, text="No folder selected", foreground="#555")
        self.folder_label.pack(side="left", padx=8)

        ttk.Label(bar, text="Filename pattern:").pack(side="left", padx=(20, 4))
        pattern_entry = ttk.Entry(bar, textvariable=self.pattern, width=28)
        pattern_entry.pack(side="left")
        ttk.Label(bar, text="(use {artist} {title} {track})", foreground="#777").pack(side="left", padx=4)

        self.scan_btn = ttk.Button(bar, text="Scan", command=self.start_scan, state="disabled")
        self.scan_btn.pack(side="right")

        self.remove_dup_btn = ttk.Button(bar, text="Remove Duplicates", command=self.remove_duplicates, state="disabled")
        self.remove_dup_btn.pack(side="right", padx=(0, 6))
        self.find_dup_btn = ttk.Button(bar, text="Find Duplicates", command=self.find_duplicates, state="disabled")
        self.find_dup_btn.pack(side="right", padx=(0, 6))

    def _build_main_area(self):
        container = ttk.Frame(self)
        container.pack(fill="both", expand=True, padx=8, pady=4)

        drop_text = ("🎵  Drag & drop mp3 / flac / m4a files or folders here"
                     if DND_AVAILABLE else
                     "Drag & drop unavailable (pip install tkinterdnd2 to enable it) — use Choose Folder instead")
        self.drop_label = ttk.Label(container, text=drop_text, anchor="center",
                                     relief="groove", padding=8, foreground="#555")
        self.drop_label.pack(fill="x", pady=(0, 6))

        table_row = ttk.Frame(container)
        table_row.pack(fill="both", expand=True)

        col_ids = [c[0] for c in COLUMNS]
        self.tree = ttk.Treeview(table_row, columns=col_ids, show="headings", selectmode="browse")
        for col_id, heading, width in COLUMNS:
            self.tree.heading(col_id, text=heading, command=lambda c=col_id: self.sort_by_column(c))
            self.tree.column(col_id, width=width, anchor="w")

        vsb = ttk.Scrollbar(table_row, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="left", fill="y")

        self.tree.tag_configure("low", background="#fff3b0")
        self.tree.tag_configure("high", background="#e6f4ea")
        self.tree.tag_configure("dup_remove", background="#f9c9c0")

        self.tree.bind("<Double-1>", self.on_double_click)
        self.tree.bind("<<TreeviewSelect>>", self.on_row_select)

        self._build_side_panel(table_row)

        if DND_AVAILABLE:
            for widget in (self.drop_label, self.tree):
                widget.drop_target_register(DND_FILES)
                widget.dnd_bind("<<Drop>>", self.on_drop)

    def _build_side_panel(self, parent):
        # Wrapped in a scrollable canvas so the side panel's content (album art,
        # visualizer, volume, edit fields) is always fully reachable regardless of
        # window height, screen size, or OS chrome — nothing can get clipped off
        # the bottom the way a plain fixed-height frame could.
        outer = ttk.Frame(parent, width=232)
        outer.pack(side="left", fill="y")
        outer.pack_propagate(False)

        canvas = tk.Canvas(outer, highlightthickness=0)
        vsb = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        canvas.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        side = ttk.Frame(canvas, padding=(10, 6))
        window_id = canvas.create_window((0, 0), window=side, anchor="nw")

        def _sync_scrollregion(event=None):
            canvas.configure(scrollregion=canvas.bbox("all"))
        side.bind("<Configure>", _sync_scrollregion)

        def _sync_inner_width(event):
            canvas.itemconfig(window_id, width=event.width)
        canvas.bind("<Configure>", _sync_inner_width)

        def _on_mousewheel(event):
            if event.num == 4:
                canvas.yview_scroll(-1, "units")
            elif event.num == 5:
                canvas.yview_scroll(1, "units")
            else:
                canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        canvas.bind("<Enter>", lambda e: (canvas.bind_all("<MouseWheel>", _on_mousewheel),
                                           canvas.bind_all("<Button-4>", _on_mousewheel),
                                           canvas.bind_all("<Button-5>", _on_mousewheel)))
        canvas.bind("<Leave>", lambda e: (canvas.unbind_all("<MouseWheel>"),
                                           canvas.unbind_all("<Button-4>"),
                                           canvas.unbind_all("<Button-5>")))

        ttk.Label(side, text="Album Art", font=("", 10, "bold")).pack(anchor="w")

        self.visualizer_canvas = tk.Canvas(side, width=190, height=36, bg="#1a1a1a", highlightthickness=0)
        self.visualizer_canvas.pack(pady=(4, 4))

        art_frame = ttk.Frame(side, width=190, height=190, relief="sunken")
        art_frame.pack(pady=(0, 6))
        art_frame.pack_propagate(False)
        self.art_label = ttk.Label(art_frame, text="No selection", anchor="center", justify="center", wraplength=170)
        self.art_label.pack(fill="both", expand=True)
        if not PIL_AVAILABLE:
            self.art_label.config(text="No selection\n\n(install pillow to preview art)")

        self.now_playing_label = ttk.Label(side, text="", wraplength=200, foreground="#333")
        self.now_playing_label.pack(fill="x", pady=(2, 6))

        self.play_btn = ttk.Button(side, text="▶ Play", command=self.toggle_play, state="disabled")
        self.play_btn.pack(fill="x")

        if not PLAYBACK_AVAILABLE:
            ttk.Label(side, text="(install pygame to enable playback)", foreground="#999",
                      wraplength=200, font=("", 8)).pack(pady=(4, 0))

        ttk.Label(side, text="Volume", font=("", 8), foreground="#666").pack(anchor="w", pady=(8, 0))
        volume_scale = ttk.Scale(side, from_=0.0, to=1.0, orient="horizontal",
                                  variable=self.volume, command=self._on_volume_change)
        volume_scale.pack(fill="x")
        if not PLAYBACK_AVAILABLE:
            volume_scale.state(["disabled"])

        ttk.Separator(side, orient="horizontal").pack(fill="x", pady=(12, 8))
        ttk.Label(side, text="Edit Selected Track", font=("", 9, "bold")).pack(anchor="w")

        ttk.Label(side, text="Artist", font=("", 8), foreground="#666").pack(anchor="w", pady=(6, 0))
        self.edit_artist_entry = ttk.Entry(side, textvariable=self.edit_artist_var)
        self.edit_artist_entry.pack(fill="x")

        ttk.Label(side, text="Title", font=("", 8), foreground="#666").pack(anchor="w", pady=(6, 0))
        self.edit_title_entry = ttk.Entry(side, textvariable=self.edit_title_var)
        self.edit_title_entry.pack(fill="x")

        ttk.Label(side, text="Track #", font=("", 8), foreground="#666").pack(anchor="w", pady=(6, 0))
        self.edit_track_entry = ttk.Entry(side, textvariable=self.edit_track_var, width=8)
        self.edit_track_entry.pack(anchor="w")

        self.edit_apply_btn = ttk.Button(side, text="💾 Save Changes", command=self._apply_manual_edit, state="disabled")
        self.edit_apply_btn.pack(fill="x", pady=(10, 4))
        ttk.Label(side, text="Updates the list below — click Apply\nto actually write it to the file.",
                  font=("", 7), foreground="#888", justify="left").pack(anchor="w", pady=(0, 10))

        for entry in (self.edit_artist_entry, self.edit_title_entry, self.edit_track_entry):
            entry.bind("<Return>", lambda e: self._apply_manual_edit())

    def _build_bottom_bar(self):
        bar = ttk.Frame(self, padding=8)
        bar.pack(fill="x")

        self.status = ttk.Label(bar, text="Pick a folder or drag & drop files to get started.")
        self.status.pack(side="left")

        self.count_label = ttk.Label(bar, text="0 songs", foreground="#555")
        self.count_label.pack(side="left", padx=(16, 0))

        self.apply_btn = ttk.Button(bar, text="Apply (rename + tag)", command=lambda: self.start_apply(dry_run=False), state="disabled")
        self.apply_btn.pack(side="right", padx=(6, 0))
        self.preview_btn = ttk.Button(bar, text="Check for Conflicts", command=lambda: self.start_apply(dry_run=True), state="disabled")
        self.preview_btn.pack(side="right")

        ttk.Checkbutton(bar, text="Re-fetch even if art exists", variable=self.force_art).pack(side="right", padx=(4, 16))
        self.fetch_art_only_btn = ttk.Button(bar, text="🎨 Fetch Album Art Only", command=self.start_fetch_art_only, state="disabled")
        self.fetch_art_only_btn.pack(side="right", padx=(4, 4))
        ttk.Checkbutton(bar, text="Fetch album art (needs internet)", variable=self.fetch_art).pack(side="right", padx=(4, 4))
        ttk.Checkbutton(bar, text="Look up unclear tracks online", variable=self.online_lookup).pack(side="right", padx=(4, 16))

    def _update_song_count(self):
        n = len(self.rows)
        self.count_label.config(text=f"{n} song{'s' if n != 1 else ''}")

    # ---------- folder scan ----------

    def choose_folder(self):
        folder = filedialog.askdirectory(title="Choose your music folder")
        if folder:
            self.folder.set(folder)
            self.folder_label.config(text=folder)
            self.scan_btn.config(state="normal")

    def start_scan(self):
        folder = self.folder.get()
        if not folder:
            return
        pattern = self.pattern.get()
        self.scan_btn.config(state="disabled")
        self.status.config(text="Scanning…")

        thread = threading.Thread(target=self._scan_worker, args=(folder, pattern), daemon=True)
        thread.start()

    def _scan_worker(self, folder, pattern):
        def progress(i, total, path):
            if path is not None:
                self.after(0, lambda: self.status.config(text=f"Scanning {i + 1}/{total}: {path.name}"))

        try:
            rows = core.scan_folder(folder, pattern=pattern, progress_cb=progress)
        except Exception as e:
            self.after(0, lambda: self._scan_failed(e))
            return
        self.after(0, lambda: self._scan_done(rows))

    def _scan_failed(self, exc):
        self.status.config(text="Scan failed.")
        self.scan_btn.config(state="normal")
        messagebox.showerror("Scan failed", str(exc))

    def _update_dup_button_states(self):
        self.find_dup_btn.config(state="normal" if self.rows else "disabled")
        has_pending_dups = any(r.get("duplicate") == "remove" for r in self.rows)
        self.remove_dup_btn.config(state="normal" if has_pending_dups else "disabled")

    def _scan_done(self, rows):
        self.rows = rows
        self.scan_btn.config(state="normal")
        self._rebuild_tree()

        if not rows:
            self.status.config(text="No audio files found in that folder.")
            self.apply_btn.config(state="disabled")
            self.preview_btn.config(state="disabled")
            self.fetch_art_only_btn.config(state="disabled")
            self._update_dup_button_states()
            return

        low_count = sum(1 for r in rows if r["confidence"] == "low")
        self.status.config(text=f"Found {len(rows)} file(s) — {low_count} need a manual check (highlighted).")
        self.apply_btn.config(state="normal")
        self.preview_btn.config(state="normal")
        self.fetch_art_only_btn.config(state="normal")
        self._update_dup_button_states()

    # ---------- drag & drop ----------

    def on_drop(self, event):
        try:
            paths = [p for p in self.tk.splitlist(event.data) if p]
        except Exception:
            paths = [event.data] if event.data else []
        if not paths:
            return
        pattern = self.pattern.get()
        self.status.config(text=f"Scanning {len(paths)} dropped item(s)…")
        thread = threading.Thread(target=self._scan_paths_worker, args=(paths, pattern), daemon=True)
        thread.start()

    def _scan_paths_worker(self, paths, pattern):
        def progress(i, total, path):
            if path is not None:
                self.after(0, lambda: self.status.config(text=f"Scanning {i + 1}/{total}: {path.name}"))

        try:
            new_rows = core.scan_paths(paths, pattern=pattern, progress_cb=progress)
        except Exception as e:
            self.after(0, lambda: self._scan_failed(e))
            return
        self.after(0, lambda: self._merge_rows(new_rows))

    def _merge_rows(self, new_rows):
        if not new_rows:
            self.status.config(text="No audio files found in what you dropped.")
            return

        by_path = {r["original_path"]: i for i, r in enumerate(self.rows)}
        added = 0
        for row in new_rows:
            if row["original_path"] in by_path:
                self.rows[by_path[row["original_path"]]] = row
            else:
                self.rows.append(row)
                added += 1

        self._rebuild_tree()
        self.status.config(text=f"Added {added} new file(s) from drop (updated {len(new_rows) - added} existing). Total: {len(self.rows)}.")
        self.apply_btn.config(state="normal")
        self.preview_btn.config(state="normal")
        self.fetch_art_only_btn.config(state="normal")
        self._update_dup_button_states()

    # ---------- table helpers ----------

    def sort_by_column(self, col_id):
        if not self.rows:
            return
        if self.sort_column == col_id:
            self.sort_reverse = not self.sort_reverse
        else:
            self.sort_column = col_id
            self.sort_reverse = False

        self.rows.sort(key=self._sort_key_func(col_id), reverse=self.sort_reverse)

        selected_path = self.selected_path
        self._rebuild_tree()
        self._refresh_column_headings()

        if selected_path:
            for item_id, idx in self.row_id_to_index.items():
                if self.rows[idx]["original_path"] == selected_path:
                    self.tree.selection_set(item_id)
                    self.tree.see(item_id)
                    break

    def _sort_key_func(self, col_id):
        def track_key(r):
            t = r.get("track", "").strip()
            return int(t) if t.isdigit() else float("inf")

        funcs = {
            "filename": lambda r: Path(r["original_path"]).name.lower(),
            "artist": lambda r: r.get("artist", "").lower(),
            "title": lambda r: r.get("title", "").lower(),
            "track": track_key,
            "has_art": lambda r: r.get("has_art", ""),
            "confidence": lambda r: r.get("confidence", ""),
            "duplicate": lambda r: r.get("duplicate", ""),
            "proposed_filename": lambda r: r.get("proposed_filename", "").lower(),
        }
        return funcs.get(col_id, lambda r: "")

    def _refresh_column_headings(self):
        for col_id, heading, _ in COLUMNS:
            text = heading
            if col_id == self.sort_column:
                text += " ▼" if self.sort_reverse else " ▲"
            self.tree.heading(col_id, text=text)

    def _rebuild_tree(self):
        for item in self.tree.get_children():
            self.tree.delete(item)
        self.row_id_to_index = {}
        for idx, row in enumerate(self.rows):
            self._insert_row(idx, row)
        self._update_song_count()

    def _insert_row(self, idx, row):
        filename = Path(row["original_path"]).name
        dup_status = row.get("duplicate", "")
        tag = "dup_remove" if dup_status == "remove" else ("low" if row["confidence"] == "low" else "high")
        values = (
            filename,
            row["artist"],
            row["title"],
            row["track"],
            row.get("has_art", ""),
            row["confidence"],
            dup_status,
            row["proposed_filename"],
        )
        item_id = self.tree.insert("", "end", values=values, tags=(tag,))
        self.row_id_to_index[item_id] = idx

    def clear_all(self):
        self.stop_playback()
        self.rows = []
        self.row_id_to_index = {}
        for item in self.tree.get_children():
            self.tree.delete(item)
        self._clear_art_preview()
        self.apply_btn.config(state="disabled")
        self.preview_btn.config(state="disabled")
        self.fetch_art_only_btn.config(state="disabled")
        self._update_dup_button_states()
        self._update_song_count()
        self.status.config(text="Cleared. Pick a folder or drag & drop files to get started.")

    def on_double_click(self, event):
        region = self.tree.identify("region", event.x, event.y)
        if region != "cell":
            return
        item_id = self.tree.identify_row(event.y)
        col_id = self.tree.identify_column(event.x)  # like '#2'
        col_index = int(col_id.replace("#", "")) - 1
        col_name = COLUMNS[col_index][0]

        if item_id not in self.row_id_to_index:
            return

        if col_name == "filename":
            self.tree.selection_set(item_id)
            self.toggle_play()
            return

        if col_name not in EDITABLE_COLS:
            return

        x, y, width, height = self.tree.bbox(item_id, col_id)
        current_value = self.tree.set(item_id, col_name)

        editor = ttk.Entry(self.tree)
        editor.insert(0, current_value)
        editor.select_range(0, "end")
        editor.focus()
        editor.place(x=x, y=y, width=width, height=height)

        def save(_event=None):
            new_value = editor.get()
            editor.destroy()
            self.tree.set(item_id, col_name, new_value)
            idx = self.row_id_to_index[item_id]
            self.rows[idx][col_name] = new_value
            self._refresh_proposed_filename(item_id, idx)

        editor.bind("<Return>", save)
        editor.bind("<FocusOut>", save)
        editor.bind("<Escape>", lambda e: editor.destroy())

    def _refresh_proposed_filename(self, item_id, idx):
        row = self.rows[idx]
        ext = Path(row["original_path"]).suffix
        new_name = core.build_proposed_filename(row["artist"], row["title"], row["track"], ext, self.pattern.get())
        row["proposed_filename"] = new_name
        self.tree.set(item_id, "proposed_filename", new_name)

    # ---------- selection: album art preview + playback target ----------

    def on_row_select(self, event=None):
        selection = self.tree.selection()
        if not selection:
            self._clear_art_preview()
            return
        item_id = selection[0]
        idx = self.row_id_to_index.get(item_id)
        if idx is None:
            return
        row = self.rows[idx]
        self.selected_path = row["original_path"]
        self.selected_item_id = item_id
        self.now_playing_label.config(text=f"{row['artist']} — {row['title']}" if row["artist"] or row["title"] else Path(row["original_path"]).name)
        self._update_art_preview(self.selected_path)
        self.play_btn.config(state="normal" if PLAYBACK_AVAILABLE else "disabled")

        self.edit_artist_var.set(row["artist"])
        self.edit_title_var.set(row["title"])
        self.edit_track_var.set(row["track"])
        self.edit_apply_btn.config(state="normal")

    def _update_art_preview(self, path):
        if not PIL_AVAILABLE:
            return
        image_bytes, mime = core.get_album_art_bytes(path)
        if not image_bytes:
            self.art_label.config(image="", text="No album art")
            self.art_image_ref = None
            return
        try:
            img = Image.open(io.BytesIO(image_bytes))
            img.thumbnail((180, 180))
            photo = ImageTk.PhotoImage(img)
            self.art_label.config(image=photo, text="")
            self.art_image_ref = photo  # prevent garbage collection
        except Exception:
            self.art_label.config(image="", text="(couldn't preview art)")
            self.art_image_ref = None

    def _clear_art_preview(self):
        self.art_label.config(image="", text="No selection" if PIL_AVAILABLE else "No selection\n\n(install pillow to preview art)")
        self.art_image_ref = None
        self.now_playing_label.config(text="")
        self.selected_path = None
        self.selected_item_id = None
        self.play_btn.config(state="disabled")
        self.edit_artist_var.set("")
        self.edit_title_var.set("")
        self.edit_track_var.set("")
        self.edit_apply_btn.config(state="disabled")

    def _apply_manual_edit(self):
        if self.selected_item_id is None or self.selected_item_id not in self.row_id_to_index:
            return
        item_id = self.selected_item_id
        idx = self.row_id_to_index[item_id]

        artist = self.edit_artist_var.get().strip()
        title = self.edit_title_var.get().strip()
        track = self.edit_track_var.get().strip()

        self.rows[idx]["artist"] = artist
        self.rows[idx]["title"] = title
        self.rows[idx]["track"] = track
        # A manual correction is as trustworthy as it gets — mark it so it
        # renders green like any other high-confidence row, and so a later
        # online lookup won't try to "correct" it again.
        self.rows[idx]["confidence"] = "high (manual)"

        self.tree.set(item_id, "artist", artist)
        self.tree.set(item_id, "title", title)
        self.tree.set(item_id, "track", track)
        self.tree.set(item_id, "confidence", "high (manual)")
        self.tree.item(item_id, tags=("high",))

        self._refresh_proposed_filename(item_id, idx)
        self.now_playing_label.config(text=f"{artist} — {title}" if artist or title else Path(self.rows[idx]["original_path"]).name)

        # Give clear, immediate confirmation that the save actually happened —
        # both in the status bar and a brief change on the button itself.
        label = f"{artist} — {title}".strip(" —") or Path(self.rows[idx]["original_path"]).name
        self.status.config(text=f'Saved "{label}" to the list. Click Preview or Apply to write it to the file.')
        self.edit_apply_btn.config(text="✓ Saved")
        self.after(1000, lambda: self.edit_apply_btn.config(text="💾 Save Changes"))

    # ---------- volume + visualizer ----------

    def _on_volume_change(self, value=None):
        if PLAYBACK_AVAILABLE:
            try:
                pygame.mixer.music.set_volume(self.volume.get())
            except Exception:
                pass

    def _animate_visualizer(self):
        if self.currently_playing:
            self._vis_heights = [
                max(3, min(30, h + random.randint(-9, 9))) for h in self._vis_heights
            ]
        else:
            # ease back down to flat rather than snapping, so it doesn't look glitchy
            self._vis_heights = [max(3, h - 3) for h in self._vis_heights]

        self.visualizer_canvas.delete("all")
        w, h, n = 190, 36, self._vis_bar_count
        bar_w = w / n
        color = "#4CAF50" if self.currently_playing else "#3a3a3a"
        for i, bar_h in enumerate(self._vis_heights):
            x0 = i * bar_w + 1
            x1 = x0 + bar_w - 2
            y1 = h - 2
            y0 = y1 - bar_h
            self.visualizer_canvas.create_rectangle(x0, y0, x1, y1, fill=color, outline="")

        self.after(120, self._animate_visualizer)

    # ---------- playback ----------

    def toggle_play(self):
        if not PLAYBACK_AVAILABLE:
            return
        if self.currently_playing:
            self.stop_playback()
            return
        path = self.selected_path
        if not path:
            return
        try:
            pygame.mixer.music.load(path)
            pygame.mixer.music.play()
            pygame.mixer.music.set_volume(self.volume.get())
            self.currently_playing = path
            self.play_btn.config(text="■ Stop")
            self._poll_playback()
        except Exception as e:
            messagebox.showerror("Playback error", str(e))

    def _poll_playback(self):
        if not self.currently_playing:
            return
        if not pygame.mixer.music.get_busy():
            self.stop_playback()
            return
        self.after(500, self._poll_playback)

    def stop_playback(self):
        if PLAYBACK_AVAILABLE:
            try:
                pygame.mixer.music.stop()
            except Exception:
                pass
        self.currently_playing = None
        self.play_btn.config(text="▶ Play")

    # ---------- duplicates ----------

    def find_duplicates(self):
        if not self.rows:
            return

        groups = {}
        for idx, row in enumerate(self.rows):
            title = row.get("title", "").strip().lower()
            if not title:
                continue
            key = (row.get("artist", "").strip().lower(), title)
            groups.setdefault(key, []).append(idx)

        dup_groups = {k: v for k, v in groups.items() if len(v) > 1}

        # reset any stale markings from a previous run before recomputing
        for row in self.rows:
            row["duplicate"] = ""

        removed_count = 0
        for indices in dup_groups.values():
            keep_idx = self._pick_best_duplicate(indices)
            for idx in indices:
                self.rows[idx]["duplicate"] = "keep" if idx == keep_idx else "remove"
                if idx != keep_idx:
                    removed_count += 1

        self._rebuild_tree()
        self._update_dup_button_states()

        if not dup_groups:
            self.status.config(text="No duplicates found (matched by artist + title).")
        else:
            self.status.config(
                text=f"Found {len(dup_groups)} duplicate group(s) — {removed_count} file(s) would be removed, "
                     f"keeping the best copy of each (highlighted in red)."
            )

    def _pick_best_duplicate(self, indices):
        """Pick which row in a duplicate group to keep: prefer one with existing
        embedded art, then confidently-tagged/corrected metadata, then the larger
        file (as a rough proxy for a more complete/higher-quality rip)."""
        def score(idx):
            row = self.rows[idx]
            has_art = row.get("has_art") == "yes"
            confident = row.get("confidence", "").startswith("high")
            try:
                size = Path(row["original_path"]).stat().st_size
            except Exception:
                size = 0
            return (has_art, confident, size)

        return max(indices, key=score)

    def remove_duplicates(self):
        pending = [row for row in self.rows if row.get("duplicate") == "remove"]
        if not pending:
            messagebox.showinfo("No duplicates to remove", "Run \"Find Duplicates\" first.")
            return

        confirmed = messagebox.askyesno(
            "Remove duplicates?",
            f"{len(pending)} duplicate file(s) will be moved into a \"Duplicates\" subfolder "
            f"next to each original — the best copy of each group stays where it is.\n\n"
            f"Files are moved, not deleted, so this is reversible. Continue?",
        )
        if not confirmed:
            return

        moved, errors = 0, []
        handled_paths = set()
        for row in pending:
            src = Path(row["original_path"])
            if not src.exists():
                errors.append(f"{src.name}: file missing")
                handled_paths.add(row["original_path"])
                continue
            try:
                dup_folder = src.parent / "Duplicates"
                dup_folder.mkdir(exist_ok=True)
                dest = dup_folder / src.name
                counter = 1
                while dest.exists():
                    dest = dup_folder / f"{src.stem} ({counter}){src.suffix}"
                    counter += 1
                shutil.move(str(src), str(dest))
                moved += 1
                handled_paths.add(row["original_path"])
            except Exception as e:
                errors.append(f"{src.name}: {e}")

        self.rows = [r for r in self.rows if r["original_path"] not in handled_paths]
        self._rebuild_tree()
        self._update_dup_button_states()

        summary = f"Moved {moved} duplicate file(s) to their \"Duplicates\" folder(s)."
        if errors:
            summary += f" {len(errors)} error(s)."
        self.status.config(text=summary)
        if errors:
            messagebox.showwarning("Finished with some errors", "\n".join(errors[:20]))

    # ---------- fetch album art only ----------

    def start_fetch_art_only(self):
        if not self.rows:
            return

        confirmed = messagebox.askyesno(
            "Fetch album art?",
            f"This will look up and embed cover art into {len(self.rows)} file(s) — "
            f"it won't rename them or change any other tags.\n\nContinue?",
        )
        if not confirmed:
            return

        self.stop_playback()
        self.apply_btn.config(state="disabled")
        self.preview_btn.config(state="disabled")
        self.find_dup_btn.config(state="disabled")
        self.remove_dup_btn.config(state="disabled")
        self.fetch_art_only_btn.config(state="disabled")
        self.status.config(text="Working…")

        force_art = self.force_art.get()
        online_lookup = self.online_lookup.get()
        thread = threading.Thread(target=self._fetch_art_only_worker, args=(force_art, online_lookup), daemon=True)
        thread.start()

    def _fetch_art_only_worker(self, force_art, online_lookup):
        results = []
        added, skipped, not_found, errors = 0, 0, 0, 0
        n = len(self.rows)

        def status_cb(text):
            self.after(0, lambda text=text: self.status.config(text=text))

        for i, row in enumerate(self.rows):
            label = f"{row['artist']} — {row['title']}".strip(" —") or Path(row["original_path"]).name
            text = f"({i + 1}/{n}) Fetching album art for {label}…"
            self.after(0, lambda text=text: self.status.config(text=text))

            result = core.apply_art_only(row, force_art=force_art, improve_metadata=online_lookup, status_cb=status_cb)
            results.append((row, result))

            if result["art"] == "added":
                added += 1
            elif result["art"] and result["art"].startswith("skipped"):
                skipped += 1
            elif result["art"] == "not found":
                not_found += 1
            elif result["art"] and result["art"].startswith("error"):
                errors += 1

            tally = f"art: {added} added, {skipped} skipped, {not_found} not found, {errors} errors"
            self.after(0, lambda i=i, n=n, label=label, tally=tally: self.status.config(
                text=f"({i + 1}/{n}) {label} — {tally}"))

        self.after(0, lambda: self._fetch_art_only_done(results))

    def _fetch_art_only_done(self, results):
        self.apply_btn.config(state="normal")
        self.preview_btn.config(state="normal")
        self.fetch_art_only_btn.config(state="normal")
        self._update_dup_button_states()

        added = sum(1 for _, r in results if r["art"] == "added")
        not_found = sum(1 for _, r in results if r["art"] == "not found")
        errs = [(row, r) for row, r in results if r["art"] and str(r["art"]).startswith("error")]
        missing = [(row, r) for row, r in results if r.get("error") == "file missing"]

        # patch the Has Art column in place for rows that got new art, without a full rescan
        for row, result in results:
            if result["art"] == "added":
                row["has_art"] = "yes"
                item_id = next((iid for iid, idx in self.row_id_to_index.items() if self.rows[idx] is row), None)
                if item_id:
                    self.tree.set(item_id, "has_art", "yes")

        # refresh the art preview panel if the currently-selected track was touched
        if self.selected_path:
            self._update_art_preview(self.selected_path)

        summary = f"Album art: {added} added, {not_found} not found"
        if errs:
            summary += f", {len(errs)} error(s)"
        if missing:
            summary += f", {len(missing)} file(s) missing"
        summary += "."
        self.status.config(text=summary)

        if errs or missing:
            lines = [f"{Path(row['original_path']).name}: {r['art'] or r.get('error')}" for row, r in (errs + missing)]
            messagebox.showwarning("Finished with some errors", "\n".join(lines[:20]))
        else:
            messagebox.showinfo("Done", summary)

    # ---------- apply ----------

    def start_apply(self, dry_run):
        if not self.rows:
            return
        if not dry_run:
            notes = []
            if self.online_lookup.get():
                notes.append("look up unclear tracks online")
            if self.fetch_art.get():
                notes.append("fetch album art")
            note = f" and {', '.join(notes)}" if notes else ""
            confirmed = messagebox.askyesno(
                "Apply changes?",
                f"This will rename and re-tag {len(self.rows)} file(s) in place{note}.\n\n"
                "This can't be automatically undone. Continue?",
            )
            if not confirmed:
                return

        self.stop_playback()
        self.apply_btn.config(state="disabled")
        self.preview_btn.config(state="disabled")
        self.find_dup_btn.config(state="disabled")
        self.remove_dup_btn.config(state="disabled")
        self.fetch_art_only_btn.config(state="disabled")
        self.status.config(text="Working…")

        pattern = self.pattern.get()
        fetch_art = self.fetch_art.get()
        force_art = self.force_art.get()
        online_lookup = self.online_lookup.get()
        thread = threading.Thread(target=self._apply_worker, args=(dry_run, pattern, fetch_art, force_art, online_lookup), daemon=True)
        thread.start()

    def _apply_worker(self, dry_run, pattern, fetch_art, force_art, online_lookup):
        results = []
        art_added, art_skipped, art_not_found, art_errors = 0, 0, 0, 0
        meta_corrected, meta_not_found, meta_errors = 0, 0, 0
        n = len(self.rows)

        def status_cb(text):
            self.after(0, lambda text=text: self.status.config(text=text))

        for i, row in enumerate(self.rows):
            label = f"{row['artist']} — {row['title']}".strip(" —") or Path(row["original_path"]).name
            is_low = row.get("confidence", "").startswith("low")

            if not dry_run:
                if online_lookup and is_low:
                    verb = "Looking up"
                elif fetch_art:
                    verb = "Fetching album art for"
                else:
                    verb = None
                text = f"({i + 1}/{n}) {verb} {label}…" if verb else f"({i + 1}/{n}) {label}"
                self.after(0, lambda text=text: self.status.config(text=text))
            else:
                text = f"({i + 1}/{n}) Previewing {label}…"
                self.after(0, lambda text=text: self.status.config(text=text))

            result = core.apply_row(row, dry_run=dry_run, pattern=pattern, fetch_art=fetch_art,
                                     force_art=force_art, improve_metadata=online_lookup,
                                     status_cb=None if dry_run else status_cb)
            results.append(result)

            tally_parts = []

            if online_lookup and not dry_run and result["metadata"]:
                if result["metadata"] == "corrected":
                    meta_corrected += 1
                elif result["metadata"] == "not found":
                    meta_not_found += 1
                elif result["metadata"].startswith("error"):
                    meta_errors += 1
                tally_parts.append(f"lookup: {meta_corrected} corrected, {meta_not_found} not found, {meta_errors} errors")

            if fetch_art and not dry_run and result["art"]:
                if result["art"] == "added":
                    art_added += 1
                elif result["art"].startswith("skipped"):
                    art_skipped += 1
                elif result["art"] == "not found":
                    art_not_found += 1
                elif result["art"].startswith("error"):
                    art_errors += 1
                tally_parts.append(f"art: {art_added} added, {art_skipped} skipped, {art_not_found} not found, {art_errors} errors")

            if tally_parts and not dry_run:
                tally = " — ".join(tally_parts)
                self.after(0, lambda i=i, n=n, label=label, tally=tally: self.status.config(
                    text=f"({i + 1}/{n}) {label} — {tally}"))

        self.after(0, lambda: self._apply_done(results, dry_run))

    def _find_collisions(self, results):
        """Two things the flat table can't easily show at a glance: two different
        source files both resolving to the identical new name, or a new name that
        would silently overwrite some other file already sitting on disk."""
        dest_map = {}
        for r in results:
            if r.get("dest"):
                dest_map.setdefault(str(r["dest"]).lower(), []).append(r)
        batch_collisions = {k: v for k, v in dest_map.items() if len(v) > 1}

        disk_collisions = []
        for r in results:
            dest = r.get("dest")
            if not dest or str(dest).lower() in batch_collisions:
                continue  # already reported as a batch collision above
            dest_path = Path(dest)
            src_path = Path(r["original_path"])
            try:
                same_file = dest_path.exists() and dest_path.resolve() == src_path.resolve()
            except Exception:
                same_file = False
            if dest_path.exists() and not same_file:
                disk_collisions.append((r, dest_path))

        return batch_collisions, disk_collisions

    def _apply_done(self, results, dry_run):
        self.apply_btn.config(state="normal")
        self.preview_btn.config(state="normal")
        self.fetch_art_only_btn.config(state="normal")
        self._update_dup_button_states()

        renamed = sum(1 for r in results if r["renamed"])
        tagged = sum(1 for r in results if r["tagged"])
        errors = [r for r in results if r["error"]]
        art_added = sum(1 for r in results if r["art"] == "added")
        art_not_found = sum(1 for r in results if r["art"] == "not found")
        art_errors = [r for r in results if r["art"] and str(r["art"]).startswith("error")]
        meta_corrected = sum(1 for r in results if r["metadata"] == "corrected")
        meta_not_found = sum(1 for r in results if r["metadata"] == "not found")
        meta_errors = [r for r in results if r["metadata"] and str(r["metadata"]).startswith("error")]

        if dry_run:
            batch_collisions, disk_collisions = self._find_collisions(results)
            total_problems = sum(len(v) for v in batch_collisions.values()) + len(disk_collisions)

            if total_problems == 0:
                self.status.config(text=f"Preview: checked {len(results)} file(s) — no naming conflicts found, ready to Apply.")
                messagebox.showinfo(
                    "Dry run preview",
                    f"Checked {len(results)} file(s) against the planned new names — no conflicts found.\n\n"
                    f"(The Original Filename / New Filename columns in the table already show exactly "
                    f"what each file will become — this check specifically looks for two files that would "
                    f"collide, or a new name that would overwrite something already on disk.)"
                )
                return

            lines = []
            if batch_collisions:
                lines.append("Multiple files would get the SAME new name:")
                for group in list(batch_collisions.values())[:10]:
                    sources = ", ".join(Path(r["original_path"]).name for r in group)
                    lines.append(f"  \"{Path(group[0]['dest']).name}\"  <-  {sources}")
                if len(batch_collisions) > 10:
                    lines.append(f"  …and {len(batch_collisions) - 10} more group(s).")
            if disk_collisions:
                if lines:
                    lines.append("")
                lines.append("These would overwrite a file that already exists:")
                for r, dest_path in disk_collisions[:10]:
                    lines.append(f"  {Path(r['original_path']).name}  ->  {dest_path.name} (already exists)")
                if len(disk_collisions) > 10:
                    lines.append(f"  …and {len(disk_collisions) - 10} more.")

            self.status.config(text=f"Preview: found {total_problems} naming conflict(s) — see the popup for details.")
            messagebox.showwarning("Dry run preview — conflicts found", "\n".join(lines))
            return

        meta_summary = ""
        if self.online_lookup.get():
            meta_summary = f" Online lookup: {meta_corrected} corrected"
            if meta_not_found:
                meta_summary += f", {meta_not_found} not found"
            if meta_errors:
                meta_summary += f", {len(meta_errors)} error(s)"
            meta_summary += "."

        art_summary = ""
        if self.fetch_art.get():
            art_summary = f" Album art: {art_added} added"
            if art_not_found:
                art_summary += f", {art_not_found} not found"
            if art_errors:
                art_summary += f", {len(art_errors)} error(s)"
            art_summary += "."

        self.status.config(text=f"Done. Renamed {renamed}, tagged {tagged} file(s)." +
                            (f" {len(errors)} error(s)." if errors else "") + meta_summary + art_summary)

        if errors:
            detail = "\n".join(f"{Path(e['original_path']).name}: {e['error']}" for e in errors[:20])
            messagebox.showwarning("Finished with some errors", detail)
        else:
            messagebox.showinfo("Done", f"Renamed {renamed} file(s) and updated tags on {tagged} file(s)." + meta_summary + art_summary)

        # rescan to reflect new filenames/tags on disk
        if self.folder.get():
            self.start_scan()
        else:
            # library was built from drops, not a folder — rescan those exact (now renamed) paths
            new_paths = [r["dest"] for r in results if r.get("dest")]
            if new_paths:
                pattern = self.pattern.get()
                thread = threading.Thread(target=self._scan_paths_worker_replace, args=(new_paths, pattern), daemon=True)
                thread.start()

    def _scan_paths_worker_replace(self, paths, pattern):
        try:
            rows = core.scan_paths(paths, pattern=pattern)
        except Exception:
            return
        self.after(0, lambda: self._scan_done(rows))


def gui_main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in ("scan", "apply"):
        cli_main()
    else:
        gui_main()
