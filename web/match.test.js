// Parity tests for match.js against the Python reference in test_matching.py.
//
//     node web/match.test.js
//
// Plain asserts and an explicit exit code rather than `node --test`, which
// exits 0 when it matches no files -- a renamed file would leave this green
// having run nothing.

import {
  albumOverlap,
  describe as describeTrack,
  durationDeltaS,
  fuzzyOverlap,
  parseArtists,
  parseCsv,
  pickIsrcCandidate,
  playlistNameFromFilename,
  readCsv,
  scoreCandidate,
  stripNoise,
} from "./match.js";

const TOL = 4;
let passed = 0;
const failures = [];

function check(label, cond, detail = "") {
  if (cond) {
    passed += 1;
    console.log(`  ok    ${label}`);
  } else {
    failures.push(`${label}: ${detail}`);
    console.log(`  FAIL  ${label}  ${detail}`);
  }
}

const close = (a, b) => Math.abs(a - b) < 1e-9;

function track(id, name, duration = null, artists = [], album = null, isrc = null) {
  return {
    id,
    name,
    duration,
    artists: artists.map((a) => ({ name: a })),
    album: album ? { name: album } : null,
    isrc,
  };
}

function src(title, artists, album, durationMs, isrc = "") {
  return { title, artists, album, durationMs, isrc, spotifyUri: "x", row: 2 };
}

// ---------------------------------------------------------------- artists

console.log("\nparseArtists");
check("single artist", JSON.stringify(parseArtists("Sigur Rós", "spotify:artist:s1")) === '["Sigur Rós"]');
check(
  "comma inside one artist name stays whole",
  JSON.stringify(parseArtists("Earth, Wind & Fire", "spotify:artist:s3")) === '["Earth, Wind & Fire"]',
  JSON.stringify(parseArtists("Earth, Wind & Fire", "spotify:artist:s3")),
);
check(
  "two artists split when URI count agrees",
  JSON.stringify(parseArtists("Jay-Z, Alicia Keys", "a,b")) === '["Jay-Z","Alicia Keys"]',
);
check(
  "ambiguous split falls back to whole string",
  JSON.stringify(parseArtists("Earth, Wind & Fire, Jay-Z", "a,b")) === '["Earth, Wind & Fire, Jay-Z"]',
);
check("empty is empty", parseArtists("", "").length === 0);
check("no URI column assumes the split", JSON.stringify(parseArtists("A, B", "")) === '["A","B"]');

// ---------------------------------------------------------------- noise

console.log("\nstripNoise — edition markers go, recording markers stay");
for (const m of [
  "(LP Version)", "(Album Version)", "(Single Version)", "(Radio Edit)",
  "(Remastered)", "(2011 Remaster)", "(Deluxe Edition)", "(Expanded Edition)",
  "(Bonus Track Version)", "- Remastered",
]) {
  const got = stripNoise(`Some Song ${m}`);
  check(`${m} stripped`, got === "Some Song", `got ${JSON.stringify(got)}`);
}
for (const m of ["(Live)", "(Acoustic)", "(Demo)", "(Remix)", "(Instrumental)"]) {
  const got = stripNoise(`Some Song ${m}`);
  check(`${m} preserved`, got === `Some Song ${m}`, `got ${JSON.stringify(got)}`);
}
check(
  "96 Quite Bitter Beings (LP Version) matches the plain title",
  fuzzyOverlap("96 Quite Bitter Beings (LP Version)", "96 Quite Bitter Beings") === 1,
);
check(
  "album remaster marker stripped (real 95_dodge_caravan case)",
  albumOverlap(
    src("Drain You", ["Nirvana"], "Nevermind (Remastered)", 223000),
    track(1, "Drain You", 223, ["Nirvana"], "Nevermind"),
  ) === 1,
);

// ---------------------------------------------------------------- scoring

console.log("\nscoreCandidate — duration corroborates, vetoes far outliers");
const s1 = src("Drain You", ["Nirvana"], "Nevermind (Remastered)", 223000);
check(
  "perfect match scores 1.0",
  close(scoreCandidate(s1, track(1, "Drain You", 223, ["Nirvana"], "Nevermind"), TOL), 1),
  `${scoreCandidate(s1, track(1, "Drain You", 223, ["Nirvana"], "Nevermind"), TOL)}`,
);
check(
  "45s difference is refused outright",
  scoreCandidate(s1, track(3, "Drain You", 268, ["Nirvana"], "Some Live Set"), TOL) === 0,
);
check(
  "just past 3x tolerance is refused",
  scoreCandidate(s1, track(4, "Drain You", 236, ["Nirvana"], "Nevermind"), TOL) === 0,
);
const mid = scoreCandidate(s1, track(5, "Drain You", 231, ["Nirvana"], "Nevermind"), TOL);
check("8s drift survives but is penalised", mid > 0 && mid < 1, `${mid}`);
check(
  "2s drift outscores 8s drift",
  scoreCandidate(s1, track(6, "Drain You", 225, ["Nirvana"], "Nevermind"), TOL) > mid,
);

