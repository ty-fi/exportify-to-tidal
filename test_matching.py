#!/usr/bin/env python3
"""
Offline tests for the matching logic. No network, no Tidal login.

    uv run test_matching.py

Plain asserts rather than pytest so this stays a zero-dependency smoke check.
The point is to make --min-score / --duration-tolerance safe to tune without
having to re-run a real playlist against the API to find out what changed.
"""

from __future__ import annotations

import math
import os
import sys
import tempfile
from pathlib import Path

from tidalapi.exceptions import ObjectNotFound

from exportify_to_tidal import (
    SourceTrack,
    album_overlap,
    cache_lookup,
    discover,
    duration_delta_s,
    file_digest,
    isrc_lookup,
    parse_artists,
    playlist_name_from_filename,
    score_candidate,
    search_match,
    strip_noise,
)

TOLERANCE = 4  # seconds, the default


# ---------------------------------------------------------------- fakes


class FakeArtist:
    def __init__(self, name: str):
        self.name = name


class FakeAlbum:
    def __init__(self, name: str):
        self.name = name


class FakeTrack:
    """Mimics the tidalapi Track surface the scorer touches."""

    def __init__(self, id, name, duration=None, artists=(), album=None, isrc=None):
        self.id = id
        self.name = name
        self.duration = duration  # seconds, as tidalapi reports it
        self.artists = [FakeArtist(a) for a in artists]
        self.album = FakeAlbum(album) if album else None
        self.isrc = isrc


class FakeSession:
    def __init__(self, isrc_result=None, isrc_raises=None, search_results=None):
        self._isrc_result = isrc_result or []
        self._isrc_raises = isrc_raises
        self._search_results = search_results or []
        self.searches: list[str] = []

    def get_tracks_by_isrc(self, isrc):
        if self._isrc_raises:
            raise self._isrc_raises
        return self._isrc_result

    def search(self, query, models=None, limit=None):
        self.searches.append(query)
        return {"tracks": self._search_results}


def src(title, artists, album, duration_ms, isrc=""):
    return SourceTrack(
        title=title,
        artists=list(artists),
        album=album,
        duration_ms=duration_ms,
        isrc=isrc,
        spotify_uri="spotify:track:test",
        row=2,
    )


PASSES: list[str] = []
FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSES.append(label)
        print(f"  ok    {label}")
    else:
        FAILURES.append(f"{label}: {detail}")
        print(f"  FAIL  {label}  {detail}")


# ---------------------------------------------------------------- artist parsing


def test_parse_artists():
    print("\nparse_artists — Exportify's comma-joined names")

    check(
        "single artist",
        parse_artists("Sigur Rós", "spotify:artist:s1") == ["Sigur Rós"],
        parse_artists("Sigur Rós", "spotify:artist:s1"),
    )
    # The regression that sent "September Earth" to the search API.
    got = parse_artists("Earth, Wind & Fire", "spotify:artist:s3")
    check("comma inside one artist name stays whole", got == ["Earth, Wind & Fire"], got)

    got = parse_artists("Jay-Z, Alicia Keys", "spotify:artist:a,spotify:artist:b")
    check("two artists split when URI count agrees", got == ["Jay-Z", "Alicia Keys"], got)

    got = parse_artists("Earth, Wind & Fire, Jay-Z", "spotify:artist:a,spotify:artist:b")
    check(
        "ambiguous split falls back to whole string",
        got == ["Earth, Wind & Fire, Jay-Z"],
        got,
    )

    check("empty is empty", parse_artists("", "") == [], parse_artists("", ""))
    got = parse_artists("A, B", "")
    check("no URI column assumes the split", got == ["A", "B"], got)


# ---------------------------------------------------------------- noise stripping


