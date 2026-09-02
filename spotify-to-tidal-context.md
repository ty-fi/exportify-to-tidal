# Spotify → Tidal playlist porting: context handoff

**Status:** Working end to end as of 2026-09-01. 9/9 tracks matched by ISRC, and a
full (non-dry) run successfully created the playlist in Tidal.
**Companion artifacts:** `exportify_to_tidal.py`, `test_matching.py`
**Environment:** uv project, Python 3.11, `tidalapi==0.8.11` (pinned exactly).

---

## 1. The problem

Recurring, not one-time. People send me Spotify playlists; I use Tidal. I need a
low-friction way to port an arbitrary shared playlist into my Tidal account,
repeatedly, without babysitting it.

Constraints that fall out of this:

- Source playlists are **owned by other people**, not me.
- I should not have to maintain a fragile toolchain between uses.
- Match quality matters more than throughput. A silently wrong recording — a
  cover, a remix, a radio edit — is worse than a visible gap I can fill by hand.

> **Revised 2026-09-01.** An earlier draft of this line claimed the library
> "skews toward live and concert recordings." It does not. That premise drove an
> aggressive duration veto (D3/D7) that was later relaxed — see D13. The general
> preference above still holds; the live-music emphasis specifically does not.

---

## 2. Why the obvious paths are closed

### Spotify killed the self-hosted route in February 2026

This is the single most important fact in this document, and it invalidates most
tutorials and READMEs written before mid-2026.

Spotify's blog post *Update on Developer Access and Platform Security*
(2026-02-06) reduced Development Mode in scope. For newly created Development
Mode Client IDs, from **2026-02-11**:

- Requires a **Spotify Premium account**
- **One** Development Mode Client ID per developer
- Max **five** authorized users per Client ID
- Access restricted to a **smaller set of supported endpoints**

From 2026-03-09 the same rules applied to existing integrations — except that
after community pushback, Spotify **postponed only the endpoint restrictions**
for existing integrations. The Premium requirement, user cap, and one-Client-ID
limit took effect as planned. **No end date for that postponement has been
announced** — the blog says only "We will share further details on updated
timelines as soon as we're able to share more."

**Consequence:** anyone registering a *new* Spotify app today lands in the
restricted endpoint set immediately, with no grandfathering.

### Extended quota mode is exempt — this is not grandfathering (verified 2026-09-01)

An earlier draft of this document conflated two different things. They matter
differently:

- **Grandfathering** (the postponement above) applies to *existing Development
  Mode* integrations, is time-limited, and has no announced expiry. This is what
  `spotify_to_tidal`'s maintainer is riding.
- **Exemption** applies to extended quota mode, and is not time-limited. The
  February 2026 migration guide is explicit: *"Extended Quota Mode apps: No
  migration required. Apps in extended quota mode are not affected by any of the
  changes described in this guide — all existing endpoints, fields, and
  behaviors remain unchanged."*

exportify.app is in the second category, so **our path is not on a clock.**

Evidence it's extended quota rather than a grandfathered Dev Mode app: the
five-authorized-user cap took effect *on schedule* for existing integrations —
only the endpoint changes were postponed. A public tool serving far more than
five users could not have survived 2026-03-09 in Development Mode. Corroborating:
the repo has no February-2026 migration commits at all; the only 2026 commits are
April 13 (a Greek translation and a whitespace revert), and the last substantive
API work was the Nov 2025 PKCE migration. Seven months on, an affected app would
have migrated or broken.

**Residual risks, neither date-bound:** (a) the `external_ids`/ISRC restoration is
marked `[REVERTED]` without being characterised as permanent, so an all-tier
change could still remove it; (b) exportify.app is a third-party single point of
failure — if it loses extended quota, is abandoned, or gets revoked, the path dies
with no notice. Both are mitigated by keeping the CSV as the interface boundary:
swap the producer, leave the matcher untouched.

### What the February 2026 endpoint changes broke

From the Spotify Web API February 2026 changelog:

| Change | Detail |
|---|---|
| `GET /playlists/{id}/tracks` | **Removed** → use `/playlists/{id}/items` |
| `POST`/`PUT`/`DELETE` on `/tracks` | Same rename to `/items` |
| Playlist object fields | `tracks` → `items`, `tracks.tracks.track` → `items.items.item` |
| `GET /tracks` (several tracks) | **Removed**; only single-track `GET /tracks/{id}` remains |
| `GET /search` | `limit` max cut 50 → 10; default 20 → 5 |
| Playlist contents | Dev Mode returns `items` only for the **user's own** playlists; others return metadata only |
| `external_ids` (ISRC) | Removed in Feb, **reverted in the March 2026 changelog** — ISRC is still available |

