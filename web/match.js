// Matching logic, ported from exportify_to_tidal.py.
//
// Pure functions only -- no DOM, no network -- so match.test.js can run them
// under node and check parity with the Python test suite. The Python version
// stays the reference implementation; if the two disagree, Python is right.

// Noise that shows up in Spotify titles but not always in Tidal's, and vice
// versa. Python builds this with re.VERBOSE; JS has no such flag, so it is one
// line here. Keep the alternatives in the same order as the Python source.
//
// Deliberately absent: (Live), (Acoustic), (Demo), (Remix), (Instrumental).
// Those name a *different recording*, and stripping them would let the scorer
// treat a live take as the studio cut on text alone.
const NOISE =
  /\s*[(\[-]\s*((\d{4}\s+)?(digital\s+)?(re)?master(ed)?(\s+\d{4})?|remaster(ed)?(\s+version)?|deluxe(\s+edition)?|expanded(\s+edition)?|bonus\s+track(\s+version)?|(lp|album|single)\s+version|radio\s+edit|mono|stereo)\s*[)\]]?\s*$/i;

/** Casefold, strip accents and punctuation, collapse whitespace. */
export function normalize(text) {
  return (text || "")
    .normalize("NFKD")
    .replace(/\p{M}/gu, "") // combining marks left behind by NFKD
    .toLowerCase()
    // Python's [^\w\s] keeps underscore, so keep it here too.
    .replace(/[^\p{L}\p{N}_\s]/gu, " ")
    .replace(/\s+/g, " ")
    .trim();
}

/** Drop trailing edition markers that differ between catalogues. */
export function stripNoise(text) {
  let out = text || "";
  let prev = null;
  while (prev !== out) {
    prev = out;
    out = out.replace(NOISE, "").trim();
  }
  return out;
}

/** Jaccard similarity over word tokens. */
export function tokenOverlap(a, b) {
  const ta = new Set(normalize(a).split(" ").filter(Boolean));
  const tb = new Set(normalize(b).split(" ").filter(Boolean));
  if (!ta.size || !tb.size) return 0;
  let shared = 0;
  for (const t of ta) if (tb.has(t)) shared += 1;
  return shared / (ta.size + tb.size - shared);
}

/** Token overlap, taking the better of raw and noise-stripped forms. */
export function fuzzyOverlap(a, b) {
  return Math.max(tokenOverlap(a, b), tokenOverlap(stripNoise(a), stripNoise(b)));
}

/**
 * Split Exportify's "Artist Name(s)" column.
 *
 * Exportify joins names with ", ", which is ambiguous when a name contains a
 * comma -- "Earth, Wind & Fire" would otherwise split into two artists and the
 * search query would go out as just "Earth". "Artist URI(s)" gives the true
 * count, so only trust the split when the two agree.
 */
export function parseArtists(rawNames, rawUris) {
  const names = (rawNames || "").trim();
  if (!names) return [];

  const parts = names.split(",").map((p) => p.trim()).filter(Boolean);
  const uriCount = (rawUris || "").split(",").filter((u) => u.trim()).length;

  if (!uriCount) return parts; // no URI column to cross-check; assume the split
  if (parts.length === uriCount) return parts;
  return [names]; // split disagrees with the URI count -- keep the string whole
}

export function albumName(candidate) {
  return (candidate && candidate.album && candidate.album.name) || "";
}

/** Absolute duration difference in seconds, or Infinity if either is unknown. */
export function durationDeltaS(source, candidate) {
  const candS = (candidate && candidate.duration) || 0;
  if (!source.durationMs || !candS) return Infinity;
  return Math.abs(candS * 1000 - source.durationMs) / 1000;
}

export function albumOverlap(source, candidate) {
  const cand = albumName(candidate);
  if (!source.album || !cand) return 0;
  return fuzzyOverlap(source.album, cand);
}

