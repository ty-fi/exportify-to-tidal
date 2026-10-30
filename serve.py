#!/usr/bin/env python3
"""
Local drop-page for exportify_to_tidal. Started with `--serve`.

Removes the two tedious steps of a run: moving the download into
exportify-exports/, and remembering the command. Drop the CSV (or the whole
Export All zip) on the page, preview the matches, then create the playlists.

Deliberately stdlib-only -- `tidalapi` stays the single dependency. That rules
out a web framework, so this is http.server plus a page served as a string.

Binds to 127.0.0.1. There is no auth, because the Tidal session already lives on
this machine and anything that can reach this port can already read it.
"""

from __future__ import annotations

import copy
import io
import json
import threading
import webbrowser
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from exportify_to_tidal import (
    discover,
    load_ledger,
    load_session,
    playlist_name_from_filename,
    run_queue,
)

# One job at a time. The Tidal session isn't safe to drive concurrently, and a
# second batch running over the first would scramble the ledger.
_LOCK = threading.Lock()
JOB: dict = {
    "state": "idle",  # idle | running | done | error
    "dry_run": True,
    "log": [],
    "ported": [],
    "failed": [],
    "done": 0,
    "total": 0,
}


class _LogWriter(io.TextIOBase):
    """Collects the porter's stdout into the job log, a line at a time.

    Capturing print() this way means the matching code needs no changes and the
    page shows exactly what the CLI would have shown.
    """

    def __init__(self) -> None:
        self._buf = ""

    def write(self, s: str) -> int:
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            with _LOCK:
                JOB["log"].append(line)
        return len(s)

    def flush(self) -> None:
        if self._buf:
            with _LOCK:
                JOB["log"].append(self._buf)
            self._buf = ""


def safe_name(raw: str) -> str:
    """Filename only -- never a path. Stops an upload escaping the exports dir."""
    name = Path(raw.replace("\\", "/")).name.strip()
    if not name or name.startswith("."):
        raise ValueError(f"unusable filename: {raw!r}")
    return name


def save_upload(exports_dir: Path, filename: str, body: bytes) -> list[str]:
    """Write an uploaded CSV, or unpack the CSVs out of an Export All zip."""
    exports_dir.mkdir(parents=True, exist_ok=True)
    name = safe_name(filename)

    if name.lower().endswith(".zip"):
        written = []
        with zipfile.ZipFile(io.BytesIO(body)) as zf:
            for member in zf.namelist():
                if not member.lower().endswith(".csv") or member.endswith("/"):
                    continue
                inner = safe_name(member)
                if inner.endswith("_unmatched.csv"):
                    continue  # our own output, if a previous run's dir got zipped
                (exports_dir / inner).write_bytes(zf.read(member))
                written.append(inner)
        if not written:
            raise ValueError("no CSV files inside that zip")
        return written

    if not name.lower().endswith(".csv"):
        raise ValueError(f"not a CSV or zip: {name}")
    (exports_dir / name).write_bytes(body)
    return [name]


def queue_state(args) -> dict:
    ledger = load_ledger()
    queue, skipped = discover(args.exports_dir, ledger, args.reprocess)
    return {
        "queue": [
            {"file": p.name, "name": playlist_name_from_filename(p)} for p in queue
        ],
        "skipped": [{"file": p.name, "reason": r} for p, r in skipped],
    }


def _run(args, dry_run: bool) -> None:
    job_args = copy.copy(args)
    job_args.dry_run = dry_run

    ledger = load_ledger()
    queue, _ = discover(job_args.exports_dir, ledger, job_args.reprocess)

    with _LOCK:
        JOB.update(
            state="running", dry_run=dry_run, log=[], ported=[], failed=[],
            done=0, total=len(queue),
        )

    writer = _LogWriter()
    try:
        import contextlib

        with contextlib.redirect_stdout(writer):
            session = load_session()

            def on_each(path, entry, exc):
                with _LOCK:
                    JOB["done"] += 1
                    if entry:
                        JOB["ported"].append(entry)
                    elif exc:
                        JOB["failed"].append({"file": path.name, "error": repr(exc)})

            run_queue(queue, session, job_args, ledger, on_each=on_each)
        writer.flush()
        with _LOCK:
            JOB["state"] = "done"
    except Exception as exc:  # noqa: BLE001 - surfaced to the page, not swallowed
        writer.flush()
        with _LOCK:
            JOB["state"] = "error"
            JOB["log"].append(f"! {exc!r}")