That second-to-last row would be fatal for this use case even if everything else
worked, since the whole point is reading other people's playlists. None of the
above applies to extended quota mode.

### `spotify2tidal/spotify_to_tidal` is alive but not usable off the shelf

Not abandoned — the maintainer (timrae) opened a WIP album/artist sync PR and
filed a Tidal ETag bug on 2026-07-01. Issue creation is restricted on the repo.

But **four separate open PRs** fix the February API breakage and none are merged:
#168 (2026-02-16, 14 comments), #170, #172, #176, plus #179 for the resulting
`KeyError`. Open issues #184 and #185 are both titled "Active premium
subscription required." `main` presumably still works for the maintainer because
his app is a grandfathered existing integration — i.e. on the clock described above.

Verdict: usable only if you cherry-pick an unmerged PR and debug it yourself.

### Odesli / song.link is dead — do not build on it

The obvious workaround (feed it a Spotify URL, get a Tidal link) is gone. The
`v1-alpha.1` namespace had an announced retirement of 2026-07-31, and a
downstream project's release notes from 2026-08-29 report Odesli retired the
public song.link API on **2026-08-28**, silently breaking all callers. There was
also a hard 10 req/min cap without a key.

---

## 3. The path that works

**Exportify (hosted) → CSV with ISRC → Tidal API.**

The trick: use the *hosted* Exportify, which authenticates against the
maintainer's registered Spotify app. That app is in extended quota mode, so none
of the February restrictions apply. You never register a Spotify app, never need
Premium, never touch the restricted endpoint set.

Self-hosting Exportify would defeat this entirely — you'd register your own
Client ID and inherit every restriction. **Do not self-host it.**

### Critical: there are two unrelated projects called Exportify

| | `exportify.net` | `exportify.app` |
|---|---|---|
| Author | pavelkomarov | watsonbox |
| ISRC in CSV | **No** | **Yes, by default** |
| Tell-tale columns | Genres, Record Label, Danceability/Energy/Key/… | Artist URI(s), Album URI, Disc Number, Track Number, Track Preview URL |

Use **exportify.app**. Confirmed header row (this is what the script parses):

```
Track URI, Track Name, Artist URI(s), Artist Name(s), Album URI, Album Name,
Album Artist URI(s), Album Artist Name(s), Album Release Date, Album Image URL,
Disc Number, Track Number, Track Duration (ms), Track Preview URL, Explicit,
Popularity, ISRC, Added By, Added At
```

The cog only adds artist genres, audio features, and album label/copyright — ISRC
needs no configuration.

### The manual step, and why it stays manual (checked 2026-09-01)

**Workflow for a playlist someone sends me:** follow it in Spotify so it lands in
my library, export from exportify.app, unfollow.

Three structural facts rule out automating the export itself:

- **Library-only.** No URL or ID input — Exportify only lists playlists in the
  authenticated user's library. The follow step isn't a quirk, it's required.
- **No CLI, no API, no headless mode.** Browser-only SPA with in-browser PKCE.
  There is nothing to call.
- **"Export All" is all-or-nothing:** *"Click 'Export All' to save a zip file
  containing a CSV file for each playlist in your account."* No subset selection.

So the cheap wins are ergonomic, not architectural:

1. **Stop unfollowing.** Park incoming playlists in a dedicated Spotify folder.
   Pure library hygiene, removes a step at zero cost. *(Not yet adopted — a
   habit change, nothing to build.)*
2. **Batch via Export All.** Follow several over a week, one export, N CSVs.
   Amortises the browser step instead of paying it per playlist. *(Supported:
   unzip into `exportify-exports/` and run with no arguments.)*
3. **Automate downstream, not the browser.** ✅ **Done** — see D14. Running with
   no arguments scans `exportify-exports/`, ports everything not already done
   oldest-first, and derives each playlist name from the filename.

The whole per-playlist loop is now: follow in Spotify → one click in Exportify →
`uv run exportify_to_tidal.py`.

Browser automation (Playwright against a persistent, manually-logged-in profile)
is possible but was rejected: a permanent maintenance liability on two UIs we
don't control, to save ~20 seconds amortised across a batch.

---

## 4. The Tidal side