console.log("\nscoreCandidate — a missing length redistributes, it doesn't refuse");
check(
  "full text agreement reaches 1.0 without a duration",
  close(scoreCandidate(s1, track(2, "Drain You", null, ["Nirvana"], "Nevermind"), TOL), 1),
  `${scoreCandidate(s1, track(2, "Drain You", null, ["Nirvana"], "Nevermind"), TOL)}`,
);
check(
  "unknown source duration behaves the same way",
  close(
    scoreCandidate(
      src("Drain You", ["Nirvana"], "Nevermind", 0),
      track(1, "Drain You", 223, ["Nirvana"], "Nevermind"),
      TOL,
    ),
    1,
  ),
);
check(
  "title alone is still below the 0.72 default",
  scoreCandidate(s1, track(7, "Drain You", null, ["Someone Else"], "Other Album"), TOL) < 0.72,
);
check(
  "unknown duration reports infinite delta",
  !Number.isFinite(durationDeltaS(s1, track(2, "Drain You", null))),
);

// ---------------------------------------------------------------- release tie

console.log("\npickIsrcCandidate — which release wins a duration tie");
const nirvanaSrc = src("Drain You", ["Nirvana"], "Nevermind (Remastered)", 223880, "USGF19942508");
const nirvanaCands = [
  track(77610765, "Drain You", 224, ["Nirvana"], "Nevermind"),
  track(7772090, "Drain You", 224, ["Nirvana"], "Nevermind (Deluxe Edition)"),
  track(203937595, "Drain You", 224, ["Nirvana"], "Nevermind (30th Anniversary Super Deluxe)"),
  track(7772065, "Drain You", 224, ["Nirvana"], "Nevermind (Remastered)"),
  track(7772133, "Drain You", 224, ["Nirvana"], "Nevermind (Super Deluxe Edition)"),
  track(203938235, "Drain You", 224, ["Nirvana"], "Nevermind (Remastered 2021)"),
];
check(
  "exact album edition beats a bare reissue",
  pickIsrcCandidate(nirvanaSrc, nirvanaCands).id === 7772065,
  `got ${pickIsrcCandidate(nirvanaSrc, nirvanaCands).id}`,
);
const winners = new Set();
for (let r = 0; r < nirvanaCands.length; r += 1) {
  const rotated = nirvanaCands.slice(r).concat(nirvanaCands.slice(0, r));
  winners.add(pickIsrcCandidate(nirvanaSrc, rotated).id);
}
check("choice is stable across every response ordering", winners.size === 1 && winners.has(7772065), [...winners].join(","));
check(
  "duration outranks album agreement",
  pickIsrcCandidate(src("Track", ["Artist"], "Exact Album", 200000), [
    track(1, "Track", 200, ["Artist"], "Some Other Album"),
    track(2, "Track", 206, ["Artist"], "Exact Album"),
  ]).id === 1,
);

// ---------------------------------------------------------------- csv

console.log("\nreadCsv");
const CSV = [
  '"Track URI","Track Name","Artist URI(s)","Artist Name(s)","Album URI","Album Name","Track Duration (ms)","ISRC"',
  '"spotify:track:a","Hoppípolla","spotify:artist:s1","Sigur Rós","x","Takk...","268000","GBAYE0500886"',
  '"spotify:track:c","September","spotify:artist:s3","Earth, Wind & Fire","x","The Best Of","215000","USSM17800364"',
  '"","","","","","","",""',
].join("\n");
const parsed = readCsv(CSV);
check("blank row skipped", parsed.length === 2, `got ${parsed.length}`);
check("quoted comma artist kept whole", parsed[1].artists.length === 1, JSON.stringify(parsed[1].artists));
check("duration parsed", parsed[0].durationMs === 268000);
check("isrc upcased", parsed[0].isrc === "GBAYE0500886");
check("describe renders", describeTrack(parsed[0]) === "Sigur Rós — Hoppípolla", describeTrack(parsed[0]));
check(
  "wrong exportify is rejected by name",
  (() => {
    try {
      readCsv('"Track Name","Album Name"\n"a","b"');
      return false;
    } catch (e) {
      return e.message.includes("exportify.app");
    }
  })(),
);
check("CRLF handled", parseCsv("a,b\r\nc,d").length === 2);

// ---------------------------------------------------------------- filename

console.log("\nplaylistNameFromFilename");
for (const [f, want] of [
  ["95_dodge_caravan.csv", "95 Dodge Caravan"],
  ["best_of_the_80s.csv", "Best of the 80s"],
  ["road-trip-mix.csv", "Road Trip Mix"],
  ["the_wall.csv", "The Wall"],
  ["REM_Automatic.csv", "REM Automatic"],
]) {
  const got = playlistNameFromFilename(f);
  check(`${f} -> ${want}`, got === want, `got ${JSON.stringify(got)}`);
}

console.log(`\n${passed} passed, ${failures.length} failed`);
if (failures.length) {
  console.log("\nFailures:");
  for (const f of failures) console.log(`  - ${f}`);
  process.exit(1);
}