def test_strip_noise_on_albums():
    print("\nstrip_noise — now applied to albums, not just titles")

    check(
        "album remaster marker stripped",
        strip_noise("Nevermind (Remastered)") == "Nevermind",
        strip_noise("Nevermind (Remastered)"),
    )
    source = src("Drain You", ["Nirvana"], "Nevermind (Remastered)", 223_000)
    cand = FakeTrack(1, "Drain You", 223, ["Nirvana"], "Nevermind")
    overlap = album_overlap(source, cand)
    check(
        "real case from 95_dodge_caravan.csv scores 1.0, not 0.5",
        overlap == 1.0,
        f"got {overlap}",
    )


# ---------------------------------------------------------------- duration gate


def test_duration_scoring():
    print("\nscore_candidate — duration corroborates, and vetoes far outliers")

    source = src("Drain You", ["Nirvana"], "Nevermind (Remastered)", 223_000)

    exact = FakeTrack(1, "Drain You", 223, ["Nirvana"], "Nevermind")
    score = score_candidate(source, exact, TOLERANCE)
    check(
        "perfect match scores 1.0",
        math.isclose(score, 1.0) and score <= 1.0,
        f"got {score!r}",
    )

    # A different recording — cover, remix, extended mix, or the wrong song
    # sharing a title. Length is the cheapest way to catch these.
    other_recording = FakeTrack(3, "Drain You", 223 + 45, ["Nirvana"], "Some Live Set")
    score = score_candidate(source, other_recording, TOLERANCE)
    check("45s difference is refused outright", score == 0.0, f"got {score:.3f}")

    edge = FakeTrack(4, "Drain You", 223 + 13, ["Nirvana"], "Nevermind")
    check(
        "just past 3x tolerance is refused",
        score_candidate(source, edge, TOLERANCE) == 0.0,
        f"got {score_candidate(source, edge, TOLERANCE):.3f}",
    )

    mid = FakeTrack(5, "Drain You", 223 + 8, ["Nirvana"], "Nevermind")
    mid_score = score_candidate(source, mid, TOLERANCE)
    check("8s drift survives but is penalised", 0.0 < mid_score < 1.0, f"got {mid_score:.3f}")

    near = FakeTrack(6, "Drain You", 223 + 2, ["Nirvana"], "Nevermind")
    check(
        "2s drift outscores 8s drift",
        score_candidate(source, near, TOLERANCE) > mid_score,
        "taper is not monotonic",
    )


def test_missing_duration_is_not_a_veto():
    print("\nscore_candidate — a missing length redistributes, it doesn't refuse")

    source = src("Drain You", ["Nirvana"], "Nevermind (Remastered)", 223_000)

    # Full text agreement with no length to check: renormalising over the
    # evaluable weights (0.40+0.30+0.10) must still reach 1.0, not be capped at
    # 0.80 and certainly not refused.
    unknown = FakeTrack(2, "Drain You", None, ["Nirvana"], "Nevermind")
    score = score_candidate(source, unknown, TOLERANCE)
    check(
        "full text agreement reaches 1.0 without a duration",
        math.isclose(score, 1.0),
        f"got {score!r}",
    )

    no_source_duration = src("Drain You", ["Nirvana"], "Nevermind", 0)
    exact = FakeTrack(1, "Drain You", 223, ["Nirvana"], "Nevermind")
    score = score_candidate(no_source_duration, exact, TOLERANCE)
    check(
        "unknown source duration behaves the same way",
        math.isclose(score, 1.0),
        f"got {score!r}",
    )

    # Redistribution must not become a free pass: weak text still fails.
    wrong_artist = FakeTrack(7, "Drain You", None, ["Someone Else"], "Other Album")
    score = score_candidate(source, wrong_artist, TOLERANCE)
    check(
        "title alone is still below the 0.72 default",
        score < 0.72,
        f"got {score:.3f}, expected 0.5",
    )

    check(
        "unknown duration reports inf delta",
        not math.isfinite(duration_delta_s(source, unknown)),
        "expected inf",
    )


# ---------------------------------------------------------------- ISRC lookup


