# NetOps troubleshooting agent — React UI

The same agent as `netops`, with the Chainlit UI replaced by **React + FastAPI
over one WebSocket**, and one new capability: when the **source is not in the
CMDB**, the agent probes with ping/traceroute **from the machine it runs on**
instead of SSHing anywhere — and skips the Tufin lookup, since there is no
topology to ask about. The report says plainly that reachability was tested
from the agent host, not the real source.

The agent itself — the LangGraph graph, the guards, the three privacy layers,
the clipboard relay — is unchanged from `netops`.

## Layout

```
netops-react/
├── run.ps1                     start it            .\run.ps1 -Mock
├── requirements.txt            backend deps (no chainlit)
│
├── agent/                      the agent — same as netops, plus:
│   ├── prompts.py              LOCAL FALLBACK routing when the CMDB misses
│   └── vendors.py              parses Windows ping/tracert output too
│
├── mcp_tools/
│   ├── unicorn_mcp.py          get_device_details        (CMDB)
│   ├── tufin_mcp.py            get_firewall_path         (SecureTrack)
│   ├── troubleshoot_agent_mcp.py  execute_query_on_server (SSH via bastion)
│   └── local_probe_mcp.py      local_ping / local_traceroute  (NEW —
│                               subprocess on the agent host; args are a
│                               validated IP only, nothing to inject)
│
├── api/
│   ├── main.py                 the REST endpoints + the SSE stream
│   ├── models.py               Pydantic schemas -- these ARE the Swagger page
│   ├── runs.py                 run store + the loop that drives the graph
│   └── workflow.py             panel state, ported from the Chainlit UI
│
├── frontend/                   Vite + React (no UI framework)
│   └── src/api.js · App.jsx · Console.jsx · ChatPanel.jsx · styles.css
│                               agent console, not a chat: command bar,
│                               stage strip, compact activity feed (reasoning
│                               and raw output behind toggles), tabbed report
│                               (Report / Path / Deep), optional chat drawer.
│                               Light/dark theme button, persisted.
│                               api.js is the ONLY place a URL appears.
│
└── tests/                      same suite as netops, plus test_local_probe.py
```

## Setup

```powershell
cd netops-react
uv venv --python 3.12
uv pip install -r requirements.txt
cd frontend; npm install; npm run build; cd ..
Copy-Item ..\netops\credentials.yml .    # or fill credentials.example.yml
```

`.env` carries the same switches as netops (`LLM_MODE`, `MASK_IPS`,
`MASK_NAMES`, `SSH_MCP_URL`, …). `CHAT_HISTORY` is gone — this UI is
deliberately single-session.

## Run

```powershell
.\run.ps1 -Mock            # mock CMDB, devices, Tufin AND local probes
.\run.ps1                  # the real office MCPs + real local ping/tracert
.\run.ps1 -Dev             # + Vite dev server on :5173 with hot reload
```

Open http://localhost:8000. The frontend is served by FastAPI from
`frontend/dist`, so in normal use only one process runs.

## The API

Everything the UI does, it does over documented HTTP. **Swagger: http://localhost:8000/docs**

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/health` | Backend mode, mocks, masking, active run |
| `POST` | `/api/runs` | Start a run → `202` with a `run_id` |
| `GET` | `/api/runs` | Recent runs |
| `GET` | `/api/runs/{id}` | One run in full: progress, commands, verdict |
| `GET` | `/api/runs/{id}/events` | SSE — the whole run object on every change |
| `POST` | `/api/runs/{id}/approvals/{aid}` | Approve or reject the parked command |
| `POST` | `/api/runs/{id}/deep` | Run the deeper diagnostics |
| `POST` | `/api/ask` | A question, answered from run context |
| `GET` | `/api/devices/{name}` | Direct CMDB lookup |

A run executes in the background and is addressed by its id, so the browser can
be closed and reopened mid-run, and any other system can drive the agent over
plain HTTP. One run at a time (the clipboard relay is a single global
resource); starting a second returns `409`.

Drive one from the shell:

```powershell
$run = irm -Method POST http://localhost:8000/api/runs -ContentType application/json `
  -Body '{"source":"10.10.1.20","destination":"172.20.5.10","protocol":"TCP","port":"443"}'
irm "http://localhost:8000/api/runs/$($run.id)"        # poll; approve when it parks
```

**How it fits together — see [ARCHITECTURE.md](ARCHITECTURE.md)** for the whole
application end to end: the graph, the guards, the three masking layers, the run
store, and the frontend's state flow.

## The local fallback

CMDB knows neither address → the model is told (prompt routing rule) to use
`local_ping` / `local_traceroute` and to **skip SSH and Tufin**:

- The tools take an **IP address argument only**, validated with `ipaddress`;
  the command line is assembled in code (`ping -n 3 -w 1000 <ip>` /
  `tracert -d -h 5 -w 1000 <ip>` on Windows). The model never writes a
  command string, so the read-only guard has nothing to reject and there is
  no injection surface.
- They are still **approval-gated** like every other probe (the `local`
  server is in `DEVICE_SERVERS`), and audited as `agent host`.
- The report and the panel carry a visible warning that the probe ran from
  the agent machine, so a clean ping does **not** claim the real source can
  reach the destination.
- Source in CMDB but destination missing → normal workflow: the source
  device can ping whatever address it is given.

## Safety — unchanged from netops

Read-only allowlist in code, human approval on every device command, Tufin
ungated (read-only GET), credential redaction at the MCP boundary, reversible
IP/name masking for the clipboard relay.

## Tests

```powershell
.venv\Scripts\python.exe tests\test_local_probe.py    # NEW: windows parsing + gating
.venv\Scripts\python.exe tests\test_flow.py           # whole graph (set USE_MOCKS=1)
.venv\Scripts\python.exe tests\test_ip_mask.py
.venv\Scripts\python.exe tests\test_entities.py
.venv\Scripts\python.exe tests\test_redact.py
.venv\Scripts\python.exe tests\test_relay_mask.py
.venv\Scripts\python.exe tests\test_relay_delta.py
.venv\Scripts\python.exe tests\test_reply_parsing.py
```

UI run with no Copilot and no devices:

```powershell
.venv\Scripts\python.exe tests\fake_llm.py 11499
$env:LLM_MODE="api"; $env:LLM_BASE_URL="http://127.0.0.1:11499/v1"; .\run.ps1 -Mock
```

## Copilot agent instructions

The tool list grew (`local_ping`, `local_traceroute`), so if you use
`CLIP_MODE=agent`, regenerate and re-paste the instructions:

```powershell
$env:USE_MOCKS="1"; .venv\Scripts\python.exe -m agent.llm.clipboard_llm
```
