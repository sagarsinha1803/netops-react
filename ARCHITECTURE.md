# How this application works

A tour of the whole thing, from the button you press to the command that runs on
a router and back. Read it top to bottom once; after that the section headings
are the map.

---

## 1. The shape of it

```
   BROWSER                    SERVER (one process)                 NETWORK
 ┌───────────┐         ┌──────────────────────────────┐        ┌────────────┐
 │  React    │  HTTP   │  FastAPI          api/       │  stdio │  unicorn   │  CMDB
 │  console  │────────▶│    ├─ REST endpoints         │───────▶│  MCP       │
 │           │         │    └─ SSE stream             │        ├────────────┤
 │           │◀────────│                              │  stdio │  tufin     │  firewall
 └───────────┘  events │  Run store       api/runs.py │───────▶│  MCP       │  policy
                       │    one run = one agent turn  │        ├────────────┤
                       │            │                 │   SSE  │  ssh MCP   │  devices
                       │            ▼                 │───────▶│ via bastion│
                       │  LangGraph agent    agent/   │        ├────────────┤
                       │    ├─ guards.py  (read-only) │  stdio │ local probe│  this host
                       │    ├─ masking    (3 layers)  │───────▶│  MCP       │
                       │    └─ LLM: Copilot relay/API │        └────────────┘
                       └──────────────────────────────┘
```

Four layers, each replaceable without touching the others:

| Layer | Directory | Knows about |
|---|---|---|
| UI | `frontend/` | HTTP endpoints only |
| API | `api/` | Runs, the workflow panel's shape |
| Agent | `agent/` | The LLM, the graph, the guards, masking |
| Tools | `mcp_tools/` | The office systems: CMDB, Tufin, bastions |

The agent has no idea a browser exists. The browser has no idea LangGraph
exists. That boundary is the reason the Chainlit UI could be replaced without
touching a line of `agent/`.

---

## 2. What actually happens when you press Run

Follow one request the whole way down.

**1. The browser POSTs.** `frontend/src/api.js` sends

```http
POST /api/runs   {"source":"10.10.1.20","destination":"172.20.5.10",
                  "protocol":"TCP","port":"443"}
```

**2. The API starts a run and answers immediately** (`api/main.py` →
`api/runs.py: start()`). It returns `202` with a `run_id`. The agent has not
done anything yet. The run executes as an asyncio task, so the HTTP request is
not held open and the browser can be closed without killing it.

**3. The run builds the agent** (`agent/graph.py: build_agent()`): it connects
to the four MCP servers, collects their tools, and binds them to the model. A
server that cannot be reached is recorded and skipped, never fatal — that is
why the app still works from a laptop with no bastion access.

**4. The graph loops.** `START → agent → tools → agent → … → END`

- The **agent node** asks the model what to do next. The model gets the system
  prompt (`agent/prompts.py`), the conversation so far, and the tool schemas.
  It replies with either a tool call or a final answer.
- The **tools node** validates, gets approval, executes, and captures results.

**5. Every device command hits two independent gates** (`agent/graph.py`
`tools_node`, phase 1):

- **`agent/guards.py`** — a read-only allowlist enforced *in code*, not by
  asking the model nicely. `ping`, `traceroute`, `show`/`display` only. Anything
  with `configure`, `write`, `reload`, `clear`… is rejected. Commands that would
  dump a whole table (`show running-config`, bare `show route`) are rejected too
  — harmless to the device, fatal to the context window.
- **Human approval** — the graph raises a LangGraph `interrupt`. The run parks:
  `status` becomes `waiting_approval` and `pending_approval` carries the exact
  command. Nothing runs until `POST /api/runs/{id}/approvals/{aid}` arrives.

  Approvals for a whole batch are collected *before* any command executes, so
  resuming a parked run can never run something twice.

**6. The tool runs**, its output is flattened to text (`agent/utils.py`), and
two things happen to it:
- it goes back to the model as a `ToolMessage`;
- it is parsed *independently of the model* by `agent/vendors.py` — did the ping
  succeed, what hops came back. That is what the UI labels "verified
  independently of the model", and it is why a model that hallucinates a
  successful ping cannot make the panel show green.

**7. The panel state updates.** `api/workflow.py` mirrors everything into the
shape the UI renders: stage statuses, the command rows with their output, the
hop chain, the report. Each change bumps the run's version and wakes the SSE
subscribers.

**8. The model concludes** with a structured verdict — source, destination,
ping, path, result, evidence, cause, next step — and the run's status becomes
`done`.

---

## 3. The agent in detail

### The graph (`agent/graph.py`)

Built by hand: no `create_react_agent`, no `ToolNode`, no `tools_condition`.
That is deliberate — the tool node has to do validation, human approval and
structured capture between deciding on a command and running it, and the
prebuilt node has nowhere to put any of that.