def test_isrc_lookup():
    print("\nisrc_lookup — via Session.get_tracks_by_isrc")

    source = src("Drain You", ["Nirvana"], "Nevermind", 223_000, "USGF19942508")

    # tidalapi's docstring promises an empty list here. It raises.
    session = FakeSession(isrc_raises=ObjectNotFound())
    tid, warning = isrc_lookup(session, source, TOLERANCE, debug=False)
    check("ObjectNotFound is caught, not propagated", tid is None and warning == "", f"{tid} {warning!r}")

    session = FakeSession(isrc_result=[])
    tid, _ = isrc_lookup(session, source, TOLERANCE, debug=False)
    check("empty result is unmatched", tid is None, f"got {tid}")

    # Same ISRC on several releases — pick the closest duration, not [0].
    session = FakeSession(
        isrc_result=[
            FakeTrack(100, "Drain You", 260, ["Nirvana"], "Greatest Hits"),
            FakeTrack(101, "Drain You", 223, ["Nirvana"], "Nevermind"),
        ]
    )
    tid, warning = isrc_lookup(session, source, TOLERANCE, debug=False)
    check("closest duration wins over list order", tid == 101, f"got {tid}")
    check("no warning on a clean ISRC match", warning == "", f"got {warning!r}")

    # ISRC is authoritative (D2), so a duration gap is surfaced, not rejected.
    session = FakeSession(
        isrc_result=[FakeTrack(102, "Drain You", 223 + 47, ["Nirvana"], "Live")]
    )
    tid, warning = isrc_lookup(session, source, TOLERANCE, debug=False)
    check("suspicious ISRC match is still accepted", tid == 102, f"got {tid}")
    check("...but carries a warning", "47s" in warning, f"got {warning!r}")


def nirvana_case():
    """The real 6-way tie from 95_dodge_caravan.csv, in TIDAL's response order.

    All six share the ISRC and report 224s, so duration cannot separate them and
    noise-stripping flattens four of the albums to an identical "Nevermind".
    """
    source = src(
        "Drain You", ["Nirvana"], "Nevermind (Remastered)", 223_880, "USGF19942508"
    )
    candidates = [
        FakeTrack(77610765, "Drain You", 224, ["Nirvana"], "Nevermind"),
        FakeTrack(7772090, "Drain You", 224, ["Nirvana"], "Nevermind (Deluxe Edition)"),
        FakeTrack(
            203937595,
            "Drain You",
            224,
            ["Nirvana"],
            "Nevermind (30th Anniversary Super Deluxe)",
        ),
        FakeTrack(7772065, "Drain You", 224, ["Nirvana"], "Nevermind (Remastered)"),
        FakeTrack(
            7772133, "Drain You", 224, ["Nirvana"], "Nevermind (Super Deluxe Edition)"
        ),
        FakeTrack(
            203938235, "Drain You", 224, ["Nirvana"], "Nevermind (Remastered 2021)"
        ),
    ]
    return source, candidates


def test_isrc_release_choice():
    print("\nisrc_candidate_rank — which *release* wins a duration tie")

    source, candidates = nirvana_case()

    tid, _ = isrc_lookup(FakeSession(isrc_result=candidates), source, TOLERANCE, False)
    check(
        "exact album edition beats a bare reissue",
        tid == 7772065,
        f"got {tid}, expected 7772065 (Nevermind (Remastered))",
    )

    # Determinism matters: without the trailing id in the sort key, TIDAL
    # reordering its response would silently change what lands in the playlist.
    winners = set()
    for rotation in range(len(candidates)):
        rotated = candidates[rotation:] + candidates[:rotation]
        got, _ = isrc_lookup(FakeSession(isrc_result=rotated), source, TOLERANCE, False)
        winners.add(got)
    check(
        "choice is stable across every response ordering",
        winners == {7772065},
        f"got {winners}",
    )

    # Length comes first in the key: a perfect album match with a worse duration
    # must still lose to a closer length on a weaker album.
    source2 = src("Track", ["Artist"], "Exact Album", 200_000, "X0000000000")
    candidates2 = [
        FakeTrack(1, "Track", 200, ["Artist"], "Some Other Album"),  # Δ0s, weak album
        FakeTrack(2, "Track", 206, ["Artist"], "Exact Album"),  # Δ6s, perfect album
    ]
    tid, _ = isrc_lookup(FakeSession(isrc_result=candidates2), source2, TOLERANCE, False)
    check("duration outranks album agreement", tid == 1, f"got {tid}")