def make_handler(args):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a) -> None:
            pass  # the porter's own output is the interesting stream

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code: int = 200) -> None:
            self._send(code, json.dumps(obj).encode("utf-8"), "application/json")

        def do_GET(self) -> None:
            if self.path == "/":
                self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
            elif self.path == "/api/queue":
                self._json(queue_state(args))
            elif self.path == "/api/status":
                with _LOCK:
                    self._json(dict(JOB))
            else:
                self._json({"error": "not found"}, 404)

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""

            if self.path == "/api/upload":
                try:
                    written = save_upload(
                        args.exports_dir, self.headers.get("X-Filename", ""), body
                    )
                except Exception as exc:  # noqa: BLE001
                    self._json({"error": str(exc)}, 400)
                    return
                self._json({"written": written, **queue_state(args)})

            elif self.path == "/api/run":
                with _LOCK:
                    if JOB["state"] == "running":
                        self._json({"error": "a run is already in progress"}, 409)
                        return
                dry = True
                try:
                    dry = bool(json.loads(body or b"{}").get("dry_run", True))
                except Exception:  # noqa: BLE001
                    pass
                threading.Thread(target=_run, args=(args, dry), daemon=True).start()
                self._json({"started": True, "dry_run": dry})
            else:
                self._json({"error": "not found"}, 404)

    return Handler


def serve(args) -> None:
    args.exports_dir.mkdir(parents=True, exist_ok=True)

    # Log in before serving so the device-code link lands in the terminal, where
    # the person starting the server is already looking.
    print("Checking the Tidal session before starting the server...\n")
    load_session()

    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(args))
    url = f"http://127.0.0.1:{args.port}/"
    print(f"\nDrop page running at {url}")
    print(f"Watching {args.exports_dir.resolve()}")
    print("Ctrl-C to stop.\n")
    try:
        webbrowser.open(url)
    except Exception:  # noqa: BLE001 - headless is fine, the URL is printed
        pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        httpd.server_close()


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Exportify to Tidal</title>
<style>
  :root {
    --bg: #fbfaf8; --fg: #1b1b19; --muted: #6b6a66; --line: #e0ddd6;
    --card: #ffffff; --accent: #1f6f5c; --accent-fg: #ffffff;
    --warn: #8a5a00; --err: #9b2c2c; --ok: #1f6f5c;
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      --bg: #16161a; --fg: #ececea; --muted: #9b9a95; --line: #2e2e34;
      --card: #1e1e23; --accent: #4fae93; --accent-fg: #10100f;
      --warn: #d9a441; --err: #e2817c; --ok: #4fae93;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 32px 16px 64px; background: var(--bg); color: var(--fg);
    font: 15px/1.55 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
  }
  main { max-width: 760px; margin: 0 auto; }
  h1 { font-size: 20px; margin: 0 0 4px; letter-spacing: -0.01em; }
  .sub { color: var(--muted); margin: 0 0 24px; font-size: 14px; }
  #drop {
    border: 1.5px dashed var(--line); border-radius: 10px; padding: 36px 20px;
    text-align: center; color: var(--muted); background: var(--card);
    transition: border-color .15s, color .15s; cursor: pointer;
  }
  #drop.over { border-color: var(--accent); color: var(--fg); }
  #drop strong { color: var(--fg); }
  section { margin-top: 24px; }
  h2 { font-size: 13px; text-transform: uppercase; letter-spacing: .07em;
       color: var(--muted); margin: 0 0 10px; font-weight: 600; }
  ul { list-style: none; margin: 0; padding: 0; }
  li { padding: 9px 12px; border: 1px solid var(--line); border-radius: 8px;
       margin-bottom: 6px; background: var(--card); display: flex;
       justify-content: space-between; gap: 12px; align-items: baseline; }
  li .file { color: var(--muted); font-size: 13px;
             font-family: ui-monospace, Menlo, Consolas, monospace; }
  .reason { color: var(--muted); font-size: 13px; }
  .row { display: flex; gap: 10px; margin-top: 20px; flex-wrap: wrap; }
  button {
    font: inherit; padding: 9px 16px; border-radius: 8px; cursor: pointer;
    border: 1px solid var(--line); background: var(--card); color: var(--fg);
  }
  button.primary { background: var(--accent); color: var(--accent-fg);
                   border-color: transparent; font-weight: 600; }
  button:disabled { opacity: .45; cursor: default; }
  pre {
    background: var(--card); border: 1px solid var(--line); border-radius: 8px;
    padding: 14px; overflow-x: auto; font-size: 12.5px; line-height: 1.5;
    font-family: ui-monospace, Menlo, Consolas, monospace; max-height: 420px;
    white-space: pre-wrap; word-break: break-word;
  }
  .empty { color: var(--muted); font-size: 14px; }
  .note { font-size: 13px; color: var(--muted); margin-top: 10px; }
