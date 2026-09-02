# exportify.app to tidal playlist port utility  app

Port Spotify playlists into Tidal from an [Exportify](https://exportify.app/) CSV,
matching on ISRC first with a scored text-search fallback.

Built for a recurring problem: people send me Spotify playlists, I use Tidal.
Nothing is ever silently substituted — a track that can't be matched confidently
goes to a sidecar CSV so you can see the gap rather than discover a wrong
recording six months later.

## Why not one of the existing tools

Spotify reduced Development Mode in scope in February 2026: new Client IDs need
Premium, are capped at five authorized users, and — fatally here — can only read
the contents of playlists the authenticated user *owns*. Since the whole point is
reading other people's playlists, the self-hosted route is closed.

The way around it is to not touch Spotify's API at all. exportify.app runs under
an **extended quota mode** app, which the February 2026 migration guide exempts
from every one of those restrictions. You export a CSV in the browser; this tool
only ever reads that file.

On the Tidal side there's no developer app to register either. `tidalapi`'s
device-code token authenticates against the official `openapi.tidal.com/v2`
catalogue, so ISRC lookup, search, and playlist writes all run through one login.

Full reasoning, decision log, and open questions: [spotify-to-tidal-context.md](spotify-to-tidal-context.md).

## Requirements

- Python 3.11+
- [uv](https://docs.astral.sh/uv/)
- A Tidal account (you'll log in once via device code)

## Setup

```bash
uv sync
```

## Getting a CSV

Use **exportify.app** (watsonbox), *not* exportify.net — they're unrelated
projects and only exportify.app includes the ISRC column this tool matches on.

1. Follow the playlist in Spotify so it lands in your library.
2. Open [exportify.app](https://exportify.app/), grant read-only access, and
   export. "Export All" gives you a zip of every playlist in your account.
3. Unzip into `exportify-exports/`.

Don't self-host Exportify — you'd register your own Client ID and inherit every
restriction described above.

## Usage

Port everything not already done:

```bash
uv run exportify_to_tidal.py
```

Check the queue first — this needs no Tidal login:

```bash
uv run exportify_to_tidal.py --list
```

```
2 to port, oldest first:
  1. 95_dodge_caravan.csv  →  '95 Dodge Caravan'
  2. best_of_the_80s.csv   →  'Best of the 80s'
```

Playlist names come from filenames, since Exportify's CSV contains no
playlist-level metadata at all. An all-lowercase name is title-cased with small
words kept down; anything containing capitals is left alone so acronyms survive.
`--list` is where you'd catch a name you don't like.

Other common runs:

```bash
# match and report without writing anything to Tidal
uv run exportify_to_tidal.py --dry-run

# one specific file, explicit name
uv run exportify_to_tidal.py some.csv --name "Ben's Mix"

# record exports you already ported by hand, so they aren't done twice
uv run exportify_to_tidal.py --mark-done
```

### Flags

| Flag | Effect |
|---|---|
| `--list` | Show the queue and derived names, then stop. No login needed. |
| `--dry-run` | Match and report, create nothing. Still logs in (ISRC lookup goes through the session). |
| `--mark-done` | Record queued CSVs as ported without contacting Tidal. |
| `--reprocess` | Ignore the ledger. Creates a **new** playlist; does not update the old one. |
| `--exports-dir` | Directory to scan (default `exportify-exports`). |
| `--isrc-only` | Exact ISRC matches or nothing; skip the text fallback. |
| `--min-score` | Fallback threshold, 0–1 (default 0.72). |
| `--duration-tolerance` | Seconds of drift treated as the same recording (default 4). Beyond 3× this is refused. |
| `--no-cache` | Bypass the match cache. **Use this when tuning the two flags above**, or cached results will mask your changes. |
| `--delay` | Seconds between lookups (default 1.0). |
| `--name`, `--description` | Playlist metadata. `--name` only works with a single CSV. |
| `--debug` | Show every candidate with duration deltas and album overlaps. |

## How matching works

1. **ISRC** via `Session.get_tracks_by_isrc` against Tidal's openapi v2 catalogue.
   Exact. This resolves the overwhelming majority of tracks in practice.
2. **Scored text search** for anything ISRC misses — title 0.4 / artist 0.3 /
   duration 0.2 / album 0.1, thresholded at `--min-score`. A candidate more than
   3× the tolerance from the expected length is refused as a different recording
   (cover, remix, extended mix). A search hit whose *own* ISRC matches the source
   is promoted to an exact match.

Unmatched tracks are written to `<name>_unmatched.csv` next to the input.

Re-running is safe. A ledger keyed on filename skips anything already ported; a
content hash detects an export that *changed* and reports it rather than creating
a duplicate playlist.

## State

Three files under `~/.config/exportify_to_tidal/`:

| File | Contents |
|---|---|
| `session.json` | Tidal OAuth tokens. **Treat as a credential.** |
| `matches.json` | ISRC/URI → Tidal track id. Shared across playlists, so overlapping ones cost no API calls. |
| `processed.json` | Which exports have been ported. |

Deleting `matches.json` forces a re-resolve; deleting `processed.json` makes
everything look unported.

## Tests

```bash
uv run test_matching.py
```

54 assertions, no network, no pytest. Covers the scorer, the ISRC edge cases,
release-tie determinism, cache reuse rules, filename→name derivation, and
discovery.

## Known limitations

- **Playlist cover art can't be transferred.** Tidal's API has
  `GET /playlists/{id}/relationships/coverArt` but no POST, and the Exportify CSV
  has no playlist cover anyway. Set covers by hand in the Tidal app.
- **No playlist metadata beyond the name.** The CSV carries no playlist URI,
  description, or cover — the filename is all there is.
- **Not a sync tool.** It creates playlists; it never updates one it made
  earlier. A changed export is reported, not merged.
- **`tidalapi` is pinned exactly** (`==0.8.11`). It rides the undocumented
  `api.tidal.com` and has broken on a Tidal-side change before with no
  deprecation notice. Upgrade deliberately, then re-run the tests.

## Licensing and terms

*Not legal advice — this is a practical summary. Verify anything that matters to
you.*

**This repository has no LICENSE file**, which means default copyright applies:
all rights reserved, and nobody else may reuse it. If you want it to be usable by
others, add one — MIT or Apache-2.0 both work here.

**Dependencies.** One direct dependency, and it is the only copyleft item:

| Package | License | Note |
|---|---|---|
| `tidalapi` | **LGPL-3.0-or-later** | Direct dependency |
| `certifi` | MPL-2.0 | Transitive, weak copyleft |
| `requests` | Apache-2.0 | Transitive |
| `urllib3`, `charset-normalizer`, `six`, `mpegdash`, `pyaes`, `ratelimit` | MIT | Transitive |
| `idna`, `isodate` | BSD | Transitive |
| `python-dateutil` | BSD / Apache-2.0 dual | Transitive |
| `typing_extensions` | PSF-2.0 | Transitive |

The LGPL on `tidalapi` does **not** force this project to be LGPL. Importing a
Python module counts as dynamic linking, so your own code stays under whatever
licence you choose. Two things to keep true:

- We don't vendor or redistribute `tidalapi` — `uv sync` fetches it from PyPI —
  which keeps the obligations to essentially "say that you use it." Done here.
- If you ever ship a **bundled binary** (PyInstaller, Nuitka, a container image
  with site-packages baked in), you'd be distributing `tidalapi` and would then
  owe recipients its licence text, source availability, and the ability to
  substitute their own build of it.

Modifying `tidalapi` itself would put those changes under LGPL. We don't.

**Exportify** is MIT (© 2015 Howard Wilson). We consume its CSV output rather
than its code, so no obligation attaches — the MIT terms would only matter if you
vendored or forked it. Worth noting the project is being used exactly as intended
here; nothing is being scraped or circumvented.

**Spotify.** This tool never calls Spotify's API. Exportify does, under its own
registered app and Spotify's Developer Terms. The CSVs it produces contain
Spotify catalog metadata, and Spotify's terms restrict storing and redistributing
that content — so **don't commit or publish the CSVs**. `.gitignore` excludes
`*.csv` for exactly this reason, not just tidiness. Exporting playlists you
follow, for your own use, is what Exportify is for.

**Tidal — the honest caveat.** `tidalapi` authenticates using a `client_id` and
`client_secret` extracted from TIDAL's own applications and obfuscated in its
source (double base64 in `session.py`). That is not a credential TIDAL issued to
you, and using it sits outside TIDAL's developer terms — this is true of every
`tidalapi` user, not something specific to this project. Practically it's
read/write against your own account and your own subscription. Two consequences
worth accepting knowingly:

- TIDAL could rotate those credentials at any time and this tool would stop
  working immediately. That's a distinct failure mode from API drift.
- If you'd rather be on supported footing, the route is the official
  `developer.tidal.com` API with your own registered app and PKCE. That means
  reimplementing ISRC lookup, search, *and* playlist writes — see D6 in the
  context doc for why that trade was declined.