There are two Tidal APIs and they do not share auth.

### Official — `developer.tidal.com` / `openapi.tidal.com/v2`

- JSON:API compliant. Single canonical OpenAPI spec, 240+ paths, 320+ operations.
- Reference: `https://tidal-music.github.io/tidal-api-reference/` and
  `https://developer.tidal.com/documentation`.
- Free self-serve app registration.
- ISRC lookup: `filter[isrc]` on the catalogue-v2 tracks endpoint.
- Official SDKs: Web (TypeScript), iOS (Swift), Android (Kotlin). **No Python SDK.**

### Internal — `api.tidal.com`

What the Tidal apps themselves use. Undocumented, reverse-engineered (including
the device authorization grant, because the web OAuth flow is reCaptcha-protected).
This is what `tidalapi` wraps.

Pointing an official *developer* token at `api.tidal.com` returns HTTP 403
`subStatus 11004`: token missing required scope, requires `r_usr`. Requesting
`r_usr` during authorization just breaks the flow.

### The direction that *does* work (verified 2026-09-01)

The reverse of the above is fine, and it's the load-bearing discovery of this
project: **a `tidalapi` device-code user token authenticates successfully against
the official `openapi.tidal.com/v2` catalogue.**

`tidalapi` exploits this deliberately. `Session.get_tracks_by_isrc()` requests
`https://openapi.tidal.com/v2/tracks?filter[isrc]=…` using the session's own
`Authorization: {token_type} {access_token}`. Confirmed working on a real run:
9/9 ISRCs resolved, nothing fell through to text search.

**Consequence: no Tidal developer app is needed at all.** One device-code login
covers ISRC lookup, text search, and playlist writes.

### `tidalapi` (python-tidal) current state

- Moved from `tamland/python-tidal` to **`EbbLabs/python-tidal`**, maintained by
  tehkillerbee. Actively developed. 0.8.x line.
- Pinned here at **0.8.11**.
- Verified API surface (0.8.11):
  - `Session.get_tracks_by_isrc(isrc) -> list[Track]` — openapi v2, as above.
  - `Session.login_session_file(path)` — load-or-login in one call; preferred over
    hand-rolling `load_oauth_session`, which drops the token expiry.
  - `LoggedInUser.create_playlist(title, description, parent_id="root")`
  - `UserPlaylist.add(media_ids: List[str], …, limit=100) -> List[int]` — note
    **string** ids, and it returns the ids actually added.
  - `Track.duration` is in **seconds**; `Track.isrc` exists and is populated
    opportunistically on search hits.
- **Two traps found by reading the source:**
  1. `get_tracks_by_isrc`'s docstring claims *"An empty list will be returned if
     no tracks matches the ISRC."* It **raises `ObjectNotFound`** instead (and
     `InvalidISRC` on an HTTP error). Code written against the docstring crashes
     on the first unmatched track.
  2. Each ISRC hit costs **two** requests — the openapi lookup, then a
     `self.track(id)` hydration per result against the internal API.
- It logs a `WARNING` for every unavailable variant a lookup turns up (routinely
  a dozen per track, all expected and dropped before scoring). Python's
  last-resort handler dumps these to stderr; the script quiets the `tidalapi`
  logger unless `--debug`.
- Characteristic failure mode: in late 2025 a Tidal-side change returned null
  `mediaMetadata`, and tidalapi's track parser crashed on it, breaking most
  playlist operations until a fix merged. No deprecation notice — just a
  `TypeError`. **This is why the version is pinned exactly.**

---

## 5. Decisions

**D1 — Use hosted exportify.app, not a self-hosted instance or the API directly.**
Borrows an extended-quota Client ID. Sidesteps the entire February 2026 problem.

**D2 — ISRC is the primary join key; text search is a scored fallback.**
ISRC gives high precision, imperfect recall. Live/reissue material often lacks
matching ISRCs across catalogues.

**D3 — Duration corroborates a fallback match and vetoes far outliers.**
Originally justified as a studio-for-live guard; that premise was wrong (see §1).
It survives on a general basis: length is the cheapest available check on "is
this the same recording," catching a cover, a remix, an extended mix, or another
song that merely shares a title. Weight 0.20, plus a hard veto beyond 3x
tolerance. See D7 and D13 for the two corrections it took to get here.

**D4 — Unmatched tracks are written to a sidecar CSV, never guessed at.**
A visible gap beats a wrong recording.