`agent/state.py` is the state passed between nodes: messages, loop count,
device records, the audit trail, `ping_ok`, `hops`, `path`, `answer`.

**Loop budget.** `MAX_TOOL_LOOPS = 12` for a normal run, `DEEP_MAX_LOOPS = 10`
for a deeper-checks turn. The cap travels with the turn, so an escalation gets
its own allowance instead of eating the standard workflow's.

**First one wins.** The ping and traceroute captured into state are the *first*
of a run. The escalation checks ping next hops and trace remote loopbacks —
letting those overwrite the capture would report a dead destination as reachable
and replace the path with the underlay.

### The four request kinds

The system prompt routes every request into one of four, and only one:

| Kind | What runs |
|---|---|
| **A. Device details** | CMDB lookup only |
| **B. Troubleshoot** | The full workflow below |
| **C. Deeper checks** | `execute_query_on_server` alone, one check at a time |
| **D. Anything else** | Answered from the model's own knowledge, no tools |

### The workflow (kind B)

1. **CMDB** — `get_device_details` for source, then destination: vendor, model,
   OS, management IP, region.
2. **Ping** — the model writes the syntax for *that* platform (IOS
   `ping x repeat 3`, NX-OS `count 3`, FortiOS `execute ping`, …).
3. **Traceroute** — always bounded: 5 hops, 1 probe, 1s. Unbounded takes
   minutes.
4. **Firewall policy** — `get_firewall_path` asks Tufin SecureTrack whether the
   traffic is permitted end to end and which rule drops it. Always run, even
   when the ping succeeded: ICMP getting through says nothing about tcp/443.
5. **Stop and report.** The workflow *ends* at Tufin. Deeper checks are extra
   commands on production devices, so they are the user's call.

### The local fallback (no CMDB record)

If the source is not in the CMDB there is no device to SSH to and no region for
the firewall topology. The prompt routes to `local_ping` / `local_traceroute`
(`mcp_tools/local_probe_mcp.py`), which run on *this* machine, and skips SSH and
Tufin entirely.

Those tools take a **validated IP address and nothing else** — the command line
is assembled in code, so the model never writes a command string and there is
nothing to inject. They are still approval-gated, and the report says plainly
that reachability was tested from the agent host, so a clean ping is never
mistaken for proof about the real source.

### The LLM: two backends, one interface

`LLM_MODE=api` is an ordinary OpenAI-compatible endpoint.

`LLM_MODE=clipboard` (the default here) is a **human relay** — there is no model
API on this tenant. `agent/llm/clipboard_llm.py` implements LangChain's
`BaseChatModel`: it copies the rendered prompt to the clipboard and blocks; you
paste it into M365 Copilot, copy the reply, and the agent parses that reply into
proper `AIMessage.tool_calls`. The graph, the tools and the UI cannot tell the
difference.

It carries the scar tissue of making that work: Copilot markdown-escapes
underscores, renders typographic quotes, writes unescaped quotes inside JSON
strings, and turns a long paste into a file attachment instead of reading it —
so there is a compact prompt, a quote repairer, and a delta mode that pastes
only new messages.

### The three privacy layers (easy to confuse — they are separate)

| Layer | File | Protects against |
|---|---|---|
| **Credential redaction** | `mcp_tools/redact.py` | The CMDB returning passwords. Builds an allowlisted record: only named scalars are copied, so a new field in the API is absent by construction rather than leaking by default. |
| **Address masking** | `agent/llm/ip_mask.py` | Real IPv4/IPv6/MAC reaching the model. Reversible, /24-preserving, so same-subnet reasoning still works. |
| **Entity masking** | `agent/entities.py` | Hostnames, ACLs, VRFs, interfaces, route distinguishers. Learned from tool results as they arrive, because a name cannot be recognised by shape. |

All reversible masking is undone **before** the command is validated, approved
or executed — the guard, the approval card and the device all see reality. Only
the model sees stand-ins. Credentials are dropped at the MCP boundary and never
enter the agent process at all.

---

## 4. The API layer (`api/`)

| File | Role |
|---|---|
| `main.py` | The endpoints, their documentation, the static mount |
| `models.py` | Pydantic request/response shapes — these *are* the Swagger page |
| `runs.py` | The run store and the drive loop that walks the graph |
| `workflow.py` | The panel's state: stages, command rows, path, report |

### A run

A run is one agent turn, addressable by id, executing in the background:

```
running ──▶ waiting_approval ──▶ running ──▶ … ──▶ done
   │              (parked on a Future)                │
   └──────────────────────────────────────────────▶ error
```

Runs live in memory (`RunStore`, capped at 50). They are live conversations, not
records — restarting the server clears them.

**One run at a time.** The agent is a single conversation with a single model,
and in clipboard mode there is exactly one clipboard: a second concurrent run
would consume the reply meant for the first. Starting one while another is
active returns `409`.

**One graph thread for the process.** That is what lets a question be answered
from the previous run's evidence — the CMDB records and the probe output are
still in the conversation.

