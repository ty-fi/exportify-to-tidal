# exportify.app to tidal playlist port utility app

Ports Spotify playlists into Tidal from an [Exportify](https://exportify.app/)
CSV, matching on ISRC first with a scored text-search fallback.

Nothing is silently substituted. A track that cannot be matched confidently is
written to a sidecar CSV, so a gap stays visible instead of being filled with the
wrong recording.

## Why this approach

Spotify reduced Development Mode in scope in February 2026: new Client IDs
require Premium, are capped at five authorized users, and — decisively here — can
only read the contents of playlists the authenticated user *owns*. Porting a
playlist someone else made is therefore not possible through a self-registered
Spotify app.

The way around it is to not call Spotify's API at all. exportify.app runs under an
**extended quota mode** application, which the February 2026 migration guide
exempts from all of those restrictions. The CSV is exported in the browser, and
this tool only ever reads that file.

There is no Tidal developer app to register either. A `tidalapi` device-code token
authenticates against the official `openapi.tidal.com/v2` catalogue, so ISRC
lookup, search, and playlist writes all run through a single login.

## Requirements

- Python 3.11+
- [uv](https://docs.astral.sh/uv/)
- A Tidal account (one device-code login, cached afterwards)

## Setup

```bash
uv sync
```

## Getting a CSV

Use **exportify.app** (watsonbox), *not* exportify.net — they are unrelated
projects, and only exportify.app includes the ISRC column this tool matches on.

1. Follow the playlist in Spotify so it appears in the library.
2. Open [exportify.app](https://exportify.app/), grant read-only access, and
   export. "Export All" produces a zip containing one CSV per playlist.
3. Unzip into `exportify-exports/`.

Self-hosting Exportify defeats the point: a self-registered Client ID inherits
every restriction described above.

## Usage

Port everything not already done:

```bash
uv run exportify_to_tidal.py
```

Preview the queue, which requires no Tidal login:

```bash
uv run exportify_to_tidal.py --list
```

```
2 to port, oldest first:
  1. 95_dodge_caravan.csv  →  '95 Dodge Caravan'
  2. best_of_the_80s.csv   →  'Best of the 80s'
```

Playlist names are derived from filenames, because the Exportify CSV contains no
playlist-level metadata at all. An all-lowercase filename is title-cased with
small words kept down; a filename containing capitals is left alone so acronyms
survive. `--list` shows the derived names before anything is created.

Other common invocations:

```bash
# match and report without writing anything to Tidal
uv run exportify_to_tidal.py --dry-run

# a single file, with an explicit name
uv run exportify_to_tidal.py some.csv --name "Road Trip"

# record exports already ported by hand, so they are not ported twice
uv run exportify_to_tidal.py --mark-done
```

### Flags

| Flag | Effect |
|---|---|
| `--list` | Show the queue and derived names, then stop. No login required. |
| `--dry-run` | Match and report, create nothing. Still logs in, since ISRC lookup goes through the session. |
| `--mark-done` | Record queued CSVs as ported without contacting Tidal. |
| `--reprocess` | Ignore the ledger. Creates a **new** playlist; does not update an existing one. |
| `--exports-dir` | Directory to scan (default `exportify-exports`). |
| `--isrc-only` | Exact ISRC matches only; skip the text fallback. |
| `--min-score` | Fallback threshold, 0–1 (default 0.72). |
| `--duration-tolerance` | Seconds of drift treated as the same recording (default 4). Beyond 3× this is refused. |
| `--no-cache` | Bypass the match cache. Required when tuning the two flags above, otherwise cached results mask the change. |
| `--delay` | Seconds between lookups (default 1.0). |
| `--name`, `--description` | Playlist metadata. `--name` accepts only a single CSV. |
| `--debug` | Print every candidate with duration deltas and album overlaps. |

## How matching works

1. **ISRC** via `Session.get_tracks_by_isrc` against Tidal's openapi v2 catalogue.
   Exact, and in practice resolves the large majority of tracks.
2. **Scored text search** for anything ISRC misses — title 0.4 / artist 0.3 /
   duration 0.2 / album 0.1, thresholded at `--min-score`. A candidate more than
   3× the tolerance from the expected length is refused as a different recording
   (cover, remix, extended mix). A search hit whose own ISRC matches the source is
   promoted to an exact match.

When one ISRC resolves to several releases of the same recording, the winner is
chosen by duration delta, then album agreement, then track id — so the same CSV
always resolves to the same release rather than depending on response ordering.

Unmatched tracks are written to `<name>_unmatched.csv` beside the input.

Re-running is safe. A ledger keyed on filename skips anything already ported, and
a content hash detects an export that has *changed*, reporting it rather than
creating a duplicate playlist.

## State

Three files under `~/.config/exportify_to_tidal/`:

| File | Contents |
|---|---|
| `session.json` | Tidal OAuth tokens. Should be treated as a credential. |
| `matches.json` | ISRC/URI → Tidal track id. Shared across playlists, so overlapping ones cost no API calls. |
| `processed.json` | Which exports have been ported. |

Deleting `matches.json` forces a re-resolve. Deleting `processed.json` makes every
export look unported.

## Tests

```bash
uv run test_matching.py
```

54 assertions, no network and no pytest. Covers the scorer, the ISRC edge cases,
release-tie determinism, cache reuse rules, filename-to-name derivation, and
discovery.

## Limitations

- **Playlist cover art cannot be transferred.** Tidal's API exposes
  `GET /playlists/{id}/relationships/coverArt` with no corresponding POST, and the
  Exportify CSV carries no playlist cover in the first place. Covers have to be
  set in the Tidal app.
- **No playlist metadata beyond the name.** The CSV has no playlist URI,
  description, or cover; the filename is the only source.
- **Not a sync tool.** It creates playlists and never updates one it created
  earlier. A changed export is reported, not merged.
- **`tidalapi` is pinned exactly** (`==0.8.11`). It rides the undocumented
  `api.tidal.com`, and a Tidal-side change has broken its track parser before with
  no deprecation notice. Upgrades should be deliberate, followed by a test run.

## Licensing and terms

Informational summary, not legal advice.

### This project

Licensed under the **GNU General Public License v3.0** — see [LICENSE](LICENSE).
Copies and derived works must be distributed under the same terms, with the
corresponding source made available.

### Dependencies

| Package | License | |
|---|---|---|
| `tidalapi` | **LGPL-3.0-or-later** | direct |
| `certifi` | MPL-2.0 | transitive, weak copyleft |
| `requests` | Apache-2.0 | transitive |
| `urllib3`, `charset-normalizer`, `six`, `mpegdash`, `pyaes`, `ratelimit` | MIT | transitive |
| `idna`, `isodate` | BSD | transitive |
| `python-dateutil` | BSD / Apache-2.0 dual | transitive |
| `typing_extensions` | PSF-2.0 | transitive |

`tidalapi` is the only copyleft dependency, and LGPL-3.0 sits cleanly under this
project's GPL-3.0: the LGPL expressly permits conveying the library under the GPL,
so combining them raises no obligation beyond the GPL's own. Nothing here vendors
`tidalapi` in any case — `uv sync` fetches it from PyPI.

That also simplifies redistribution. A bundled artefact — PyInstaller, Nuitka, or
a container image with site-packages baked in — distributes `tidalapi` alongside
this project, but the GPL requirement to ship corresponding source for the whole
work already covers it. Modifying `tidalapi` itself would place those
modifications under the LGPL.

The remaining licences are permissive, or in `certifi`'s case file-level weak
copyleft, and impose no conditions on this project as a consumer.

### Exportify

MIT, © 2015 Howard Wilson. This project consumes Exportify's CSV output rather
than its code, so no obligation attaches; the MIT terms would apply to vendoring
or forking it.

### Spotify

This project never calls Spotify's API — Exportify does, under its own registered
application and Spotify's Developer Terms. The exported CSVs contain Spotify
catalog metadata, which those terms restrict storing and redistributing.
`.gitignore` excludes `*.csv` for that reason, and exported playlist data should
not be committed or published.

### Tidal

`tidalapi` authenticates using a `client_id` and `client_secret` extracted from
TIDAL's own applications and obfuscated in its source. These are not
developer-issued credentials, and their use falls outside TIDAL's developer terms.
That is inherent to `tidalapi` rather than specific to this project, and access is
limited to the account that logs in.

One practical consequence: TIDAL can rotate those credentials at any time, which
would break the tool immediately. That failure mode is independent of the pinned
`tidalapi` version. The supported alternative is the official
`developer.tidal.com` API with a registered application and PKCE, which would
require reimplementing ISRC lookup, search, and playlist writes.