**D5 — Cache matches locally**, keyed by ISRC with Spotify URI fallback.
Repeated use across playlists from different people means heavy overlap. See D9.

**D6 — RESOLVED: stay on `tidalapi`, single device-code session.**
Because `get_tracks_by_isrc` works (§4), tidalapi now covers ISRC lookup, search,
*and* playlist writes through one auth flow. Going official would mean
reimplementing all three against a JS-rendered spec, not just porting auth. The
durability argument got materially more expensive; revisit only if tidalapi breaks.

**D7 — First correction: the weighting had a hole, now closed.**
D3 was stated but not implemented. Duration was a 0.2-weighted term, so title +
artist + album alone reached 0.80 — over the 0.72 threshold — meaning a candidate
with an unknown or 4–12s-divergent length could pass on text agreement alone.
Fixed the taper so the score falls continuously across the tolerance..3x band
instead of dropping to zero at exactly `tolerance`, and kept the beyond-3x veto.

**D13 — Second correction: a missing length redistributes, it does not refuse.**
D7 over-corrected by refusing any candidate with no duration on either side.
That was defensible only under the (false) live-music premise; in general it
penalises a candidate for metadata we never had, and makes the outcome hinge on
where the 0.80 ceiling happened to sit relative to the threshold. Now, when
either side lacks a duration, its 0.20 weight is redistributed across the text
signals — renormalising over 0.40 + 0.30 + 0.10 — so full text agreement still
reaches 1.0 and partial agreement still has to clear the threshold on merit
(title alone lands at 0.50 and fails). The beyond-3x veto is untouched and still
fires whenever a length *is* available.

Worth noting how little this changes in practice: the scorer only runs when ISRC
misses, and ISRC has so far resolved 100% of real tracks. The fallback is a
rarely-exercised path, which is also why Q6 was reframed.

**D14 — Batch mode is keyed on filename, with a content hash as a change tripwire.**
Running with no arguments ports every CSV in `exportify-exports/` that isn't in
the ledger at `~/.config/exportify_to_tidal/processed.json`. Design points, each
one a trap avoided:

- **Filename is the identity**, because Exportify names the file after the
  playlist. The stored `sha256` is not part of the key — it only detects that a
  known export *changed*.
- **A changed export is skipped, not requeued.** Re-porting would create a second
  Tidal playlist rather than update the first. That's a sync problem this tool
  doesn't solve, so it reports and defers to `--reprocess` instead of guessing.
- **`*_unmatched.csv` is excluded from discovery.** It's our own output, written
  into the same directory; without the exclusion the sidecar becomes an input.
- **Ordering is `(mtime, name)`** — chronological when timestamps mean something,
  alphabetical when a zip extract flattened them to one instant. Never arbitrary.
- **The ledger is written after each playlist**, not at the end, so an interrupt
  keeps what already succeeded.
- **A dry run writes nothing to the ledger.** Nothing was created, so recording
  it would strand the export as permanently "done".
- **One failure doesn't abort the batch.** It's reported, left out of the ledger,
  and retried next run.
- **`--mark-done`** records exports as ported without contacting Tidal — needed
  to bootstrap, since `95_dodge_caravan.csv` was ported before the ledger existed
  and would otherwise have been duplicated.
- **Name derivation:** Exportify slugifies, so casing is unrecoverable. An
  all-lowercase stem is title-cased with small words kept down
  (`best_of_the_80s` → `Best of the 80s`); a stem containing capitals is left
  alone so acronyms don't become `Rem` or `Oar`. `--name` overrides, and is
  rejected when more than one playlist is queued.

**D8 — Search hits self-verify by ISRC, asymmetrically.**
`Track.isrc` is populated on search results, so a hit whose ISRC equals the
source's is promoted to an exact match (score 1.0, method `search+isrc`) and
bypasses the duration gate — the identifier is stronger evidence than length. A
*differing* ISRC is deliberately **not** a veto: the same recording legitimately
carries different ISRCs across distributors, so a mismatch is weak evidence.

**D9 — Cache entries record the scorer settings that produced them.**
ISRC matches are settings-independent and always reusable. Search matches store
`min=…:tol=…` and are only reused when it matches — otherwise tuning
`--min-score` was silently defeated by the cache, which is exactly the run you'd
be tuning. Negatives are not cached at all: the catalogue changes, and a visible
gap is the thing most worth retrying. `--no-cache` bypasses both directions.

