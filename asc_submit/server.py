"""Read-only browser view over the run journal — GitHub Actions style.

``asc-submit serve`` starts a stdlib HTTP server (no dependencies) that
serves a small dashboard plus the JSON/plain-text API it renders from:

    GET /                     dashboard (auto-selects the newest run)
    GET /runs/<id>            same page, one run preselected
    GET /api/runs             JSON list of run summaries, newest first
    GET /api/runs/<id>        JSON run detail: status, timing, steps
    GET /api/runs/<id>/log    log slice, character offsets: ?from=N[&to=M]

The browser only watches: workflows are started from the CLI as always, and
the same data is reachable without a browser via ``asc-submit runs`` /
``asc-submit logs`` (or curl against the endpoints above). The server binds
127.0.0.1 by default and writes nothing.
"""

from __future__ import annotations

import json
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from . import journal

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8756


class _Handler(BaseHTTPRequestHandler):
    runs_dir: Path = journal.resolve_runs_dir()

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        parts = [unquote(p) for p in urlsplit(self.path).path.split("/") if p]
        query = {k: v[-1] for k, v in parse_qs(urlsplit(self.path).query).items()}

        if parts in ([], ["runs"]) or (len(parts) == 2 and parts[0] == "runs"):
            self._html(PAGE)
        elif parts == ["api", "runs"]:
            self._json({"runs": journal.list_runs(self.runs_dir)})
        elif len(parts) == 3 and parts[:2] == ["api", "runs"]:
            state = journal.read_run(parts[2], self.runs_dir)
            if state is None:
                self._json({"error": f"unknown run: {parts[2]}"}, 404)
            else:
                self._json(state)
        elif len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "log":
            run_id = parts[2]
            if journal.read_run(run_id, self.runs_dir) is None:
                self._json({"error": f"unknown run: {run_id}"}, 404)
                return
            try:
                start = int(query.get("from", 0))
                end = query.get("to")
                end = int(end) if end is not None else None
            except ValueError:
                self._json({"error": "from/to must be integers"}, 400)
                return
            text, next_offset, size = journal.read_log(run_id, self.runs_dir, start, end)
            self._json({"text": text, "next": next_offset, "size": size})
        else:
            self._json({"error": "not found"}, 404)

    # -- responses ----------------------------------------------------------

    def _send(self, body: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj: dict, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._send(body, "application/json; charset=utf-8", status)

    def _html(self, page: str, status: int = 200) -> None:
        self._send(page.encode("utf-8"), "text/html; charset=utf-8", status)

    def log_message(self, fmt: str, *args) -> None:
        # Poll traffic is one request per second per viewer; stay quiet.
        pass


def make_server(
    runs_dir: str | None = None,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), _Handler)
    server.daemon_threads = True
    _Handler.runs_dir = journal.resolve_runs_dir(runs_dir)
    return server


def serve(
    runs_dir: str | None = None,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    open_browser: bool = False,
) -> None:
    server = make_server(runs_dir, host, port)
    url = f"http://{server.server_address[0]}:{server.server_address[1]}/"
    print(f"asc-submit workflows → {url}")
    print(f"watching {journal.resolve_runs_dir(runs_dir)} (read-only; runs are recorded by the CLI)")
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