### The endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/health` | Backend mode, mocks, masking, active run |
| `POST` | `/api/runs` | Start a troubleshooting run → `202` + run |
| `GET` | `/api/runs` | Recent runs, newest first |
| `GET` | `/api/runs/{id}` | One run in full |
| `GET` | `/api/runs/{id}/events` | SSE: the whole run object on every change |
| `POST` | `/api/runs/{id}/approvals/{aid}` | Approve or reject the parked command |
| `POST` | `/api/runs/{id}/deep` | Run the deeper diagnostics |
| `POST` | `/api/ask` | A question, answered from run context |
| `GET` | `/api/devices/{name}` | Direct CMDB lookup |

Interactive docs at **`/docs`**, spec at `/openapi.json`.

Every SSE frame is the *complete* run object, not a delta, so a client never
merges partial updates — it renders whatever arrived last, and a reconnect
self-heals.

---

## 5. The frontend (`frontend/src/`)

| File | Role |
|---|---|
| `api.js` | Every call to the backend. The only place a URL appears. |
| `App.jsx` | Holds one run object; wires actions to endpoints |
| `Console.jsx` | The console: command bar, stage strip, activity feed, report |
| `ChatPanel.jsx` | The optional chat drawer |
| `styles.css` | Both themes as CSS variables |

### State flow

```
   CommandBar ──POST /api/runs──▶ backend
                                    │
   App  ◀──── SSE: whole run ───────┘
     │
     ├─▶ Console      (stage strip · activity feed · report tabs)
     │      └─ Approval card ──POST approvals──▶ backend
     └─▶ ChatPanel ────────────POST /api/ask───▶ backend
```

`App.jsx` holds **one piece of real state: the current run object, exactly as
the API returns it.** Everything on screen is derived from it — the stage strip
from `workflow.steps`, the feed from `workflow.basics` + `workflow.checks`, the
verdict from `report.result`. No state exists in the browser that the backend
does not also hold, which is why a mid-run refresh rejoins the run instead of
losing it (`App.jsx` looks for a live run on load and re-subscribes).

### The console, top to bottom

- **Command bar** — source → destination, service, port, Run. This is the
  primary path; you never have to type a sentence.
- **Stage strip** — CMDB · Ping · Traceroute · Policy · Conclusion, each
  pending / running / done / failed / skipped. An amber note appears when the
  run fell back to probing from the agent host.
- **Approval card** — one line: the command, where it runs, Approve / Reject.
- **Activity feed** — one row per command: status dot, command, device chip,
  one-line result. Click a row for raw output; "show reasoning" reveals the
  model's thought before each step. Collapsible.
- **Report card** — verdict banner, then three tabs: **Report** (endpoints,
  cause, next step, evidence), **Path** (the hop chain as chips), **Deep** (the
  offer button → spinner → the deeper-checks verdict).
- **Chat drawer** — optional, behind the header button. Questions go to
  `/api/ask` with the current run id, so answers use that run's evidence. A
  question never disturbs the report it is asked about.

---

## 6. Where things live

```
netops-react/
├── agent/            the agent: graph, prompts, guards, masking, LLM backends
├── mcp_tools/        the four MCP servers + credential redaction
├── api/              FastAPI: endpoints, run store, panel state
├── frontend/src/     React console + chat
├── tests/            plain scripts, no pytest; mocks/ for offline runs
└── data/             vendor command registry
```

## 7. Running and testing it

```powershell
.\run.ps1                  # real MCPs, clipboard relay
.\run.ps1 -Mock            # mock CMDB / devices / Tufin / probes
.\run.ps1 -Dev             # + Vite dev server with hot reload
```

Offline demo with no Copilot and no devices:

```powershell
.venv\Scripts\python.exe tests\fake_llm.py 11499
$env:LLM_MODE="api"; $env:LLM_BASE_URL="http://127.0.0.1:11499/v1"; .\run.ps1 -Mock
```

Tests are plain scripts — exit 0 means pass:

```powershell
.venv\Scripts\python.exe tests\test_flow.py         # the whole graph
.venv\Scripts\python.exe tests\test_local_probe.py  # the local fallback
.venv\Scripts\python.exe tests\test_entities.py     # name masking
.venv\Scripts\python.exe tests\test_redact.py       # credential redaction
.venv\Scripts\python.exe tests\test_ip_mask.py      # address masking
```

## 8. If you change one thing, remember the other

- **The prompt or the tool list** → regenerate the Copilot agent instructions:
  `python -m agent.llm.clipboard_llm`. They are a *copy* pasted into Copilot and
  do not follow the code.
- **`mcp_tools/troubleshoot_agent_mcp.py`** → it is deployed on the host with
  bastion access; the copy here is the source of truth for what runs there.
- **The response models in `api/models.py`** → that is the published contract;
  the frontend and Swagger both follow it.