**D10 — An ISRC match with a large duration gap is surfaced, not rejected.**
ISRC is authoritative per D2, so the gate in D7 does not apply to pass 1. But a
gap beyond 3× tolerance prints a warning and appears in a summary block, because
for a live-heavy library that's the signal worth eyeballing.

**D11 — Pin `tidalapi` exactly.** See §4's failure mode. `uv.lock` pins for
`uv sync`; the `==` in `pyproject.toml` stops an unrelated `uv lock` from moving it.

**D12 — Release selection among ISRC ties is fully ordered and deterministic.**
One ISRC routinely resolves to the same recording on half a dozen releases,
identical in length. Observed on `Drain You`: six candidates, all 224s, four of
them flattening to album overlap 1.00 once edition markers are stripped. Since
they're the same recording, this is really choosing which *release* the playlist
points at — i.e. the album art and attribution. Ranked by: duration delta, then
noise-stripped album overlap, then **raw** album overlap so the exact edition the
source named wins over a bare reissue, then shortest album name, then track id.

The trailing id is not cosmetic. Without it the winner depended on TIDAL's
response ordering, so the same CSV could resolve to a different release from one
run to the next — which would also poison the match cache. Adding the raw-overlap
term moved two of the nine test tracks to the edition the source actually named
(`Nevermind` → `Nevermind (Remastered)`, and the Beck track likewise).

---

## 6. Open questions

| # | Question | State |
|---|---|---|
| Q1 | Official API (PKCE) vs `tidalapi` (device code) for playlist writes? | **Resolved** → tidalapi. See D6. |
| Q2 | Exact response shape of catalogue-v2 `filter[isrc]` | **Dead.** `tidalapi` owns this parsing now; `CatalogueClient` deleted. |
| Q3 | Does `tidalapi`'s built-in ISRC lookup make the developer app unnecessary? | **Resolved: yes.** Verified 9/9 on a real run. No developer app, one auth flow. |
| Q4 | Exact request bodies for official playlist create + add-items | **Deferred.** Only relevant if Q1 reverses. |
| Q5 | Is exportify.app's ISRC column reliably populated? | **Resolved: yes.** 9/9 (100%) on `95_dodge_caravan.csv`. |
| Q6 | ~~Match rate on live/concert-heavy playlists~~ → **How often does ISRC miss at all?** | **Open, and reframed.** The original framing assumed a live-heavy library, which was wrong (§1). The question that actually matters: across real incoming playlists, what fraction of tracks fall through to the text scorer? At 9/9 so far the answer may be "almost none," in which case the scorer barely matters and hand-fixing a couple of tracks is the whole reconciliation story. Resolve by accumulating match stats over the next few real playlists, not by a single stress test. |
| Q7 | Do playlist create + add actually work? | **Resolved: yes.** A full non-dry run created the playlist in Tidal successfully on 2026-09-01. |

---

## 7. Next steps

The tool works and the ergonomics are done. What's left is evidence, not code.

1. **Just use it.** Unzip an Export All into `exportify-exports/` and run
   `uv run exportify_to_tidal.py`. Note the match-stats line each time; that
   accumulates the answer to Q6 as a side effect of normal use, with no dedicated
   stress test needed.
2. **Adopt the free habit change:** stop unfollowing, park incoming playlists in
   a Spotify folder instead.
3. **Revisit the scorer only if Q6 says so.** If the text fallback turns out to
   fire on a meaningful fraction of tracks, *then* tune `--min-score` /
   `--duration-tolerance` (always with `--no-cache`, per D9) and decide whether
   sidecar reconciliation belongs in the tool. If it stays near zero, neither
   question needs answering.
4. **Don't touch the duration logic again without new evidence.** It has been
   revised twice (D7, D13) for a path that rarely executes.

---

## 8. Script summary

`exportify_to_tidal.py` — CLI, ~990 lines, Python 3.11+, single dependency `tidalapi`.

Run with **no arguments** to port every unprocessed CSV in `exportify-exports/`
(D14). Name a CSV to port just that one.

Two-pass matching through **one** tidalapi session, shared across every playlist
in a batch: ISRC via `get_tracks_by_isrc` (openapi v2), then scored text search
with a far-outlier duration veto and opportunistic ISRC self-verification.
Sidecar CSV for unmatched, settings-aware match cache, `login_session_file` for
session reuse, batched playlist adds (100 at a time, string ids).

The shared match cache pays off exactly as D5 predicted: a second playlist
overlapping the first resolves entirely from cache with zero API calls.