# ---------------------------------------------------------------- search fallback


def test_search_isrc_confirmation():
    print("\nsearch_match — ISRC confirmation promotes a text hit")

    source = src("Drain You", ["Nirvana"], "Nevermind", 223_000, "USGF19942508")

    # A confirmed ISRC outranks the duration gate: the identifier is stronger
    # evidence than length, so this must match despite an absurd duration.
    session = FakeSession(
        search_results=[
            FakeTrack(200, "Drain You", 999, ["Nirvana"], "Nevermind", isrc="USGF19942508")
        ]
    )
    tid, score, confirmed = search_match(session, source, 0.72, TOLERANCE, debug=False)
    check("ISRC-confirmed hit bypasses the gate", tid == 200, f"got {tid}")
    check("...and scores 1.0", score == 1.0, f"got {score}")
    check("...and is flagged confirmed", confirmed is True, f"got {confirmed}")

    # A *differing* ISRC is weak evidence, not a veto — same recording can carry
    # different ISRCs per distributor. So this falls through to normal scoring.
    session = FakeSession(
        search_results=[
            FakeTrack(201, "Drain You", 223, ["Nirvana"], "Nevermind", isrc="GBXXX0000001")
        ]
    )
    tid, score, confirmed = search_match(session, source, 0.72, TOLERANCE, debug=False)
    check("differing ISRC still scored on merit", tid == 201 and not confirmed, f"{tid} {confirmed}")

    # An unconfirmed hit with no duration is now scored on text, not refused.
    session = FakeSession(
        search_results=[FakeTrack(202, "Drain You", None, ["Nirvana"], "Nevermind")]
    )
    tid, score, confirmed = search_match(session, source, 0.72, TOLERANCE, debug=False)
    check(
        "unconfirmed hit with no duration is scored, not refused",
        tid == 202 and not confirmed,
        f"got {tid}, confirmed={confirmed}",
    )

    # The far-outlier veto still bites when a length *is* available.
    session = FakeSession(
        search_results=[FakeTrack(203, "Drain You", 223 + 60, ["Nirvana"], "Live Set")]
    )
    tid, score, _ = search_match(session, source, 0.72, TOLERANCE, debug=False)
    check("60s-longer recording still refused", tid is None, f"got {tid}")

    session = FakeSession(search_results=[])
    tid, score, _ = search_match(session, source, 0.72, TOLERANCE, debug=False)
    check("no candidates is unmatched", tid is None and score == 0.0, f"{tid} {score}")


# ---------------------------------------------------------------- cache


def test_cache_reuse_rules():
    print("\ncache_lookup — search results must not survive a scorer change")

    entries = {
        "ISRC_HIT": {"tidal_id": 1, "method": "isrc", "score": 1.0, "params": None},
        "SEARCH_HIT": {
            "tidal_id": 2,
            "method": "search",
            "score": 0.81,
            "params": "min=0.72:tol=4",
        },
        "MISS": {"tidal_id": None, "method": "unmatched", "score": 0.4, "params": None},
    }

    check(
        "ISRC entry reused under any settings",
        cache_lookup(entries, "ISRC_HIT", "min=0.95:tol=2") is not None,
        "ISRC match should be settings-independent",
    )
    check(
        "search entry reused when settings match",
        cache_lookup(entries, "SEARCH_HIT", "min=0.72:tol=4") is not None,
        "should reuse",
    )
    # This is the bug: tuning --min-score used to be silently defeated.
    check(
        "search entry ignored when settings differ",
        cache_lookup(entries, "SEARCH_HIT", "min=0.95:tol=4") is None,
        "stale search match leaked across a --min-score change",
    )
    check(
        "negative entry never reused",
        cache_lookup(entries, "MISS", "min=0.72:tol=4") is None,
        "should not reuse a miss",
    )
    check(
        "unknown key is a miss",
        cache_lookup(entries, "NOPE", "min=0.72:tol=4") is None,
        "should be None",
    )


