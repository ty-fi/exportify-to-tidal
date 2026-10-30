// Deployment configuration.
//
// CLIENT_ID is a *public* identifier. PKCE exists precisely so a browser app
// needs no client secret, so this is safe to commit and to serve.
//
// To deploy your own copy:
//   1. Register an app at https://developer.tidal.com (free, self-serve).
//   2. Set its redirect URI to exactly where this page is served from,
//      including the path — e.g. https://you.github.io/exportify-to-tidal/
//      Trailing slash matters; it must match what tidal.js sends.
//   3. Paste the client ID below.
export const CLIENT_ID = "";

// Only what this page actually does: read and write playlists, and identify
// the signed-in account. Deliberately narrower than TIDAL's own example, which
// also asks for playback and collection write — a consent screen listing
// permissions the page never uses is a reason for someone to back out.
export const SCOPES = ["playlists.read", "playlists.write", "user.read"];

// Catalogue country. Availability and which releases come back vary by market.
export const COUNTRY_CODE = "US";

// Scorer settings, mirroring the Python CLI defaults.
export const MIN_SCORE = 0.72;
export const DURATION_TOLERANCE = 4; // seconds