</style>
</head>
<body>
<main>
  <h1>Exportify to Tidal</h1>
  <p class="sub">Drop a CSV or an Export All zip. Nothing reaches Tidal until you say so.</p>

  <div id="drop">
    <strong>Drop files here</strong><br>or click to choose
    <input id="file" type="file" multiple accept=".csv,.zip" hidden>
  </div>

  <section>
    <h2>Ready to port</h2>
    <ul id="queue"></ul>
    <p id="queue-empty" class="empty">Nothing pending.</p>
  </section>

  <section id="skipped-wrap" hidden>
    <h2>Skipped</h2>
    <ul id="skipped"></ul>
  </section>

  <div class="row">
    <button id="preview">Preview matches</button>
    <button id="create" class="primary" disabled>Create in Tidal</button>
  </div>
  <p class="note" id="note">Preview runs the full match without writing anything.</p>

  <section id="log-wrap" hidden>
    <h2>Output</h2>
    <pre id="log"></pre>
  </section>
</main>
<script>
const $ = id => document.getElementById(id);
let polling = null;

async function refresh() {
  const r = await fetch('/api/queue');
  render(await r.json());
}

function render(s) {
  const q = $('queue');
  q.innerHTML = '';
  for (const item of s.queue) {
    const li = document.createElement('li');
    const n = document.createElement('span');
    n.textContent = item.name;
    const f = document.createElement('span');
    f.className = 'file';
    f.textContent = item.file;
    li.append(n, f);
    q.append(li);
  }
  $('queue-empty').hidden = s.queue.length > 0;
  $('preview').disabled = s.queue.length === 0;
  if (s.queue.length === 0) $('create').disabled = true;

  const sk = $('skipped');
  sk.innerHTML = '';
  for (const item of s.skipped) {
    const li = document.createElement('li');
    const n = document.createElement('span');
    n.className = 'file';
    n.textContent = item.file;
    const r = document.createElement('span');
    r.className = 'reason';
    r.textContent = item.reason;
    li.append(n, r);
    sk.append(li);
  }
  $('skipped-wrap').hidden = s.skipped.length === 0;
}

async function upload(files) {
  for (const f of files) {
    const r = await fetch('/api/upload', {
      method: 'POST',
      headers: { 'X-Filename': f.name },
      body: f,
    });
    const j = await r.json();
    if (j.error) { alert(f.name + ': ' + j.error); continue; }
    render(j);
  }
}

const drop = $('drop');
drop.addEventListener('click', () => $('file').click());
$('file').addEventListener('change', e => upload(e.target.files));
['dragenter', 'dragover'].forEach(ev =>
  drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.add('over'); }));
['dragleave', 'drop'].forEach(ev =>
  drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.remove('over'); }));
drop.addEventListener('drop', e => upload(e.dataTransfer.files));

async function run(dryRun) {
  $('preview').disabled = true;
  $('create').disabled = true;
  $('log-wrap').hidden = false;
  $('log').textContent = '';
  await fetch('/api/run', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ dry_run: dryRun }),
  });
  polling = setInterval(poll, 500);
}

async function poll() {
  const s = await (await fetch('/api/status')).json();
  $('log').textContent = s.log.join('\\n');
  $('log').scrollTop = $('log').scrollHeight;
  if (s.state === 'running') {
    $('note').textContent = `Working... ${s.done}/${s.total} playlists.`;
    return;
  }
  clearInterval(polling);
  if (s.state === 'error') {
    $('note').textContent = 'Something failed - see the output above.';
    $('preview').disabled = false;
    return;
  }
  if (s.dry_run) {
    $('note').textContent = 'Preview only. Nothing was written to Tidal.';
    $('create').disabled = false;
    $('preview').disabled = false;
  } else {
    $('note').textContent = `Done. Ported ${s.ported.length} playlist(s).`;
    await refresh();
  }
}

$('preview').addEventListener('click', () => run(true));
$('create').addEventListener('click', () => run(false));
refresh();
</script>
</body>
</html>
"""