/**
 * Sort key for choosing among tracks that share an ISRC.
 *
 * They are the same recording by definition, so this chooses which *release*
 * the playlist entry points at -- i.e. the album art and attribution. Ordered
 * by length agreement, then album lineage (noise-stripped, so every
 * "Nevermind (... Edition)" ties), then the raw album string so the exact
 * edition the source named wins over a bare reissue, then the least edition
 * cruft. The trailing id keeps the choice stable when TIDAL reorders its
 * response -- without it the same CSV can resolve to a different release.
 */
export function isrcCandidateRank(source, candidate) {
  return [
    durationDeltaS(source, candidate),
    -albumOverlap(source, candidate),
    -tokenOverlap(source.album, albumName(candidate)),
    albumName(candidate).length,
    Number(candidate.id),
  ];
}

/** Lexicographic compare of two rank tuples. */
export function compareRanks(a, b) {
  for (let i = 0; i < a.length; i += 1) {
    if (a[i] < b[i]) return -1;
    if (a[i] > b[i]) return 1;
  }
  return 0;
}

export function pickIsrcCandidate(source, candidates) {
  let best = null;
  let bestKey = null;
  for (const c of candidates) {
    const key = isrcCandidateRank(source, c);
    if (bestKey === null || compareRanks(key, bestKey) < 0) {
      best = c;
      bestKey = key;
    }
  }
  return best;
}

/**
 * Score a Tidal search hit against the source row, 0.0-1.0.
 *
 * Weighted title 0.4 / artist 0.3 / duration 0.2 / album 0.1, plus one hard
 * check: a candidate more than 3x the tolerance from the expected length is a
 * different recording -- a cover, a remix, an extended mix, or another song
 * with the same title -- not a near miss, so it scores zero.
 *
 * When either side has no duration, that weight is redistributed across the
 * text signals rather than scored as zero. A candidate shouldn't be penalised
 * for metadata we never had.
 */
export function scoreCandidate(source, candidate, durationTolerance) {
  const titleScore = fuzzyOverlap(source.title, candidate.name);
  const artistScore = tokenOverlap(
    source.artists.join(" "),
    (candidate.artists || []).map((a) => a.name).join(" "),
  );
  const albumScore = albumOverlap(source, candidate);

  const delta = durationDeltaS(source, candidate);

  if (!Number.isFinite(delta)) {
    // Renormalise over the weights we can actually evaluate (0.40+0.30+0.10).
    const total = (0.4 * titleScore + 0.3 * artistScore + 0.1 * albumScore) / 0.8;
    return Math.min(total, 1);
  }

  if (delta > durationTolerance * 3) return 0; // different recording

  const tolerance = Math.max(durationTolerance, 1);
  let durationScore;
  if (delta <= tolerance) {
    durationScore = 1 - (delta / tolerance) * 0.5; // 1.0 -> 0.5
  } else {
    // Taper 0.5 -> 0.0 across the tolerance..3x band.
    durationScore = 0.5 * (1 - (delta - tolerance) / (2 * tolerance));
  }

  const total =
    0.4 * titleScore + 0.3 * artistScore + 0.2 * durationScore + 0.1 * albumScore;
  // The weights sum to 1.0 in decimal but to 1.0000000000000002 in binary.
  return Math.min(total, 1);
}

/**
 * A search hit's own ISRC, when the payload happens to carry one.
 *
 * A *matching* ISRC promotes a hit to an exact match. A *differing* one is
 * deliberately not a veto: the same recording legitimately carries different
 * ISRCs across distributors, which real data confirmed (Spotify USCGH1234432
 * vs TIDAL USCGJ0782214 for the same CKY recording).
 */
export function candidateIsrc(candidate) {
  return ((candidate && candidate.isrc) || "").trim().toUpperCase();
}

// ------------------------------------------------------------------ CSV

