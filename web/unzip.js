// Minimal ZIP reader for Exportify's "Export All" download.
//
// Hand-rolled rather than pulling in JSZip: the page has no build step and no
// dependencies, and this only needs to walk a central directory and inflate
// stored or deflated entries. DecompressionStream("deflate-raw") does the
// actual inflating and is available in every browser that also supports the
// crypto.subtle calls the PKCE flow already requires.

const EOCD_SIG = 0x06054b50;
const CEN_SIG = 0x02014b50;

/**
 * Extract the text files from a ZIP.
 * Returns [{ name, text }] for entries matching `filter` (by name).
 */
export async function unzipText(buffer, filter = () => true) {
  const view = new DataView(buffer);
  const eocd = findEocd(view);
  if (eocd < 0) throw new Error("That doesn't look like a ZIP file.");

  const count = view.getUint16(eocd + 10, true);
  let p = view.getUint32(eocd + 16, true); // central directory offset

  const out = [];
  for (let i = 0; i < count; i += 1) {
    if (view.getUint32(p, true) !== CEN_SIG) break;

    const method = view.getUint16(p + 10, true);
    const compressedSize = view.getUint32(p + 20, true);
    const nameLen = view.getUint16(p + 28, true);
    const extraLen = view.getUint16(p + 30, true);
    const commentLen = view.getUint16(p + 32, true);
    const localOffset = view.getUint32(p + 42, true);
    const name = new TextDecoder().decode(
      new Uint8Array(buffer, p + 46, nameLen),
    );
    p += 46 + nameLen + extraLen + commentLen;

    if (name.endsWith("/") || !filter(name)) continue;

    // The local header repeats the name and extra fields, and its extra length
    // can differ from the central one — always read it from the local header.
    const lNameLen = view.getUint16(localOffset + 26, true);
    const lExtraLen = view.getUint16(localOffset + 28, true);
    const start = localOffset + 30 + lNameLen + lExtraLen;
    const bytes = new Uint8Array(buffer, start, compressedSize);

    let text;
    if (method === 0) {
      text = new TextDecoder().decode(bytes);
    } else if (method === 8) {
      text = await inflateRaw(bytes);
    } else {
      continue; // an Exportify zip only ever uses stored or deflate
    }
    out.push({ name, text });
  }
  return out;
}

async function inflateRaw(bytes) {
  if (typeof DecompressionStream === "undefined") {
    throw new Error(
      "This browser can't unpack ZIP files. Unzip it yourself and drop the CSV.",
    );
  }
  const stream = new Blob([bytes])
    .stream()
    .pipeThrough(new DecompressionStream("deflate-raw"));
  return new Response(stream).text();
}

/** Scan backwards for the End Of Central Directory record. */
function findEocd(view) {
  // 22 bytes minimum, plus up to 64KB of trailing comment.
  const min = Math.max(0, view.byteLength - 22 - 0xffff);
  for (let i = view.byteLength - 22; i >= min; i -= 1) {
    if (view.getUint32(i, true) === EOCD_SIG) return i;
  }
  return -1;
}