def test_playlist_name_from_filename():
    print("\nplaylist_name_from_filename — Exportify slugifies, so recover a name")

    cases = {
        "95_dodge_caravan.csv": "95 Dodge Caravan",  # the real export
        "best_of_the_80s.csv": "Best of the 80s",
        "road-trip-mix.csv": "Road Trip Mix",
        "the_wall.csv": "The Wall",  # small word, but first
        "songs_to_the_siren.csv": "Songs to the Siren",
        "single.csv": "Single",
        # Existing capitals mean the case is probably meaningful — don't mangle
        # acronyms into "Rem" or "Oar".
        "REM_Automatic.csv": "REM Automatic",
        "O.A.R. live.csv": "O.A.R. live",
    }
    for filename, expected in cases.items():
        got = playlist_name_from_filename(Path(filename))
        check(f"{filename} → {expected!r}", got == expected, f"got {got!r}")


def test_discover():
    print("\ndiscover — queue unprocessed exports, oldest first")

    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        (d / "alpha.csv").write_text("already ported", encoding="utf-8")
        (d / "bravo.csv").write_text("brand new", encoding="utf-8")
        (d / "charlie.csv").write_text("edited since", encoding="utf-8")
        # Our own sidecar output must never be picked up as an input.
        (d / "bravo_unmatched.csv").write_text("sidecar", encoding="utf-8")
        (d / "notes.txt").write_text("not a csv", encoding="utf-8")

        # Force distinct mtimes so ordering is meaningful and deterministic.
        for i, name in enumerate(["alpha.csv", "bravo.csv", "charlie.csv"]):
            os.utime(d / name, (1_700_000_000 + i * 60,) * 2)

        ledger = {
            "alpha.csv": {
                "sha256": file_digest(d / "alpha.csv"),
                "playlist_name": "Alpha",
            },
            "charlie.csv": {"sha256": "stale-hash-from-an-earlier-export"},
        }

        queue, skipped = discover(d, ledger, reprocess=False)
        names = [p.name for p in queue]
        check("only the unported CSV is queued", names == ["bravo.csv"], f"got {names}")

        reasons = {p.name: r for p, r in skipped}
        check(
            "unchanged + ported is skipped as done",
            "already ported as 'Alpha'" in reasons.get("alpha.csv", ""),
            f"got {reasons.get('alpha.csv')!r}",
        )
        check(
            "edited-since is skipped, not silently re-ported",
            "changed since it was ported" in reasons.get("charlie.csv", ""),
            f"got {reasons.get('charlie.csv')!r}",
        )
        check(
            "sidecar output is not an input",
            "bravo_unmatched.csv" not in names and "bravo_unmatched.csv" not in reasons,
            "sidecar leaked into discovery",
        )
        check("non-csv ignored", "notes.txt" not in names, f"got {names}")

        queue, skipped = discover(d, ledger, reprocess=True)
        names = [p.name for p in queue]
        check(
            "--reprocess queues everything, oldest first",
            names == ["alpha.csv", "bravo.csv", "charlie.csv"],
            f"got {names}",
        )
        check("--reprocess skips nothing", skipped == [], f"got {skipped}")

        queue, skipped = discover(d / "nope", {}, reprocess=False)
        check(
            "missing directory is empty, not an error",
            (queue, skipped) == ([], []),
            f"got {queue} {skipped}",
        )


def main() -> int:
    test_parse_artists()
    test_strip_noise_on_albums()
    test_duration_scoring()
    test_missing_duration_is_not_a_veto()
    test_isrc_lookup()
    test_isrc_release_choice()
    test_search_isrc_confirmation()
    test_cache_reuse_rules()
    test_playlist_name_from_filename()
    test_discover()

    print(f"\n{len(PASSES)} passed, {len(FAILURES)} failed")
    if FAILURES:
        print("\nFailures:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
