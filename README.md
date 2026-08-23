# NetOps troubleshooting agent — React UI

The same agent as `netops`, with the Chainlit UI replaced by **React + FastAPI
over one WebSocket**, and one behaviour of its own: when the **source is not in
the CMDB** there is no device to log into, so the run skips ping and traceroute
and reports the **firewall verdict alone**, saying plainly that nothing was
tested from the source itself.

The agent itself — the LangGraph graph, the guards, the three privacy layers,
the clipboard relay — is unchanged from `netops`.

## Layout

```
netops-react/
├── run.ps1                     start it            .\run.ps1 -Mock
├── requirements.txt            backend deps (no chainlit)
│
├── agent/                      the agent — same as netops, plus:
│   ├── prompts.py              CMDB-miss routing: skip to the policy check
│   ├── llm/masked_llm.py       masks prompts for an API model, not just the relay
│   └── vendors.py              parses Windows ping/tracert output too
│
├── mcp_tools/
│   ├── unicorn_mcp.py          get_device_details        (CMDB)
│   ├── tufin_mcp.py            get_firewall_path         (SecureTrack)
│   ├── troubleshoot_agent_mcp.py  execute_query_on_server (SSH via bastion)
│   └── local_probe_mcp.py      local_ping / local_traceroute — OFF unless
│                               LOCAL_PROBES=1; args are a validated IP only
│
├── api/
│   ├── main.py                 FastAPI + the /ws WebSocket protocol
│   └── workflow.py             panel state, ported from the Chainlit UI
│
├── frontend/                   Vite + React (no UI framework)
│   └── src/App.jsx · Console.jsx · ChatPanel.jsx · styles.css
│                               agent console, not a chat: command bar, stage
│                               strip, collapsible activity feed, tabbed report
│                               (Report / Path / Deep), optional chat drawer.
│                               Light/dark theme button, persisted.
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

## The WebSocket protocol

One socket per tab: `/ws`. The client sends
`{"type":"chat","text":…}`, `{"type":"deep_check"}` and
`{"type":"approval","id":…,"approved":…}`; the server streams `thought`,
`tool_result`, `approval_request`, `workflow` (full panel snapshot), `step`,
`final`, `status` and `error` frames. No polling anywhere — the flicker and
lag of the Chainlit sidebar (each update re-mounted the component) is gone
by construction.

## When the CMDB has no record

Ping and traceroute only mean anything **run on the source device**, and
reaching it needs the management address and region that the CMDB record
carries. No record → no device to SSH to. So the agent:

- runs **no device command** — nothing to run it on;
- does **not** substitute a probe from the agent machine. A ping from here
  answers "can this box reach it", which is a different question and reads
  like an answer to the one that was asked;
- **does** call Tufin. `get_firewall_path` works from the two addresses alone
  — it models the topology and the policy and needs no CMDB record — so the
  firewall verdict is still available, and is the whole of what can be
  established.

The report says so plainly: ping `NOT RUN`, source `<ip> (not in CMDB)`, the
verdict taken from the policy result, and a next step of adding the device to
the CMDB. The panel shows the CMDB stage failed, with a note that the policy
check is the only one available.

Only the DESTINATION missing → normal workflow: the source device can ping
whatever address it is given.

### Probing from the agent machine

`mcp_tools/local_probe_mcp.py` (`local_ping`, `local_traceroute`) still exists
but is **off by default**, because the reading above is what it produces:
evidence about this host presented as evidence about the source. Set
`LOCAL_PROBES=1` to offer the tools when that is genuinely what you want —
they take a validated IP argument only, the command line is assembled in code,
and they stay approval-gated and audited as `agent host`.

## Safety — unchanged from netops

Read-only allowlist in code, human approval on every device command, Tufin
ungated (read-only GET), credential redaction at the MCP boundary, reversible
IP/name masking for the clipboard relay.

## Tests

```powershell
.venv\Scripts\python.exe tests\test_guards.py         # read-only enforcement
.venv\Scripts\python.exe tests\test_flow.py           # whole graph (set USE_MOCKS=1)
.venv\Scripts\python.exe tests\test_command_status.py # refusal vs result vs silence
.venv\Scripts\python.exe tests\test_tufin_shape.py    # SecureTrack reply shapes
.venv\Scripts\python.exe tests\test_cmdb_record.py    # found vs not-found
.venv\Scripts\python.exe tests\test_api_mask.py       # masking through an API model
.venv\Scripts\python.exe tests\test_local_probe.py    # the optional local probes
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

They are a COPY pasted into a custom Copilot agent and do not follow the code,
so if you use `CLIP_MODE=agent`, regenerate and re-paste after any prompt or
tool change:

```powershell
$env:USE_MOCKS="1"; .venv\Scripts\python.exe -m agent.llm.clipboard_llm
```
