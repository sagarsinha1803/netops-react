"""A plain page for reading the prompt trace, served by the app itself.

Phoenix is a full observability product -- projects, spans, evaluators,
datasets. For "what did the agent send, what came back, and did masking work",
that is a lot of machinery to learn. This is the whole feature in one page:
turn by turn, real and masked side by side, no extra process to run.

    TRACE=file  .\\run.ps1        then open  http://localhost:8000/trace
"""
import json
import os

from fastapi.responses import HTMLResponse, JSONResponse

TRACE_FILE = os.environ.get(
    "TRACE_FILE",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "data", "prompt_trace.jsonl"))


def read_trace(limit: int = 400) -> dict:
    """The trace file as runs. A prompt with a short history starts a new run."""
    if not os.path.exists(TRACE_FILE):
        return {"runs": [], "path": TRACE_FILE, "exists": False}

    rows = []
    with open(TRACE_FILE, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    rows = rows[-limit:]

    runs, current = [], []
    for row in rows:
        if row.get("event") == "prompt" and len(row.get("messages") or []) <= 2:
            if current:
                runs.append(current)
            current = []
        current.append(row)
    if current:
        runs.append(current)

    return {"runs": list(reversed(runs)), "path": TRACE_FILE, "exists": True}


PAGE = """<!doctype html>
<meta charset="utf-8"><title>Prompt trace</title>
<style>
  :root { color-scheme: dark light; }
  body { font: 14px/1.5 "Segoe UI", system-ui, sans-serif; margin: 0;
         background: #0b1220; color: #e2e8f0; }
  header { padding: 12px 20px; background: #101a2e; border-bottom: 1px solid #1e293b;
           display: flex; gap: 14px; align-items: center; position: sticky; top: 0; }
  h1 { font-size: 15px; margin: 0; }
  .path { color: #64748b; font-size: 11.5px; margin-left: auto; }
  main { max-width: 1000px; margin: 0 auto; padding: 18px 20px 60px; }
  .run { border: 1px solid #1e293b; border-radius: 10px; margin-bottom: 18px;
         background: #131f36; overflow: hidden; }
  .run > summary { padding: 11px 15px; cursor: pointer; font-weight: 600; }
  .turn { border-top: 1px solid #1e293b; padding: 12px 15px; }
  .lbl { font-size: 10.5px; letter-spacing: .5px; text-transform: uppercase;
         color: #64748b; margin-bottom: 3px; }
  pre { margin: 0 0 10px; padding: 9px 11px; border-radius: 7px; background: #0a0f1c;
        border: 1px solid #1e293b; white-space: pre-wrap; overflow-wrap: anywhere;
        font: 12px ui-monospace, Consolas, monospace; max-height: 220px; overflow: auto; }
  pre.masked { border-color: #a16207; color: #fcd34d; }
  .reply { border-left: 3px solid #22c55e; padding-left: 11px; }
  .call { font: 12px ui-monospace, Consolas, monospace; color: #38bdf8; margin: 3px 0; }
  .secs { color: #64748b; font-size: 11.5px; }
  .empty { color: #64748b; text-align: center; padding: 50px 20px; }
  .warn { background: #422006; color: #fcd34d; padding: 8px 20px; font-size: 12.5px; }
  @media (prefers-color-scheme: light) {
    body { background: #eef2f7; color: #16233c; }
    header { background: #fff; border-color: #e2e8f0; }
    .run { background: #fff; border-color: #e2e8f0; }
    .turn { border-color: #e2e8f0; }
    pre { background: #f1f5f9; border-color: #e2e8f0; }
  }
</style>
<header>
  <h1>Prompt trace</h1>
  <label><input type="checkbox" id="masked" checked> show what the endpoint received</label>
  <span class="path" id="path"></span>
</header>
<div class="warn">These prompts are UNMASKED — this page is for you, on this
  machine. Do not screenshot it into a ticket.</div>
<main id="out"><div class="empty">loading…</div></main>
<script>
const esc = s => String(s ?? "").replace(/&/g,"&amp;").replace(/</g,"&lt;");

function turn(row, showMasked) {
  if (row.event === "prompt") {
    const msgs = row.messages || [];
    const last = msgs[msgs.length - 1] || {};
    const sent = (row.sent || [])[(row.sent || []).length - 1];
    let html = `<div class="lbl">${esc(row.at)} · ${msgs.length} messages · latest ${esc(last.role)}</div>`;
    html += `<pre>${esc(last.content)}</pre>`;
    if (showMasked && sent)
      html += `<div class="lbl">sent to the endpoint</div>
               <pre class="masked">${esc(sent.content)}</pre>`;
    return html;
  }
  if (row.event === "reply") {
    let html = `<div class="reply"><div class="lbl">reply
      <span class="secs">${row.seconds ?? "?"}s</span></div>`;
    if (row.content) html += `<pre>${esc(row.content)}</pre>`;
    for (const c of row.tool_calls || [])
      html += `<div class="call">→ ${esc(c.name)}(${esc(JSON.stringify(c.args))})</div>`;
    return html + "</div>";
  }
  if (row.event === "error")
    return `<div class="lbl">error</div><pre>${esc(row.error)}</pre>`;
  return "";
}

async function load() {
  const data = await (await fetch("/api/trace")).json();
  document.getElementById("path").textContent = data.path;
  const out = document.getElementById("out");
  if (!data.exists || !data.runs.length) {
    out.innerHTML = `<div class="empty">Nothing recorded yet.<br>
      Start the agent with <code>TRACE=file</code> and run something.</div>`;
    return;
  }
  const showMasked = document.getElementById("masked").checked;
  out.innerHTML = data.runs.map((run, i) => {
    const first = run.find(r => r.event === "prompt") || {};
    const msgs = first.messages || [];
    const title = (msgs[msgs.length - 1] || {}).content || "run";
    const calls = run.flatMap(r => (r.tool_calls || []).map(c => c.name));
    let body = "";
    let n = 0;
    for (const row of run) {
      if (row.event === "prompt") n++;
      body += `<div class="turn">${turn(row, showMasked)}</div>`;
    }
    return `<details class="run" ${i === 0 ? "open" : ""}>
      <summary>${esc(String(title).slice(0, 90))}
        <span class="secs"> — ${n} turns, ${calls.length} tool calls</span></summary>
      ${body}</details>`;
  }).join("");
}
document.getElementById("masked").onchange = load;
load();
setInterval(load, 5000);
</script>
"""


def register(app):
    """Add /trace and /api/trace to the FastAPI app."""

    @app.get("/api/trace", include_in_schema=False)
    async def api_trace():
        return JSONResponse(read_trace())

    @app.get("/trace", include_in_schema=False)
    async def trace_page():
        return HTMLResponse(PAGE)
