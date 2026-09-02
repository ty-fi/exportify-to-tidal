#!/usr/bin/env python3
"""
exportify_to_tidal.py — build a Tidal playlist from a watsonbox Exportify CSV.

Expects the default column set from https://exportify.app/ :

    Track URI, Track Name, Artist URI(s), Artist Name(s), Album URI, Album Name,
    Album Artist URI(s), Album Artist Name(s), Album Release Date, Album Image URL,
    Disc Number, Track Number, Track Duration (ms), Track Preview URL, Explicit,
    Popularity, ISRC, Added By, Added At

Matching runs in two passes, both through a single tidalapi session — there is no
second auth flow and no developer app to register:

  1. ISRC lookup via ``Session.get_tracks_by_isrc``, which hits TIDAL's official
     openapi v2 catalogue with ``filter[isrc]``. Exact, high precision.

  2. Text search, scored on title / artist / duration / album agreement. Duration
     is a corroborating signal plus one hard check — a candidate more than 3x the
     tolerance from the expected length is a different recording, not a near miss.
     Search hits whose own ISRC matches the source are promoted to exact matches.

Anything that doesn't clear --min-score is left unmatched rather than guessed at,
and written to a sidecar CSV for reconciliation. Nothing is silently substituted.

Setup:
    uv sync

With no CSV named, it scans `exportify-exports/` and ports every export not
already in the processed ledger (`~/.config/exportify_to_tidal/processed.json`),
oldest first, deriving each playlist name from the filename. Re-running is safe:
anything already ported is skipped rather than duplicated.

Usage:
    # port everything new in exportify-exports/
    uv run exportify_to_tidal.py

    # what would it do? (no Tidal login needed)
    uv run exportify_to_tidal.py --list

    # one specific file, with an explicit name
    uv run exportify_to_tidal.py playlist.csv --name "Ben's Mix"

    # match and report, but write nothing to Tidal (still needs a Tidal login,
    # because the ISRC lookup goes through the session)
    uv run exportify_to_tidal.py --dry-run

    # record exports you already ported by hand, so they're not done twice
    uv run exportify_to_tidal.py --mark-done

    # exact matches or nothing
    uv run exportify_to_tidal.py playlist.csv --isrc-only

    # tuning the scorer: bypass the match cache so results actually change
    uv run exportify_to_tidal.py playlist.csv --dry-run --min-score 0.85 --no-cache
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import math
import re
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

try:
    import tidalapi
    from tidalapi.exceptions import InvalidISRC, ObjectNotFound, TidalAPIError
except ImportError:
    sys.exit("tidalapi is not installed. Run: uv sync")


# Windows consoles default to cp1252, which can't encode the status glyphs below
# — nor most of the track titles we echo straight out of the CSV. Without this,
# the first accented artist name kills the run.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")


CONFIG_DIR = Path.home() / ".config" / "exportify_to_tidal"
SESSION_FILE = CONFIG_DIR / "session.json"
MATCH_CACHE = CONFIG_DIR / "matches.json"
LEDGER_FILE = CONFIG_DIR / "processed.json"

# Where Export All zips get unpacked. Scanned when no CSV is named on the CLI.
DEFAULT_EXPORTS_DIR = Path("exportify-exports")

# Words left lower case when title-casing a slugified filename.
SMALL_WORDS = {
    "a", "an", "and", "at", "but", "by", "for", "from", "in", "nor", "of",
    "on", "or", "the", "to", "vs", "with",
}

# Bumped when a cache entry's *meaning* changes, not just its layout — a
# mismatch discards the cache rather than trusting stale entries.
#   2: settings-aware entries ({tidal_id, method, score, params}).
#   3: ISRC ties now resolve by release (D12/isrc_candidate_rank), so v2 entries
#      can name a different release than the current ranking would choose. They
#      were written with params=None, i.e. always-reusable, so without this bump
#      a normal run would silently resurrect the old picks.
CACHE_VERSION = 3

# Exportify writes these exact headers. Fail loudly if they've drifted.
# "Artist URI(s)" is also the tell-tale that distinguishes exportify.app from
# exportify.net, so requiring it gives a better error on the wrong tool.
REQUIRED_COLUMNS = {
    "Track Name",
    "Artist URI(s)",
    "Artist Name(s)",
    "Album Name",
    "Track Duration (ms)",
    "ISRC",
}

# Noise that shows up in Spotify titles but not always in Tidal's, and vice versa.
NOISE = re.compile(
    r"""\s*[\(\[-]\s*(
        (\d{4}\s+)?(digital\s+)?(re)?master(ed)?(\s+\d{4})?
        | remaster(ed)?(\s+version)?
        | deluxe(\s+edition)?
        | expanded(\s+edition)?
        | bonus\s+track(\s+version)?
        | single\s+version
        | album\s+version
        | mono | stereo
    )\s*[\)\]]?\s*$""",
    re.IGNORECASE | re.VERBOSE,
)


@dataclass
class SourceTrack:
    """One row of the Exportify CSV."""

    title: str
    artists: list[str]
    album: str
    duration_ms: int
    isrc: str
    spotify_uri: str
    row: int

    @property
    def primary_artist(self) -> str:
        return self.artists[0] if self.artists else ""

    def describe(self) -> str:
        return f"{', '.join(self.artists)} — {self.title}"


@dataclass
class MatchResult:
    source: SourceTrack
    tidal_id: int | None = None
    method: str = "unmatched"
    score: float = 0.0
    note: str = ""


@dataclass
class Stats:
    by_isrc: int = 0
    by_search_isrc: int = 0
    by_search: int = 0
    from_cache: int = 0
    warnings: list[MatchResult] = field(default_factory=list)
    unmatched: list[MatchResult] = field(default_factory=list)


# ---------------------------------------------------------------- normalisation


def normalize(text: str) -> str:
    """Casefold, strip accents and punctuation, collapse whitespace."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.casefold()
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def strip_noise(text: str) -> str:
    """Drop trailing remaster/deluxe/edition markers that differ between catalogues.

    Applies to album names as much as titles — "Nevermind (Remastered)" vs
    "Nevermind" otherwise halves the album component of the score.
    """
    prev = None
    while prev != text:
        prev = text
        text = NOISE.sub("", text).strip()
    return text