/** Exportify writes these exact headers. Fail loudly if they have drifted. */
export const REQUIRED_COLUMNS = [
  "Track Name",
  "Artist URI(s)",
  "Artist Name(s)",
  "Album Name",
  "Track Duration (ms)",
  "ISRC",
];

/** Minimal RFC 4180 reader: quoted fields, embedded commas, doubled quotes. */
export function parseCsv(text) {
  const rows = [];
  let row = [];
  let field = "";
  let quoted = false;
  let i = 0;

  if (text.charCodeAt(0) === 0xfeff) i = 1; // strip BOM

  for (; i < text.length; i += 1) {
    const c = text[i];
    if (quoted) {
      if (c === '"') {
        if (text[i + 1] === '"') {
          field += '"';
          i += 1;
        } else {
          quoted = false;
        }
      } else {
        field += c;
      }
    } else if (c === '"') {
      quoted = true;
    } else if (c === ",") {
      row.push(field);
      field = "";
    } else if (c === "\n" || c === "\r") {
      if (c === "\r" && text[i + 1] === "\n") i += 1;
      row.push(field);
      rows.push(row);
      row = [];
      field = "";
    } else {
      field += c;
    }
  }
  if (field !== "" || row.length) {
    row.push(field);
    rows.push(row);
  }
  return rows;
}

/**
 * Turn an Exportify CSV into source tracks.
 *
 * Throws with a pointed message when the headers don't match -- the usual cause
 * is exportify.net (pavelkomarov), which has no ISRC column, rather than
 * exportify.app (watsonbox).
 */
export function readCsv(text) {
  const rows = parseCsv(text).filter((r) => r.some((c) => c !== ""));
  if (!rows.length) throw new Error("That file is empty.");

  const headers = rows[0].map((h) => h.trim());
  const missing = REQUIRED_COLUMNS.filter((c) => !headers.includes(c));
  if (missing.length) {
    throw new Error(
      `This CSV is missing: ${missing.join(", ")}. ` +
        "It needs to come from exportify.app (watsonbox), not exportify.net " +
        "(pavelkomarov) -- the latter has no ISRC column.",
    );
  }

  const idx = Object.fromEntries(headers.map((h, n) => [h, n]));
  const get = (r, name) => (idx[name] === undefined ? "" : (r[idx[name]] || "").trim());

  const tracks = [];
  rows.slice(1).forEach((r, n) => {
    const title = get(r, "Track Name");
    if (!title) return; // local files and removed tracks come through blank

    const durRaw = get(r, "Track Duration (ms)");
    const durationMs = Number.isFinite(parseFloat(durRaw))
      ? Math.trunc(parseFloat(durRaw))
      : 0;

    tracks.push({
      title,
      artists: parseArtists(get(r, "Artist Name(s)"), get(r, "Artist URI(s)")),
      album: get(r, "Album Name"),
      durationMs,
      isrc: get(r, "ISRC").toUpperCase(),
      spotifyUri: get(r, "Track URI"),
      row: n + 2, // row 1 is the header
    });
  });
  return tracks;
}

export function describe(t) {
  return `${t.artists.join(", ")} — ${t.title}`;
}

/** Playlist name from a slugified Exportify filename. See the Python twin. */
const SMALL_WORDS = new Set([
  "a", "an", "and", "at", "but", "by", "for", "from", "in", "nor", "of",
  "on", "or", "the", "to", "vs", "with",
]);

export function playlistNameFromFilename(filename) {
  const stem = filename.replace(/\.[^.]+$/, "");
  const words = stem.split(/[\s_-]+/).filter(Boolean);
  if (!words.length) return stem;
  // Existing capitals probably mean something (acronyms, band names).
  if (/[A-Z]/.test(stem)) return words.join(" ");
  return words
    .map((w, i) =>
      SMALL_WORDS.has(w) && i !== 0 && i !== words.length - 1
        ? w
        : w.charAt(0).toUpperCase() + w.slice(1),
    )
    .join(" ");
}
