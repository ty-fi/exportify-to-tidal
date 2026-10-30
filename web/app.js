// UI wiring. Matching lives in match.js, TIDAL access in tidal.js; this file
// only moves data between them and the DOM.

import { COUNTRY_CODE, CLIENT_ID, DURATION_TOLERANCE, MIN_SCORE } from "./config.js";
import {
  candidateIsrc,
  describe,
  pickIsrcCandidate,
  playlistNameFromFilename,
  readCsv,
  scoreCandidate,
  stripNoise,
} from "./match.js";
import {
  addTracks,
  beginLogin,
  createPlaylist,
  finishLogin,
  isSignedIn,
  searchTracks,
  signOut,
  tracksByIsrc,
} from "./tidal.js";
import { unzipText } from "./unzip.js";

const $ = (id) => document.getElementById(id);
const LOOKUP_DELAY_MS = 200; // polite pacing; 429s are retried in tidal.js

let tracks = [];      // parsed CSV rows
let results = [];     // { source, tidalId, method, score }
let csvLabel = "";

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function setStatus(el, msg, kind = "") {
  el.textContent = msg;
  el.className = `status ${kind}`;
}

// ---------------------------------------------------------------- auth

async function initAuth() {
  if (!CLIENT_ID) {
    $("login").disabled = true;
    setStatus(
      $("authStatus"),
      "This copy has no TIDAL client ID configured yet — see web/config.js.",
      "err",
    );
    return;
  }
  try {
    await finishLogin();
  } catch (e) {
    setStatus($("authStatus"), e.message, "err");
  }
  renderAuth();
}

function renderAuth() {
  const on = isSignedIn();
  $("login").classList.toggle("hidden", on);
  $("logout").classList.toggle("hidden", !on);
  if (on) setStatus($("authStatus"), "Signed in to Tidal.", "ok");
  else if (CLIENT_ID) setStatus($("authStatus"), "");
  refreshButtons();
}

$("login").addEventListener("click", () => {
  beginLogin().catch((e) => setStatus($("authStatus"), e.message, "err"));
});
$("logout").addEventListener("click", () => {
  signOut();
  renderAuth();
});

// ---------------------------------------------------------------- file

const drop = $("drop");
drop.addEventListener("click", () => $("file").click());
$("file").addEventListener("change", (e) => {
  if (e.target.files[0]) handleFile(e.target.files[0]);
});
["dragenter", "dragover"].forEach((ev) =>
  drop.addEventListener(ev, (e) => {
    e.preventDefault();
    drop.classList.add("over");
  }),
);
["dragleave", "drop"].forEach((ev) =>
  drop.addEventListener(ev, (e) => {
    e.preventDefault();
    drop.classList.remove("over");
  }),
);
drop.addEventListener("drop", (e) => {
  if (e.dataTransfer.files[0]) handleFile(e.dataTransfer.files[0]);
});

async function handleFile(file) {
  $("pickWrap").classList.add("hidden");
  $("results").classList.add("hidden");
  results = [];
  refreshButtons();

  try {
    if (/\.zip$/i.test(file.name)) {
      const entries = await unzipText(
        await file.arrayBuffer(),
        (n) => /\.csv$/i.test(n) && !/_unmatched\.csv$/i.test(n),
      );
      if (!entries.length) throw new Error("No playlist CSVs inside that ZIP.");
      if (entries.length === 1) {
        loadCsv(entries[0].name, entries[0].text);
      } else {
        offerChoice(entries);
      }
    } else if (/\.csv$/i.test(file.name)) {
      loadCsv(file.name, await file.text());
    } else {
      throw new Error("That's not a .csv or .zip file.");
    }
  } catch (e) {
    setStatus($("fileStatus"), e.message, "err");
  }
}

function offerChoice(entries) {
  const sel = $("pick");
  sel.innerHTML = "";
  entries.forEach((e, i) => {
    const o = document.createElement("option");
    o.value = String(i);
    o.textContent = playlistNameFromFilename(e.name.split("/").pop());
    sel.append(o);
  });
  sel.onchange = () => {
    const e = entries[Number(sel.value)];
    loadCsv(e.name, e.text);
  };
  $("pickWrap").classList.remove("hidden");
  loadCsv(entries[0].name, entries[0].text);
}

function loadCsv(name, text) {
  const base = name.split("/").pop();
  try {
    tracks = readCsv(text);
  } catch (e) {
    tracks = [];
    setStatus($("fileStatus"), e.message, "err");
    refreshButtons();
    return;
  }
  if (!tracks.length) {
    setStatus($("fileStatus"), "No playable tracks in that file.", "err");
    refreshButtons();
    return;
  }
  csvLabel = base;
  const withIsrc = tracks.filter((t) => t.isrc).length;
  setStatus(
    $("fileStatus"),
    `${base} — ${tracks.length} tracks, ${withIsrc} with a recording ID.`,
    "ok",
  );
  if (!$("plname").value.trim()) $("plname").value = playlistNameFromFilename(base);
  refreshButtons();
}

// ---------------------------------------------------------------- matching

$("match").addEventListener("click", () => {
  runMatch().catch((e) => setStatus($("matchStatus"), e.message, "err"));
});