def token_overlap(a: str, b: str) -> float:
    """Jaccard similarity over word tokens. Cheap, and good enough here."""
    ta, tb = set(normalize(a).split()), set(normalize(b).split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def fuzzy_overlap(a: str, b: str) -> float:
    """Token overlap, taking the better of raw and noise-stripped forms."""
    return max(token_overlap(a, b), token_overlap(strip_noise(a), strip_noise(b)))


# ---------------------------------------------------------------- reading input


def parse_artists(raw_names: str, raw_uris: str) -> list[str]:
    """Split Exportify's "Artist Name(s)" column.

    Exportify joins names with ", ", which is ambiguous when a name contains a
    comma — "Earth, Wind & Fire" would otherwise split into two artists and the
    search query would go out as just "Earth". "Artist URI(s)" gives the true
    count, so only trust the split when the two agree.
    """
    names = raw_names.strip()
    if not names:
        return []

    parts = [p.strip() for p in names.split(",") if p.strip()]
    uri_count = len([u for u in raw_uris.split(",") if u.strip()])

    if not uri_count:
        return parts  # no URI column to cross-check against; assume the split
    if len(parts) == uri_count:
        return parts
    return [names]  # split disagrees with the URI count — keep the string whole


def read_csv(path: Path) -> list[SourceTrack]:
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        headers = set(reader.fieldnames or [])

        missing = REQUIRED_COLUMNS - headers
        if missing:
            sys.exit(
                f"CSV is missing expected columns: {', '.join(sorted(missing))}\n"
                f"Found: {', '.join(sorted(headers))}\n"
                "This script expects output from exportify.app (watsonbox), not "
                "exportify.net (pavelkomarov) — the latter has no ISRC column."
            )

        tracks: list[SourceTrack] = []
        for i, row in enumerate(reader, start=2):  # row 1 is the header
            title = (row.get("Track Name") or "").strip()
            if not title:
                continue  # local files and removed tracks come through blank

            try:
                duration_ms = int(float(row.get("Track Duration (ms)") or 0))
            except ValueError:
                duration_ms = 0

            tracks.append(
                SourceTrack(
                    title=title,
                    artists=parse_artists(
                        row.get("Artist Name(s)") or "", row.get("Artist URI(s)") or ""
                    ),
                    album=(row.get("Album Name") or "").strip(),
                    duration_ms=duration_ms,
                    isrc=(row.get("ISRC") or "").strip().upper(),
                    spotify_uri=(row.get("Track URI") or "").strip(),
                    row=i,
                )
            )
        return tracks


# ---------------------------------------------------------------- candidate maths


def duration_delta_s(source: SourceTrack, candidate) -> float:
    """Absolute duration difference in seconds, or inf if either side is unknown."""
    cand_s = getattr(candidate, "duration", None) or 0
    if not source.duration_ms or not cand_s:
        return math.inf
    return abs(cand_s * 1000 - source.duration_ms) / 1000.0


def album_name(candidate) -> str:
    return getattr(getattr(candidate, "album", None), "name", "") or ""


def album_overlap(source: SourceTrack, candidate) -> float:
    cand_album = album_name(candidate)
    if not source.album or not cand_album:
        return 0.0
    return fuzzy_overlap(source.album, cand_album)


def isrc_candidate_rank(source: SourceTrack, candidate) -> tuple:
    """Sort key for choosing among tracks that share an ISRC.

    They are the same recording by definition, so this is really choosing which
    *release* the playlist entry points at — which decides the album art and
    attribution you end up looking at.

    Ordered by: length agreement, then album lineage (noise-stripped, so every
    "Nevermind (… Edition)" ties), then the raw album string so the exact edition
    the source named wins over a bare reissue, then the least edition cruft. The
    trailing id keeps the choice stable if TIDAL reorders its response — without
    it the same CSV can resolve to a different release from one run to the next.
    """
    return (
        duration_delta_s(source, candidate),
        -album_overlap(source, candidate),
        -token_overlap(source.album, album_name(candidate)),
        len(album_name(candidate)),
        candidate.id,
    )


def candidate_isrc(candidate) -> str:
    """A search hit's own ISRC, when the payload happens to carry one."""
    return (getattr(candidate, "isrc", None) or "").strip().upper()


def score_candidate(source: SourceTrack, candidate, duration_tolerance: int) -> float:
    """
    Score a Tidal search hit against the source row, 0.0–1.0.

    Weighted title 0.4 / artist 0.3 / duration 0.2 / album 0.1, plus one hard
    check: a candidate more than 3x the tolerance from the expected length is a
    different recording — a cover, a remix, an extended mix, or simply another
    song with the same title — not a near miss, so it scores zero.

    When either side has no duration at all, that weight is redistributed across
    the text signals rather than scored as zero. A candidate shouldn't be
    penalised for metadata we never had; scoring it zero capped the total at
    0.80 and made the outcome depend on where the threshold happened to sit.
    """
    title_score = fuzzy_overlap(source.title, candidate.name)
    artist_score = token_overlap(
        " ".join(source.artists),
        " ".join(a.name for a in (candidate.artists or [])),
    )
    album_score = album_overlap(source, candidate)

    delta = duration_delta_s(source, candidate)

    if not math.isfinite(delta):
        # No length on one side. Renormalise over the weights we can actually
        # evaluate (0.40 + 0.30 + 0.10 = 0.80) so a full text agreement still
        # reaches 1.0 and a partial one still has to clear the threshold on merit.
        total = (0.40 * title_score + 0.30 * artist_score + 0.10 * album_score) / 0.80
        return min(total, 1.0)

    if delta > duration_tolerance * 3:
        return 0.0  # different recording, not a near miss

    tolerance = max(duration_tolerance, 1)
    if delta <= tolerance:
        duration_score = 1.0 - (delta / tolerance) * 0.5  # 1.0 → 0.5
    else:
        # Taper 0.5 → 0.0 across the tolerance..3x band.
        duration_score = 0.5 * (1.0 - (delta - tolerance) / (2.0 * tolerance))

    total = (
        0.40 * title_score
        + 0.30 * artist_score
        + 0.20 * duration_score
        + 0.10 * album_score
    )
    # The weights sum to 1.0 in decimal but to 1.0000000000000002 in binary,
    # so clamp to keep the documented range honest.
    return min(total, 1.0)


# ---------------------------------------------------------------- ISRC lookup


def isrc_lookup(
    session, source: SourceTrack, duration_tolerance: int, debug: bool
) -> tuple[int | None, str]:
    """Resolve a Tidal track id from the source ISRC.

    Returns (track_id, warning). ISRC is the authoritative join key (D2), so a
    duration disagreement here is surfaced rather than used to reject — unlike
    the text scorer, where it's a gate.
    """
    try:
        candidates = session.get_tracks_by_isrc(source.isrc)
    except (ObjectNotFound, InvalidISRC):
        # tidalapi's docstring claims an empty list is returned when nothing
        # matches. It raises instead. Don't trust the docstring.
        return None, ""
    except TidalAPIError as exc:
        if debug:
            print(f"    isrc lookup failed: {exc!r}")
        return None, ""

    if not candidates:
        return None, ""

    # Several Tidal tracks can share an ISRC — same recording, different album
    # or regional release. See isrc_candidate_rank for how the winner is chosen.
    best = min(candidates, key=lambda c: isrc_candidate_rank(source, c))

    if debug:
        # One ISRC routinely resolves to the same recording on half a dozen
        # releases, all identical in length. Show the album and both overlaps
        # (noise-stripped / raw), or the tie-break that picked the winner is
        # invisible.
        for c in candidates:
            marker = "*" if c.id == best.id else " "
            c_delta = duration_delta_s(source, c)
            shown = "?" if not math.isfinite(c_delta) else f"{c_delta:.2f}s"
            print(
                f"   {marker} isrc hit {c.id}: {c.name} "
                f"({c.duration}s, Δ{shown}) — {album_name(c) or '?'} "
                f"[album {album_overlap(source, c):.2f}"
                f"/{token_overlap(source.album, album_name(c)):.2f}]"
            )

    delta = duration_delta_s(source, best)
    warning = ""
    if math.isfinite(delta) and delta > duration_tolerance * 3:
        warning = f"ISRC matched but duration differs by {delta:.0f}s"

    return best.id, warning


# ---------------------------------------------------------------- text fallback


def search_match(
    session, source: SourceTrack, min_score: float, duration_tolerance: int, debug: bool
) -> tuple[int | None, float, bool]:
    """Scored text search.

    Returns (track_id, score, isrc_confirmed). A hit whose own ISRC equals the
    source ISRC is promoted to an exact match and skips scoring entirely — the
    identifier is stronger evidence than any combination of text and length.

    A *differing* ISRC is deliberately not treated as disqualifying: the same
    recording legitimately carries different ISRCs across distributors, so a
    mismatch is weak evidence, not a veto.
    """
    queries = [
        f"{source.title} {source.primary_artist}",
        f"{strip_noise(source.title)} {source.primary_artist}",
    ]

    best_id, best_score = None, 0.0
    seen: set[int] = set()

    for query in dict.fromkeys(queries):  # dedupe, preserve order
        try:
            results = session.search(query, models=[tidalapi.media.Track], limit=20)
        except Exception as exc:
            if debug:
                print(f"    search failed for {query!r}: {exc}")
            continue

        for candidate in results.get("tracks", []) or []:
            if candidate.id in seen:
                continue
            seen.add(candidate.id)

            if source.isrc and candidate_isrc(candidate) == source.isrc:
                if debug:
                    print(f"    isrc-confirmed in search: {candidate.name}")
                return candidate.id, 1.0, True

            score = score_candidate(source, candidate, duration_tolerance)
            if debug:
                delta = duration_delta_s(source, candidate)
                shown = "?" if not math.isfinite(delta) else f"{delta:.0f}s"
                print(f"    {score:.2f} (Δ{shown})  {candidate.name} [{candidate.id}]")
            if score > best_score:
                best_id, best_score = candidate.id, score

        if best_score >= 0.9:
            break  # good enough, stop burning requests

    if best_score >= min_score:
        return best_id, best_score, False
    return None, best_score, False


# ---------------------------------------------------------------- tidal session


def load_session() -> "tidalapi.Session":
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    session = tidalapi.Session()
    # Handles both halves: reuse the cached session file, or run the device-code
    # login and write one. Preferred over hand-rolling load_oauth_session, which
    # drops the token expiry.
    if not session.login_session_file(SESSION_FILE):
        sys.exit("Tidal login failed.")
    return session


# ---------------------------------------------------------------- match cache


def search_fingerprint(args) -> str:
    """Scorer settings a cached *search* result depends on."""
    return f"min={args.min_score}:tol={args.duration_tolerance}"


def load_cache() -> dict:
    if MATCH_CACHE.exists():
        try:
            raw = json.loads(MATCH_CACHE.read_text(encoding="utf-8"))
            if raw.get("version") == CACHE_VERSION:
                return raw.get("entries") or {}
        except Exception:
            pass
    return {}


def save_cache(entries: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    MATCH_CACHE.write_text(
        json.dumps({"version": CACHE_VERSION, "entries": entries}, indent=2),
        encoding="utf-8",
    )


def cache_lookup(entries: dict, key: str, fingerprint: str) -> dict | None:
    """A reusable cache entry, or None.

    ISRC matches are independent of the scorer settings, so they always apply.
    Search matches are not — reusing one across a --min-score change would
    silently defeat the tuning it was changed for.
    """
    entry = entries.get(key)
    if not entry or not entry.get("tidal_id"):
        return None
    if entry.get("params") in (None, fingerprint):
        return entry
    return None


# ---------------------------------------------------------- processed ledger


def file_digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def load_ledger() -> dict:
    if LEDGER_FILE.exists():
        try:
            return json.loads(LEDGER_FILE.read_text(encoding="utf-8")) or {}
        except Exception:
            pass
    return {}


def save_ledger(entries: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    LEDGER_FILE.write_text(json.dumps(entries, indent=2), encoding="utf-8")


def playlist_name_from_filename(path: Path) -> str:
    """Derive a playlist name from an Exportify filename.

    Exportify slugifies, so "95 Dodge Caravan" arrives as "95_dodge_caravan.csv"
    and the original casing is unrecoverable. If the stem has no capitals at all
    it was clearly slugified and gets title-cased; anything with existing capitals
    is left alone, since the case is probably meaningful (acronyms, band names).
    --name overrides either way.
    """
    stem = path.stem
    words = [w for w in re.split(r"[\s_-]+", stem) if w]
    if not words:
        return stem
    if any(c.isupper() for c in stem):
        return " ".join(words)

    out = []
    for i, word in enumerate(words):
        first_or_last = i == 0 or i == len(words) - 1
        if word in SMALL_WORDS and not first_or_last:
            out.append(word)
        else:
            out.append(word[:1].upper() + word[1:])
    return " ".join(out)


def discover(
    exports_dir: Path, ledger: dict, reprocess: bool
) -> tuple[list[Path], list[tuple[Path, str]]]:
    """Split the export directory into (queue, skipped-with-reason).

    Ordered by mtime then name: chronological when the timestamps mean anything,
    alphabetical when a zip extract flattened them all to the same instant.

    A CSV whose content changed since it was ported is *skipped*, not requeued —
    re-running it would create a second Tidal playlist rather than update the
    first, and that's a sync problem this tool doesn't pretend to solve.
    """
    if not exports_dir.is_dir():
        return [], []

    candidates = sorted(
        (
            p
            for p in exports_dir.glob("*.csv")
            # *_unmatched.csv is our own output — never treat it as an input.
            if not p.name.endswith("_unmatched.csv")
        ),
        key=lambda p: (p.stat().st_mtime, p.name),
    )

    queue: list[Path] = []
    skipped: list[tuple[Path, str]] = []
    for path in candidates:
        entry = ledger.get(path.name)
        if entry and not reprocess:
            if entry.get("sha256") == file_digest(path):
                ported = entry.get("playlist_name") or "?"
                skipped.append((path, f"already ported as '{ported}'"))
            else:
                skipped.append(
                    (path, "changed since it was ported — pass --reprocess to port again")
                )
            continue
        queue.append(path)
    return queue, skipped


# ---------------------------------------------------------------- resolution


def resolve(tracks: Iterable[SourceTrack], session, args) -> tuple[list[MatchResult], Stats]:
    entries = {} if args.no_cache else load_cache()
    fingerprint = search_fingerprint(args)
    results: list[MatchResult] = []
    stats = Stats()

    for n, source in enumerate(tracks, start=1):
        print(f"[{n}] {source.describe()}")

        cache_key = source.isrc or source.spotify_uri
        cached = cache_lookup(entries, cache_key, fingerprint) if cache_key else None
        if cached:
            result = MatchResult(
                source, cached["tidal_id"], f"{cached['method']} (cached)", cached["score"]
            )
            print(f"    → {result.tidal_id} via {result.method}")
            stats.from_cache += 1
            results.append(result)
            continue

        result = MatchResult(source)
        cache_params: str | None = None

        # Pass 1: ISRC, the primary join key.
        if source.isrc:
            tidal_id, warning = isrc_lookup(
                session, source, args.duration_tolerance, args.debug
            )
            time.sleep(args.delay)
            if tidal_id:
                result = MatchResult(source, tidal_id, "isrc", 1.0, warning)
                stats.by_isrc += 1
                print(f"    → {tidal_id} via ISRC {source.isrc}")
                if warning:
                    print(f"    ! {warning}")
                    stats.warnings.append(result)

        # Pass 2: scored text search.
        if result.tidal_id is None and not args.isrc_only:
            tidal_id, score, isrc_confirmed = search_match(
                session, source, args.min_score, args.duration_tolerance, args.debug
            )
            time.sleep(args.delay)
            if tidal_id and isrc_confirmed:
                result = MatchResult(source, tidal_id, "search+isrc", 1.0)
                stats.by_search_isrc += 1
                print(f"    → {tidal_id} via search, ISRC-confirmed")
            elif tidal_id:
                result = MatchResult(source, tidal_id, "search", score)
                stats.by_search += 1
                cache_params = fingerprint
                print(f"    → {tidal_id} via search (score {score:.2f})")
            else:
                result.score = score
                result.note = (
                    f"best search score {score:.2f} < {args.min_score}"
                    if score
                    else "no candidates, or all refused as a different recording"
                )

        if result.tidal_id is None:
            print(f"    ✗ unmatched — {result.note or 'no ISRC match, search skipped'}")
            stats.unmatched.append(result)
        elif cache_key and not args.no_cache:
            # Negatives are deliberately not cached: the catalogue changes, and a
            # visible gap is the thing most worth retrying on the next run.
            entries[cache_key] = {
                "tidal_id": result.tidal_id,
                "method": result.method,
                "score": result.score,
                "params": cache_params,
            }

        results.append(result)

    if not args.no_cache:
        save_cache(entries)
    return results, stats


def write_unmatched(path: Path, unmatched: list[MatchResult]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            ["CSV Row", "Track Name", "Artist Name(s)", "Album Name", "ISRC", "Reason"]
        )
        for result in unmatched:
            s = result.source
            writer.writerow(
                [s.row, s.title, ", ".join(s.artists), s.album, s.isrc, result.note]
            )


# ---------------------------------------------------------------- porting


def port_one(csv_path: Path, session, args) -> dict | None:
    """Match one CSV and, unless --dry-run, create the Tidal playlist.

    Returns a ledger-shaped summary when a playlist was actually created, else
    None. Callers treat None as "nothing to record" — a dry run, an empty CSV, or
    nothing matched.
    """
    name = args.name or playlist_name_from_filename(csv_path)
    print(f"\n{'=' * 64}\n{csv_path.name}  →  '{name}'\n{'=' * 64}")

    tracks = read_csv(csv_path)
    if not tracks:
        print("No usable rows in that CSV; skipping.")
        return None

    with_isrc = sum(1 for t in tracks if t.isrc)
    print(f"{len(tracks)} tracks, {with_isrc} with an ISRC.\n")

    results, stats = resolve(tracks, session, args)
    matched = [r for r in results if r.tidal_id]

    print(
        f"\nMatched {len(matched)}/{len(tracks)} — "
        f"{stats.by_isrc} by ISRC, {stats.by_search_isrc} ISRC-confirmed in search, "
        f"{stats.by_search} by score, {stats.from_cache} from cache"
    )

    if stats.warnings:
        print(f"{len(stats.warnings)} matched with a warning — worth eyeballing:")
        for r in stats.warnings:
            print(f"  row {r.source.row}: {r.source.describe()} — {r.note}")

    if stats.unmatched:
        out = csv_path.with_name(csv_path.stem + "_unmatched.csv")
        write_unmatched(out, stats.unmatched)
        print(f"{len(stats.unmatched)} unmatched — written to {out}")

    if args.dry_run:
        print("Dry run — no playlist created.")
        return None
    if not matched:
        print("Nothing matched, so no playlist was created.")
        return None

    playlist = session.user.create_playlist(name, args.description)

    # add() takes string ids and caps each call at 100.
    ids = [str(r.tidal_id) for r in matched]
    added: list[int] = []
    for i in range(0, len(ids), 100):
        added.extend(playlist.add(ids[i : i + 100]))
        time.sleep(1)

    print(f"Created '{name}' with {len(added)} tracks (playlist {playlist.id}).")
    if len(added) != len(ids):
        print(
            f"! {len(ids) - len(added)} of {len(ids)} matched tracks were not added "
            "— Tidal rejected them, possibly as duplicates or region-locked."
        )

    return {
        "sha256": file_digest(csv_path),
        "processed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "playlist_id": str(playlist.id),
        "playlist_name": name,
        "tracks": len(tracks),
        "matched": len(added),
    }


# ---------------------------------------------------------------- main


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Port Exportify CSVs to Tidal playlists. With no CSV named, "
        "scans the exports directory and ports everything not yet done.",
    )
    parser.add_argument(
        "csv",
        nargs="?",
        type=Path,
        help="A single Exportify CSV. Omit to auto-discover unprocessed exports.",
    )
    parser.add_argument(
        "--exports-dir",
        type=Path,
        default=DEFAULT_EXPORTS_DIR,
        help=f"Directory scanned when no CSV is named (default: {DEFAULT_EXPORTS_DIR})",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        dest="list_only",
        help="Show what would be ported, then stop. Needs no Tidal login.",
    )
    parser.add_argument(
        "--reprocess",
        action="store_true",
        help="Ignore the processed ledger. Creates a *new* playlist; it does not "
        "update the one made last time.",
    )
    parser.add_argument(
        "--mark-done",
        action="store_true",
        help="Record the queued CSVs as already ported without contacting Tidal. "
        "For playlists you ported by hand, or before the ledger existed.",
    )
    parser.add_argument("--name", help="Override the playlist name (single CSV only)")
    parser.add_argument("--description", default="", help="Playlist description")
    parser.add_argument(
        "--min-score",
        type=float,
        default=0.72,
        help="Fallback match threshold, 0-1. Raise it if you see bad substitutions.",
    )
    parser.add_argument(
        "--duration-tolerance",
        type=int,
        default=4,
        help="Seconds of duration drift treated as the same recording. Candidates "
        "further than 3x this are refused as a different recording.",
    )
    parser.add_argument(
        "--isrc-only",
        action="store_true",
        help="Skip text-search fallback entirely — exact matches or nothing",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Ignore and don't update the match cache. Use this when tuning "
        "--min-score or --duration-tolerance.",
    )
    parser.add_argument(
        "--delay", type=float, default=1.0, help="Seconds between lookups"
    )
    parser.add_argument("--dry-run", action="store_true", help="Match but don't write")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    # tidalapi logs a WARNING for every unavailable variant an ISRC lookup turns
    # up — routinely a dozen per track, and all of them expected, since it drops
    # them before we ever score. Python's last-resort handler dumps those to
    # stderr and buries the actual match output, so quiet the logger by default.
    logging.getLogger("tidalapi").setLevel(
        logging.INFO if args.debug else logging.ERROR
    )

    ledger = load_ledger()

    if args.csv:
        if not args.csv.exists():
            sys.exit(f"No such file: {args.csv}")
        queue, skipped = [args.csv], []
        prior = ledger.get(args.csv.name)
        if prior and not args.reprocess:
            print(
                f"Note: {args.csv.name} was already ported as "
                f"'{prior.get('playlist_name')}' on {prior.get('processed_at')}. "
                "Porting again because you named it explicitly.\n"
            )
    else:
        if not args.exports_dir.is_dir():
            sys.exit(
                f"No exports directory: {args.exports_dir}\n"
                "Name a CSV explicitly, or point --exports-dir somewhere else."
            )
        queue, skipped = discover(args.exports_dir, ledger, args.reprocess)

    if args.name and len(queue) > 1:
        sys.exit(
            f"--name applies to one playlist, but {len(queue)} are queued. "
            "Name a single CSV, or drop --name and let the filenames decide."
        )

    if skipped:
        print(f"Skipping {len(skipped)}:")
        for path, reason in skipped:
            print(f"  - {path.name}: {reason}")
        print()

    if not queue:
        print("Nothing to port — everything in that directory is already done.")
        return

    print(f"{len(queue)} to port, oldest first:")
    for i, path in enumerate(queue, start=1):
        print(f"  {i}. {path.name}  →  '{args.name or playlist_name_from_filename(path)}'")

    if args.list_only:
        return

    if args.mark_done:
        for path in queue:
            ledger[path.name] = {
                "sha256": file_digest(path),
                "processed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "playlist_id": None,  # ported outside this tool
                "playlist_name": args.name or playlist_name_from_filename(path),
                "tracks": None,
                "matched": None,
                "note": "marked done manually, not ported by this run",
            }
        save_ledger(ledger)
        print(f"\nMarked {len(queue)} as done. Nothing was sent to Tidal.")
        return

    session = load_session()

    ported: list[dict] = []
    failed: list[tuple[Path, Exception]] = []
    for path in queue:
        try:
            entry = port_one(path, session, args)
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            # One bad CSV shouldn't cost the rest of the batch. It stays out of
            # the ledger, so the next run retries it.
            print(f"\n! {path.name} failed: {exc!r}")
            failed.append((path, exc))
            continue

        if entry:
            ledger[path.name] = entry
            save_ledger(ledger)  # after each, so an interrupt keeps what's done
            ported.append(entry)

    if len(queue) > 1 or failed:
        print(f"\n{'=' * 64}")
        print(f"Ported {len(ported)}/{len(queue)}")
        for entry in ported:
            print(
                f"  ✓ {entry['playlist_name']} — "
                f"{entry['matched']}/{entry['tracks']} tracks "
                f"(playlist {entry['playlist_id']})"
            )
        for path, exc in failed:
            print(f"  ✗ {path.name} — {exc!r}")

    if args.dry_run:
        print("\nDry run — nothing written to Tidal, ledger unchanged.")


if __name__ == "__main__":
    main()
