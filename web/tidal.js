// TIDAL auth (Authorization Code + PKCE) and openapi v2 access, browser-side.
//
// No server and no client secret: PKCE is designed for exactly this, and the
// token never leaves this browser. Verified 2026-10-30 that openapi.tidal.com
// and auth.tidal.com both send permissive CORS headers, including
// `authorization` as an allowed request header and POST on the playlist routes.
//
// The v2 API differs from python-tidalapi in two ways that silently break
// matching if you miss them, so adaptTrack() normalises both here and match.js
// keeps the tidalapi-shaped objects it was written against:
//   * `duration` is an ISO 8601 string ("PT3M43S"), not integer seconds
//   * the title field is `title`, not `name`

import { CLIENT_ID, SCOPES } from "./config.js";

const AUTHORIZE = "https://login.tidal.com/authorize";
const TOKEN = "https://auth.tidal.com/v1/oauth2/token";
const API = "https://openapi.tidal.com/v2";

const STORE = "tidal_token";
const VERIFIER = "tidal_pkce_verifier";
const STATE = "tidal_pkce_state";

// ---------------------------------------------------------------- PKCE

function randomString(bytes = 48) {
  const a = new Uint8Array(bytes);
  crypto.getRandomValues(a);
  return base64url(a);
}

function base64url(bytes) {
  let s = "";
  for (const b of bytes) s += String.fromCharCode(b);
  return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

async function challengeFor(verifier) {
  const digest = await crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(verifier),
  );
  return base64url(new Uint8Array(digest));
}

export function redirectUri() {
  // Must match the redirect URI registered on the TIDAL app exactly, so strip
  // any query or hash left over from a previous round trip.
  return window.location.origin + window.location.pathname;
}

export async function beginLogin() {
  if (!CLIENT_ID) throw new Error("No TIDAL client ID configured. See web/config.js.");
  const verifier = randomString();
  const state = randomString(16);
  sessionStorage.setItem(VERIFIER, verifier);
  sessionStorage.setItem(STATE, state);

  const q = new URLSearchParams({
    response_type: "code",
    client_id: CLIENT_ID,
    redirect_uri: redirectUri(),
    scope: SCOPES.join(" "),
    code_challenge_method: "S256",
    code_challenge: await challengeFor(verifier),
    state,
  });
  window.location.assign(`${AUTHORIZE}?${q}`);
}

/**
 * Complete the round trip if we just came back from TIDAL.
 * Returns true when a token was obtained, false when there was nothing to do.
 */
export async function finishLogin() {
  const params = new URLSearchParams(window.location.search);
  const code = params.get("code");
  if (!code) {
    if (params.get("error")) {
      cleanUrl();
      throw new Error(params.get("error_description") || params.get("error"));
    }
    return false;
  }

  const expected = sessionStorage.getItem(STATE);
  if (!expected || params.get("state") !== expected) {
    cleanUrl();
    throw new Error("Login state mismatch — please try signing in again.");
  }
  const verifier = sessionStorage.getItem(VERIFIER);
  sessionStorage.removeItem(STATE);
  sessionStorage.removeItem(VERIFIER);

  const body = new URLSearchParams({
    grant_type: "authorization_code",
    code,
    redirect_uri: redirectUri(),
    client_id: CLIENT_ID,
    code_verifier: verifier,
  });
  const res = await fetch(TOKEN, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body,
  });
  cleanUrl();
  if (!res.ok) throw new Error(`Tidal rejected the sign-in (HTTP ${res.status}).`);
  storeToken(await res.json());
  return true;
}

function cleanUrl() {
  window.history.replaceState({}, "", redirectUri());
}

function storeToken(t) {
  localStorage.setItem(
    STORE,
    JSON.stringify({
      access_token: t.access_token,
      refresh_token: t.refresh_token || readToken()?.refresh_token,
      expires_at: Date.now() + (t.expires_in || 3600) * 1000 - 60_000,
    }),
  );
}

function readToken() {
  try {
    return JSON.parse(localStorage.getItem(STORE) || "null");
  } catch {
    return null;
  }
}

export function isSignedIn() {
  return Boolean(readToken()?.access_token);
}

export function signOut() {
  localStorage.removeItem(STORE);
}

async function accessToken() {
  const t = readToken();
  if (!t) throw new Error("Not signed in to Tidal.");
  if (Date.now() < t.expires_at) return t.access_token;
  if (!t.refresh_token) {
    signOut();
    throw new Error("Your Tidal session expired — please sign in again.");
  }
  const res = await fetch(TOKEN, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      grant_type: "refresh_token",
      refresh_token: t.refresh_token,
      client_id: CLIENT_ID,
    }),
  });
  if (!res.ok) {
    signOut();
    throw new Error("Your Tidal session expired — please sign in again.");
  }
  storeToken(await res.json());
  return readToken().access_token;
}

// ---------------------------------------------------------------- requests