async function matchOne(source) {
  // Pass 1: ISRC, the primary join key.
  if (source.isrc) {
    const hits = await tracksByIsrc(source.isrc, COUNTRY_CODE);
    if (hits.length) {
      const best = pickIsrcCandidate(source, hits);
      if (best) return { source, tidalId: best.id, method: "ISRC", score: 1, match: best };
    }
  }

  // Pass 2: scored text search.
  const queries = [
    `${source.title} ${source.artists[0] || ""}`.trim(),
    `${stripNoise(source.title)} ${source.artists[0] || ""}`.trim(),
  ];
  let best = null;
  let bestScore = 0;
  const seen = new Set();

  for (const q of [...new Set(queries)]) {
    let hits = [];
    try {
      hits = await searchTracks(q, COUNTRY_CODE);
    } catch {
      continue; // one bad query shouldn't abandon the track
    }
    for (const c of hits) {
      if (seen.has(c.id)) continue;
      seen.add(c.id);

      // A matching ISRC is stronger evidence than any text score, so it skips
      // scoring entirely. A *differing* one is not a veto -- the same recording
      // carries different ISRCs across distributors.
      if (source.isrc && candidateIsrc(c) === source.isrc) {
        return { source, tidalId: c.id, method: "ISRC (via search)", score: 1, match: c };
      }
      const s = scoreCandidate(source, c, DURATION_TOLERANCE);
      if (s > bestScore) {
        bestScore = s;
        best = c;
      }
    }
    if (bestScore >= 0.9) break;
  }

  if (best && bestScore >= MIN_SCORE) {
    return { source, tidalId: best.id, method: `score ${bestScore.toFixed(2)}`, score: bestScore, match: best };
  }
  return {
    source,
    tidalId: null,
    method: bestScore ? `best ${bestScore.toFixed(2)}, below ${MIN_SCORE}` : "no candidates",
    score: bestScore,
    match: null,
  };
}

async function runMatch() {
  $("match").disabled = true;
  $("create").disabled = true;
  $("prog").classList.remove("hidden");
  $("prog").max = tracks.length;
  $("prog").value = 0;
  $("results").classList.remove("hidden");
  const tbody = $("table").querySelector("tbody");
  tbody.innerHTML = "";
  results = [];

  for (const [i, source] of tracks.entries()) {
    let r;
    try {
      r = await matchOne(source);
    } catch (e) {
      r = { source, tidalId: null, method: e.message, score: 0, match: null };
    }
    results.push(r);
    tbody.append(rowFor(r));
    $("prog").value = i + 1;
    setStatus($("matchStatus"), `Looking up ${i + 1} of ${tracks.length}…`);
    await sleep(LOOKUP_DELAY_MS);
  }

  const hit = results.filter((r) => r.tidalId).length;
  $("prog").classList.add("hidden");
  setStatus(
    $("matchStatus"),
    `Found ${hit} of ${tracks.length}.` +
      (hit < tracks.length
        ? ` ${tracks.length - hit} couldn't be matched confidently and will be left out.`
        : ""),
    hit === tracks.length ? "ok" : "",
  );
  $("match").disabled = false;
  refreshButtons();
}

function rowFor(r) {
  const tr = document.createElement("tr");
  const a = document.createElement("td");
  a.textContent = describe(r.source);
  const b = document.createElement("td");
  b.textContent = r.match
    ? `${(r.match.artists || []).map((x) => x.name).join(", ")} — ${r.match.name}`
    : "— not found —";
  b.className = r.tidalId ? "hit" : "miss";
  const c = document.createElement("td");
  c.className = "r";
  c.textContent = r.method;
  tr.append(a, b, c);
  return tr;
}

// ---------------------------------------------------------------- create

$("create").addEventListener("click", () => {
  runCreate().catch((e) => setStatus($("createStatus"), e.message, "err"));
});

async function runCreate() {
  const ids = results.filter((r) => r.tidalId).map((r) => r.tidalId);
  if (!ids.length) return;
  const name = $("plname").value.trim() || playlistNameFromFilename(csvLabel);

  $("create").disabled = true;
  setStatus($("createStatus"), "Creating the playlist…");

  const id = await createPlaylist(name, "Ported from a Spotify export.", COUNTRY_CODE);
  if (!id) throw new Error("Tidal didn't return a playlist id.");

  setStatus($("createStatus"), `Adding ${ids.length} tracks…`);
  await addTracks(id, ids, COUNTRY_CODE);

  const missed = results.length - ids.length;
  setStatus(
    $("createStatus"),
    `Done — "${name}" is in your Tidal library with ${ids.length} tracks.` +
      (missed ? ` ${missed} track${missed === 1 ? "" : "s"} left out; see the table above.` : ""),
    "ok",
  );
}

// ---------------------------------------------------------------- state

function refreshButtons() {
  $("match").disabled = !(isSignedIn() && tracks.length);
  $("create").disabled = !(isSignedIn() && results.some((r) => r.tidalId));
  $("createBlurb").textContent = results.some((r) => r.tidalId)
    ? `Ready to add ${results.filter((r) => r.tidalId).length} tracks.`
    : "Once you're happy with the matches above.";
}

initAuth();