Three state files, all under `~/.config/exportify_to_tidal/`:
`session.json` (OAuth), `matches.json` (ISRC/URI → Tidal id, versioned),
`processed.json` (the ledger).

Key flags: `--list`, `--reprocess`, `--mark-done`, `--exports-dir`, `--dry-run`,
`--debug`, `--isrc-only`, `--no-cache`, `--min-score`, `--duration-tolerance`,
`--delay`, `--name`, `--description`.

`--list` shows the queue and derived names without a Tidal login. `--debug`
prints every ISRC candidate with its duration delta and both album overlaps
(noise-stripped / raw), which is what made D12 visible — without it the tie-break
is invisible.

`test_matching.py` — ~545 lines, plain asserts, no pytest, no network. Fakes the
tidalapi `Track`/`Session` surface and uses a temp directory for discovery.
Covers artist-comma parsing, album noise stripping, duration scoring and the
far-outlier veto, missing-duration weight redistribution (D13), the
`ObjectNotFound` trap, multi-candidate ISRC ranking (including the real six-way
`Drain You` tie and a rotation test for ordering stability), ISRC-confirmation
bypass, the cache reuse rules, filename→name derivation, and discovery
(ledger skips, changed-export detection, sidecar exclusion, ordering,
`--reprocess`, missing directory). Run with `uv run test_matching.py`.
54 assertions, all passing.

### Bugs found on first execution (the script had never been run)

- `NOISE` regex had unbalanced parentheses — crashed at **import**, line 78.
- `stdout` is cp1252 on Windows; the `→`/`✗` glyphs and any accented track title
  raised `UnicodeEncodeError`. Fixed by reconfiguring stdout/stderr to UTF-8.
- `playlist.add()` takes `List[str]`; the original passed ints.
- `strip_noise` was applied only to titles, halving the album score on real data
  (`Nevermind (Remastered)` vs `Nevermind`).
- `Artist Name(s)` was split on `,`, so "Earth, Wind & Fire" searched as
  "Earth". Now cross-checked against the `Artist URI(s)` count.

---

## 9. References

**Spotify**
- Developer access changes (2026-02-06): `https://developer.spotify.com/blog/2026-02-06-update-on-developer-access-and-platform-security`
- February 2026 changelog: `https://developer.spotify.com/documentation/web-api/references/changes/february-2026`
- March 2026 changelog (ISRC reversion): `https://developer.spotify.com/documentation/web-api/references/changes/march-2026`
- Migration guide (extended-quota exemption): `https://developer.spotify.com/documentation/web-api/tutorials/february-2026-migration-guide`

**Exportify**
- watsonbox (use this): `https://github.com/watsonbox/exportify` → `https://exportify.app/`
- pavelkomarov (no ISRC): `https://github.com/pavelkomarov/exportify` → `https://exportify.net/`

**Tidal**
- Portal: `https://developer.tidal.com/documentation`
- API reference: `https://tidal-music.github.io/tidal-api-reference/`
- Discussions: `https://github.com/tidal-music/discussions` (see #6 playlist manipulation, #26 ISRC lookup, #78 official vs internal token scopes)
- `tidalapi`: `https://github.com/EbbLabs/python-tidal`, docs `https://tidalapi.netlify.app/`

**Prior art**
- `https://github.com/spotify2tidal/spotify_to_tidal` (blocked on unmerged PRs)
- `https://github.com/Zibbp/spotify-playlist-sync` (ISRC-first, Docker, but reads Spotify's API so it hits the same Dev Mode wall)

**Commercial fallback** — if the self-hosted path stalls, Soundiiz runs under
extended quota mode and is unaffected by all of the above. $5/month or $39/year,
cancellable after a one-time transfer. Free tier is one playlist at a time,
200 tracks max, which is not enough. Its matching is metadata-based, not
ISRC-first, so expect worse results on live material than this script.

---

## 10. Note on source reliability

§4's claims about `tidalapi` are now read directly from the installed 0.8.11
source, and the ISRC path is confirmed by a real run — not inferred from
changelogs. The two docstring/behaviour discrepancies noted there are exactly why.

The *official* Tidal Playlists API capability claims (create/read/update/delete,
scope vocabulary) remain **unverified** — they came from a third-party API
catalogue plus TIDAL's npm docs, and the primary sources are JavaScript SPAs that
don't render for a text-only fetcher. That only becomes load-bearing if D6
reverses.