# ---------------------------------------------------------------------------
# dashboard — one static page, no external assets


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>asc-submit · workflows</title>
<link rel="icon" href="data:,">
<style>
  :root {
    --bg: #0d1117; --panel: #161b22; --border: #30363d;
    --text: #e6edf3; --muted: #8b949e;
    --green: #3fb950; --red: #f85149; --yellow: #d29922; --blue: #58a6ff;
    --mono: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--bg); color: var(--text);
         font: 14px/1.5 -apple-system, "Segoe UI", Helvetica, Arial, sans-serif; }
  header { display: flex; align-items: center; gap: 10px; padding: 10px 16px;
           border-bottom: 1px solid var(--border); background: var(--panel); }
  header .logo { width: 10px; height: 10px; border-radius: 50%;
                 background: var(--green); box-shadow: 0 0 8px var(--green); }
  header h1 { font-size: 14px; margin: 0; font-weight: 600; }
  header .where { color: var(--muted); font-family: var(--mono); font-size: 12px; }
  #app { display: flex; min-height: calc(100vh - 41px); }

  /* sidebar */
  #sidebar { width: 300px; min-width: 220px; border-right: 1px solid var(--border);
             overflow-y: auto; padding: 8px; }
  .run-item { padding: 8px 10px; border-radius: 6px; cursor: pointer;
              border-left: 3px solid transparent; margin-bottom: 4px; }
  .run-item:hover { background: #1c2129; }
  .run-item.selected { background: #1c2129; border-left-color: var(--blue); }
  .run-item .t { display: flex; gap: 7px; align-items: center; white-space: nowrap;
                 overflow: hidden; text-overflow: ellipsis; }
  .run-item .t .name { overflow: hidden; text-overflow: ellipsis; }
  .run-item .s { color: var(--muted); font-size: 12px; margin-left: 17px;
                 white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .dot { width: 8px; height: 8px; border-radius: 50%; flex: none; }
  .dot.success { background: var(--green); }
  .dot.failed { background: var(--red); }
  .dot.cancelled { background: var(--muted); }
  .dot.running { background: var(--yellow); animation: pulse 1.2s ease-in-out infinite; }

  /* main */
  #main { flex: 1; padding: 18px 22px; overflow-y: auto; }
  .head h2 { margin: 0 0 6px; font-size: 18px; }
  .badges { display: flex; flex-wrap: wrap; gap: 8px; margin: 8px 0 14px; }
  .badge { border: 1px solid var(--border); border-radius: 999px; padding: 2px 10px;
           font-size: 12px; color: var(--muted); background: var(--panel); }
  .badge.status-success { color: var(--green); border-color: var(--green); }
  .badge.status-failed { color: var(--red); border-color: var(--red); }
  .badge.status-running { color: var(--yellow); border-color: var(--yellow); }
  .badge.status-cancelled { color: var(--muted); }
  .badge.mono { font-family: var(--mono); }
  .error-box { border: 1px solid var(--red); background: rgba(248,81,73,.08);
               color: #ffb3ad; border-radius: 6px; padding: 10px 12px; margin: 10px 0;
               font-family: var(--mono); font-size: 13px; white-space: pre-wrap; }

  /* steps */
  .step { border: 1px solid var(--border); border-radius: 6px; margin-bottom: 8px;
          background: var(--panel); }
  .step-head { display: flex; align-items: center; gap: 10px; width: 100%;
               padding: 9px 12px; background: none; border: none; color: var(--text);
               font: inherit; text-align: left; cursor: pointer; }
  .step-head:hover { background: #1c2129; border-radius: 6px; }
  .sic { font-family: var(--mono); width: 16px; text-align: center; flex: none; }
  .sic.success { color: var(--green); }
  .sic.failed { color: var(--red); }
  .sic.cancelled { color: var(--muted); }
  .sic.running { color: var(--yellow); animation: pulse 1.2s ease-in-out infinite; }
  .sname { flex: 1; }
  .sdur { color: var(--muted); font-family: var(--mono); font-size: 12px; }
  .chev { color: var(--muted); transition: transform .15s; }
  .step.open .chev { transform: rotate(90deg); }
  .step-log { display: none; margin: 0; padding: 10px 14px; border-top: 1px solid var(--border);
              font-family: var(--mono); font-size: 12.5px; line-height: 1.55;
              white-space: pre-wrap; word-break: break-word; color: #c9d1d9;
              max-height: 340px; overflow-y: auto; }
  .step.open .step-log { display: block; }
  .step.open .step-log:empty::after { content: "（このステップにはログがありません）"; color: var(--muted); }
  .step.open.running .step-log:empty::after { content: "ログを待っています…"; }

  .empty { color: var(--muted); text-align: center; margin-top: 15vh; }
  .empty code { font-family: var(--mono); background: var(--panel);
                border: 1px solid var(--border); border-radius: 4px; padding: 2px 6px; }
  @keyframes pulse { 50% { opacity: .35; } }
  @media (max-width: 760px) { #app { flex-direction: column; }
    #sidebar { width: auto; max-height: 200px; border-right: none;
               border-bottom: 1px solid var(--border); } }
</style>
</head>
<body>
<header>
  <div class="logo"></div>
  <h1>asc-submit workflows</h1>
  <span class="where" id="where"></span>
</header>
<div id="app">
  <nav id="sidebar"></nav>
  <main id="main"><div class="empty">読み込み中…</div></main>
</div>
<script>
"use strict";
const ICONS = {success: "✓", failed: "✕", running: "●", cancelled: "⊘"};
let runs = [], detail = null;
let selected = decodeURIComponent(location.pathname.split("/")[2] || "") || null;
const expanded = {};   // step index -> true
const logCache = {};   // step index -> {text, to}

const $ = (sel) => document.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

async function jget(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(url + " → " + res.status);
  return res.json();
}

function parseISO(s) { return s ? new Date(s) : null; }
function durText(sec) {
  if (sec == null) return "";
  if (sec < 60) return sec + "s";
  if (sec < 3600) return Math.floor(sec / 60) + "m " + (sec % 60) + "s";
  return Math.floor(sec / 3600) + "h " + Math.floor((sec % 3600) / 60) + "m";
}
function agoText(iso) {
  const t = parseISO(iso); if (!t) return "";
  const sec = Math.max(0, Math.round((Date.now() - t.getTime()) / 1000));
  if (sec < 60) return "just now";
  if (sec < 3600) return Math.floor(sec / 60) + "m ago";
  if (sec < 86400) return Math.floor(sec / 3600) + "h ago";
  return t.toLocaleDateString();
}
function stepSeconds(step) {
  const a = parseISO(step.started_at), b = parseISO(step.finished_at);
  if (!a) return null;
  return Math.max(0, Math.round(((b || new Date()) - a) / 1000));
}

function select(id) {
  if (selected === id) return;
  selected = id;
  detail = null;
  for (const k of Object.keys(logCache)) delete logCache[k];
  history.replaceState(null, "", id ? "/runs/" + encodeURIComponent(id) : "/");
  render();
  tick();
}

async function fetchStepLogs() {
  await Promise.all(detail.steps.map(async (step, i) => {
    if (!expanded[i]) return;
    const open = step.status === "running";
    const to = open ? null : step.log_end;
    const cached = logCache[i];
    if (cached && !open && cached.to === to) return;   // finished and unchanged
    const q = new URLSearchParams({from: step.log_start});
    if (!open) q.set("to", step.log_end);
    try {
      const r = await jget(`/api/runs/${encodeURIComponent(detail.id)}/log?` + q);
      logCache[i] = {text: r.text, to: open ? null : to};
    } catch (e) { /* transient — next tick retries */ }
  }));
}

async function tick() {
  try { runs = (await jget("/api/runs")).runs; } catch (e) { /* server restarting? */ }
  if (!selected && runs.length) { selected = runs[0].id; }
  if (selected) {
    try { detail = await jget("/api/runs/" + encodeURIComponent(selected)); }
    catch (e) { detail = null; }
    if (detail) {
      // auto-open the step that is executing right now
      detail.steps.forEach((step, i) => { if (step.status === "running") expanded[i] = true; });
      await fetchStepLogs();
    }
  }
  render();
}

function renderSidebar() {
  const el = $("#sidebar");
  if (!runs.length) {
    el.innerHTML = '<div class="empty" style="margin-top:24px">まだ実行はありません<br><small><code>asc-submit run …</code> がここに現れます</small></div>';
    return;
  }
  el.innerHTML = runs.map((r) => `
    <div class="run-item ${r.id === selected ? "selected" : ""}" data-id="${esc(r.id)}">
      <div class="t"><span class="dot ${r.status}"></span>
        <span class="name">${esc(r.title || r.id)}</span></div>
      <div class="s">${esc(r.command)} · ${agoText(r.started_at)}${r.status === "running" ? "" : " · " + durText(r.duration_seconds)}</div>
    </div>`).join("");
  el.querySelectorAll(".run-item").forEach((node) =>
    node.addEventListener("click", () => select(node.dataset.id)));
}

function renderMain() {
  const el = $("#main");
  if (!detail) {
    el.innerHTML = runs.length
      ? '<div class="empty">実行を選択してください</div>'
      : `<div class="empty">ワークフローの実行がまだありません。<br><br>
         <code>asc-submit run &lt;app&gt; --spec release.json --submit --yes</code><br>
         実行中の様子がこの画面にリアルタイムで表示されます。</div>`;
    return;
  }
  const chips = Object.entries(detail.meta || {})
    .filter(([, v]) => v != null && v !== "")
    .map(([k, v]) => `<span class="badge mono">${esc(k)}: ${esc(v)}</span>`)
    .join("");
  const steps = detail.steps.map((step, i) => `
    <div class="step ${expanded[i] ? "open" : ""} ${step.status}" data-i="${i}">
      <button class="step-head">
        <span class="chev">▸</span>
        <span class="sic ${step.status}">${ICONS[step.status] || "○"}</span>
        <span class="sname">${esc(step.name)}</span>
        ${step.error ? "" : `<span class="sdur">${durText(stepSeconds(step))}</span>`}
      </button>
      <pre class="step-log">${esc((logCache[i] || {}).text || "")}</pre>
    </div>`).join("");
  el.innerHTML = `
    <div class="head">
      <h2>${esc(detail.title || detail.id)}</h2>
      <div class="badges">
        <span class="badge status-${detail.status}">${ICONS[detail.status] || ""} ${esc(detail.status)}</span>
        <span class="badge mono">${esc(detail.id)}</span>
        <span class="badge">${esc(detail.command)}</span>
        ${chips}
        <span class="badge">started ${agoText(detail.started_at)}</span>
        <span class="badge">${durText(detail.duration_seconds) || "…"}</span>
      </div>
      ${detail.error ? `<div class="error-box">${esc(detail.error)}</div>` : ""}
      <div id="steps">${steps || '<div class="empty">ステップなし</div>'}</div>
    </div>`;
  el.querySelectorAll(".step-head").forEach((node) =>
    node.addEventListener("click", () => {
      const i = node.parentElement.dataset.i;
      expanded[i] = !expanded[i];
      renderMain();
      if (expanded[i]) fetchStepLogs().then(renderMain);
    }));
  // keep the live step's log pinned to the bottom while it runs
  const live = el.querySelector(".step.running .step-log");
  if (live) live.scrollTop = live.scrollHeight;
}

function render() {
  $("#where").textContent = location.host;
  renderSidebar();
  renderMain();
}

render();
tick();
setInterval(tick, 1000);
</script>
</body>
</html>
"""