async function api(path, { method = "GET", body, retry = true } = {}) {
  const res = await fetch(`${API}${path}`, {
    method,
    headers: {
      Authorization: `Bearer ${await accessToken()}`,
      Accept: "application/vnd.api+json",
      ...(body ? { "Content-Type": "application/vnd.api+json" } : {}),
    },
    ...(body ? { body: JSON.stringify(body) } : {}),
  });

  if (res.status === 429 && retry) {
    const wait = Number(res.headers.get("retry-after") || 5);
    await new Promise((r) => setTimeout(r, wait * 1000));
    return api(path, { method, body, retry: false });
  }
  if (res.status === 404) return null;
  if (!res.ok) {
    let detail = "";
    try {
      const j = await res.json();
      detail = j?.errors?.map((e) => e.detail || e.title).filter(Boolean).join("; ") || "";
    } catch {
      /* non-JSON error body */
    }
    throw new Error(`Tidal API ${res.status}${detail ? `: ${detail}` : ""}`);
  }
  if (res.status === 204 || res.headers.get("content-length") === "0") return null;
  const text = await res.text();
  return text ? JSON.parse(text) : null;
}

// ---------------------------------------------------------------- adapters

/** "PT3M43S" -> 223. Months (the M before T) are not minutes. */
export function iso8601Seconds(s) {
  if (!s) return 0;
  const m =
    /^P(?:(\d+)Y)?(?:(\d+)M)?(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:([\d.]+)S)?)?$/.exec(
      s,
    );
  if (!m) return 0;
  const [, , , w, d, h, mi, sec] = m;
  return (
    (+w || 0) * 604800 + (+d || 0) * 86400 + (+h || 0) * 3600 + (+mi || 0) * 60 + (+sec || 0)
  );
}

/** Index a JSON:API `included` array as "type:id" -> resource. */
function indexIncluded(doc) {
  const byKey = new Map();
  for (const r of doc?.included || []) byKey.set(`${r.type}:${r.id}`, r);
  return byKey;
}

function related(resource, name, included) {
  const refs = resource?.relationships?.[name]?.data || [];
  return refs.map((r) => included.get(`${r.type}:${r.id}`)).filter(Boolean);
}

/** v2 track resource -> the tidalapi-shaped object match.js expects. */
export function adaptTrack(resource, included) {
  const a = resource.attributes || {};
  const albums = related(resource, "albums", included);
  return {
    id: resource.id,
    name: a.title || "",
    duration: iso8601Seconds(a.duration),
    isrc: a.isrc || null,
    artists: related(resource, "artists", included).map((x) => ({
      name: x.attributes?.name || "",
    })),
    album: albums.length ? { name: albums[0].attributes?.title || "" } : null,
  };
}

function adaptAll(doc) {
  if (!doc) return [];
  const included = indexIncluded(doc);
  const data = Array.isArray(doc.data) ? doc.data : doc.data ? [doc.data] : [];
  return data.filter((r) => r.type === "tracks").map((r) => adaptTrack(r, included));
}

// ---------------------------------------------------------------- endpoints

export async function tracksByIsrc(isrc, countryCode) {
  const q = new URLSearchParams({
    countryCode,
    "filter[isrc]": isrc,
    include: "artists,albums",
  });
  return adaptAll(await api(`/tracks?${q}`));
}

export async function searchTracks(query, countryCode) {
  const q = new URLSearchParams({
    countryCode,
    include: "tracks,tracks.artists,tracks.albums",
  });
  const doc = await api(`/searchResults/${encodeURIComponent(query)}?${q}`);
  if (!doc) return [];
  // Search returns the hits in `included` rather than `data`, so adapt the
  // track resources found there.
  const included = indexIncluded(doc);
  return [...included.values()]
    .filter((r) => r.type === "tracks")
    .map((r) => adaptTrack(r, included));
}

export async function currentUser() {
  const doc = await api("/users/me");
  return doc?.data || null;
}

export async function createPlaylist(name, description, countryCode) {
  // Only `name` and `description` are sent. The generated spec calls the
  // visibility field `accessType` while TIDAL's own JS example sends `privacy`;
  // rather than guess, leave it out and take the account default.
  const q = new URLSearchParams({ countryCode });
  const doc = await api(`/playlists?${q}`, {
    method: "POST",
    body: {
      data: {
        type: "playlists",
        attributes: { name, ...(description ? { description } : {}) },
      },
    },
  });
  return doc?.data?.id || null;
}

/** Add track ids to a playlist. TIDAL caps a single call, so this chunks. */
export async function addTracks(playlistId, trackIds, countryCode, chunk = 20) {
  const q = new URLSearchParams({ countryCode });
  for (let i = 0; i < trackIds.length; i += chunk) {
    await api(`/playlists/${playlistId}/relationships/items?${q}`, {
      method: "POST",
      body: {
        data: trackIds.slice(i, i + chunk).map((id) => ({ id: String(id), type: "tracks" })),
        meta: { onDuplicates: "SKIP" },
      },
    });
  }
}
