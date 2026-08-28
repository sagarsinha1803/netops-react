"""The workflow panel's state, ported from the Chainlit UI minus Chainlit.

Pure state: the WebSocket layer decides when to push a snapshot. No polling
route, no element re-mounting, no publish bookkeeping -- the React client gets
the whole snapshot on every change and renders it in place, which is the entire
reason for leaving Chainlit.
"""
import ast
import json
import re

from agent import constants as C

STEP_DEFS = [
    ("cmdb", "CMDB lookup"),
    ("ping", "Basic reachability"),
    ("trace", "Traceroute / path discovery"),
    ("policy", "Firewall policy (Tufin)"),
    ("alerts", "Alerts"),
    ("checks", "Deeper checks"),
    ("done", "Conclusion"),
]

# the deeper diagnostics are offered as a follow-up, not shown as a stage
HIDDEN_STEPS = {"checks"}

_NAME_IN_BLOB = re.compile(r"['\"]name['\"]\s*:\s*['\"]([^'\"]+)['\"]")
_NAME_KEYS = ("name", "device_name", "hostname", "host_name", "ci_name")


def device_label(blob, fallback):
    """Hostname out of a CMDB record, whatever shape it came back in."""
    if not blob:
        return fallback
    try:
        d = json.loads(str(blob))
        if isinstance(d, dict):
            data = d.get("data") if isinstance(d.get("data"), dict) else d
            for k in _NAME_KEYS:
                if data.get(k):
                    return str(data[k])
    except Exception:
        pass
    m = _NAME_IN_BLOB.search(str(blob))
    return m.group(1) if m else fallback


def cmdb_record(blob):
    """(found, label) for one CMDB reply.

    "Found" must not be decided by whether the record carries a hostname. A
    lookup BY NAME deliberately returns no `name` field -- it would only echo
    the caller's own input back -- so judging on that marked a perfectly good
    Arista record as "not found in CMDB", with a red cross on the row while
    the stage above it stayed green off the same data.

    What settles it is whether a RECORD came back at all: the tool answers
    with a dict on success and a plain "No data found ..." sentence on a miss.
    """
    body = str(blob or "").strip()
    if not body.startswith("{"):
        return False, ""                     # "No data found ...", or an error
    data = None
    for loader in (json.loads, ast.literal_eval):
        try:
            got = loader(body)
            if isinstance(got, dict):
                data = got
                break
        except Exception:
            continue
    if not data:
        return False, ""
    record = data.get("data") if isinstance(data.get("data"), dict) else data
    if not record:
        return False, ""
    for key in _NAME_KEYS:
        if record.get(key):
            return True, str(record[key])
    # a record with no hostname field: name it by what was asked for
    return True, str(data.get("query") or "")


def _json_values(text):
    """Every JSON value in a string, whether it is one array, one object, or
    several objects run together.

    MCP hands a list back as one content block per element, so a three-row
    answer arrives as three objects separated by newlines -- NOT as a JSON
    array. json.loads() sees trailing data and gives up on the lot, which
    would silently drop every row but the last.
    """
    dec = json.JSONDecoder()
    out, i, n = [], 0, len(text)
    while i < n:
        while i < n and text[i] in " \t\r\n,":
            i += 1
        if i >= n:
            break
        try:
            value, i = dec.raw_decode(text, i)
        except ValueError:
            break                            # keep whatever already parsed
        out.append(value)
    return out


# the columns the table shows, in the order it shows them
ALERT_COLUMNS = ("device_name", "alert_title", "check_name", "alert_type",
                 "ticket_id", "alert_id")


def parse_alerts(blob):
    """(rows, message) from an Archangel reply.

    The tool answers with a LIST of alert dicts, or a sentence when the device
    has none. Both are useful: the sentence is what the panel shows when there
    is nothing to tabulate, and it distinguishes "no open alerts" -- a real,
    good answer -- from a query that failed.
    """
    body = str(blob or "").strip()
    if not body:
        return [], ""
    if not body.startswith("[") and not body.startswith("{"):
        return [], body                     # "No open alerts found ..." / error

    data = _json_values(body)
    if not data:
        try:
            data = [ast.literal_eval(body)]
        except Exception:
            return [], body

    # one array of rows, or several rows run together -- flatten either
    items = []
    for value in data:
        items.extend(value if isinstance(value, list) else [value])

    # Calling an MCP tool directly hands back CONTENT BLOCKS rather than the
    # plain text the graph path gets: [{"type": "text", "text": "{...}"}].
    # Unwrap them, or every column reads as empty while the row count looks
    # perfectly correct -- which is worse than an outright failure.
    unwrapped = []
    for item in items:
        if (isinstance(item, dict) and item.get("type") == "text"
                and isinstance(item.get("text"), str)):
            inner = _json_values(item["text"].strip())
            for value in inner:
                unwrapped.extend(value if isinstance(value, list) else [value])
            if not inner:
                return [], item["text"].strip()      # a sentence, not rows
        else:
            unwrapped.append(item)
    items = unwrapped

    rows = []
    for item in items:
        if not isinstance(item, dict):
            continue
        row = {k: ("" if item.get(k) is None else str(item.get(k)))
               for k in ALERT_COLUMNS}
        # keep anything the query returned that is not in the fixed column
        # list, so a schema that grows is visible rather than silently dropped
        extra = {k: str(v) for k, v in item.items() if k not in ALERT_COLUMNS}
        if extra:
            row["extra"] = extra
        rows.append(row)
    return rows, ""


class Workflow:
    """One run's live state: timeline stages, Basic/Deep command lists, the
    parsed path, and the report(s)."""

    def __init__(self, title="Guided troubleshooting"):
        self.state = {k: {"status": "pending", "detail": ""} for k, _ in STEP_DEFS}
        self.title = title
        self.params = {}          # what the current run is actually about
        self.scope = "path"       # "path" = full workflow, "lookup" = CMDB only
        self.local = False        # a probe ran on the agent host, not the source
        self.cmdb_miss = False    # no CMDB record: no device to run commands on,
                                  # so the run goes straight to the policy check
        self.checks = []          # deep-check commands
        self.alerts = []          # open alerts from Archangel, for the table
        self.basics = []          # the CMDB / ping / traceroute of the main run
        self.summary = {}         # ping_ok, path, hops -- parsed from raw CLI
        self.report = None        # final answer, for the Report tab
        self.deep_report = None   # the deeper-checks answer, appended below it
        self.path = {"nodes": [], "line": "", "reached": None}
        # every instrument that has an opinion about the route, keyed by
        # which one it was: they disagree, and the disagreement is the find
        self.paths = {}
        self._basic_seen = set()
        self._kind = {}           # command -> "ping" | "trace" | "deep"

    # ---- snapshot ----------------------------------------------------------
    def snapshot(self) -> dict:
        keep = ("cmdb", "done") if self.scope == "lookup" else None
        steps = [
            {"key": k, "label": label,
             "status": self.state[k]["status"], "detail": self.state[k]["detail"]}
            for k, label in STEP_DEFS
            if k not in HIDDEN_STEPS and (keep is None or k in keep)
        ]
        # The traceroute names the two ends; the other instruments were asked
        # by address and would otherwise label the same device three different
        # ways in three drawings of one route.
        paths = {}
        for kind, path in self.paths.items():
            nodes = [dict(n) for n in (path.get("nodes") or [])]
            if kind != "traceroute" and nodes:
                if nodes[0].get("kind") == "source":
                    nodes[0]["label"] = self.path_source()
                if nodes[-1].get("kind") == "dest":
                    nodes[-1]["label"] = self.path_dest()
                path = dict(path, nodes=nodes, line=_line(nodes))
            paths[kind] = path

        return {"title": self.title, "steps": steps, "params": self.params,
                "checks": self.checks, "basics": self.basics,
                "summary": self.summary, "report": self.report,
                "deepReport": self.deep_report, "path": self.path,
                "paths": paths,
                "local": self.local, "cmdbMiss": self.cmdb_miss,
                "alerts": self.alerts, "maxDeep": C.DEEP_MAX_LOOPS}

    # ---- mutation ----------------------------------------------------------
    def reset(self, params=None, scope="path"):
        for k in self.state:
            self.state[k] = {"status": "pending", "detail": ""}
        self.scope = scope
        self.local = False
        self.cmdb_miss = False
        self.path = {"nodes": [], "line": "", "reached": None}
        self.paths = {}
        self.basics = []
        self.checks = []
        self.alerts = []
        self.summary = {}
        self.report = None
        self.deep_report = None
        self._basic_seen = set()
        self._kind = {}
        if params:
            self.params = params

    def set(self, key, status=None, detail=None):
        if key not in self.state:
            return
        if status:
            self.state[key]["status"] = status
        if detail is not None:
            self.state[key]["detail"] = detail

    @staticmethod
    def stage_for(command: str, tool: str = "") -> str:
        c = f"{tool} {command}".lower()
        if "trace" in c or "tracert" in c:
            return "trace"
        if "ping" in c:
            return "ping"
        return "checks"

    def classify(self, command: str, deep_turn: bool = False) -> str:
        """Basic stage or deep check? First ping/trace of a normal turn owns
        the Basic timeline; everything after that is escalation."""
        if command in self._kind:
            return self._kind[command]      # asked again at the approval pause
        kind = "deep"
        if not deep_turn:
            stage = self.stage_for(command)
            if stage in ("ping", "trace") and stage not in self._basic_seen:
                self._basic_seen.add(stage)
                kind = stage
        self._kind[command] = kind
        return kind

    def from_tool_call(self, name, args, command_text):
        """Mark the stage a tool call belongs to as running."""
        from agent.constants import ALERT_TOOL_NAMES, POLICY_TOOL_NAMES
        if name in ALERT_TOOL_NAMES:
            self.set("alerts", "running",
                     f"open alerts for {args.get('device_name', '')}")
            return
        if name in POLICY_TOOL_NAMES:
            self.set("policy", "running",
                     f"{args.get('src', '')} → {args.get('dst', '')}"
                     + (f" {args['service']}" if args.get("service") else ""))
            return
        if "device_details" in name or "get_device" in name:
            self.set("cmdb", "running", f"looking up {args.get('device_name', '')}")
            return
        if name.startswith("local_"):
            self.local = True
        self.set(self.stage_for(command_text, name), "running", command_text[:60])

    def add_check(self, command, device, region=None, thought="", said=True,
                  kind="device"):
        # kind="system" is a step that asked Tufin, the CMDB or Archangel
        # rather than a device. It belongs in the list -- it spent a step of
        # the budget -- but it is not evidence about the route.
        self.checks.append({"cmd": command, "device": device, "region": region,
                            "status": "running", "detail": "", "thought": thought,
                            "saidIt": bool(said and thought), "kind": kind,
                            "output": "", "step": len(self.checks) + 1})
        return len(self.checks) - 1

    def finish_check(self, idx, ok, detail="", output=""):
        if 0 <= idx < len(self.checks):
            self.checks[idx]["status"] = "done" if ok else "failed"
            self.checks[idx]["detail"] = detail
            if output:
                self.checks[idx]["output"] = output[:2000]

    def add_basic(self, command, device=None, region=None, thought="",
                  kind="cmdb", said=True):
        # `said` is False when the model attached no words and the line under
        # the row is our own description of the step. The panel is evidence:
        # it must not put words in the model's mouth.
        self.basics.append({"cmd": command, "device": device, "region": region,
                            "status": "running", "detail": "", "thought": thought,
                            "saidIt": bool(said and thought),
                            "output": "", "kind": kind,
                            "step": len(self.basics) + 1})
        return len(self.basics) - 1

    def finish_basic(self, idx, ok, detail="", device=None, output=""):
        if 0 <= idx < len(self.basics):
            self.basics[idx]["status"] = "done" if ok else "failed"
            self.basics[idx]["detail"] = detail
            if device:
                self.basics[idx]["device"] = device
            if output:
                self.basics[idx]["output"] = output[:2000]

    # ---- derived from graph state -----------------------------------------
    def _path_from_state(self, state):
        hops = state.get("hops") or []
        if not hops:
            return None
        devices = state.get("devices") or {}
        keys = list(devices)
        src_key = (self.params or {}).get("source") or ""
        dst_key = (self.params or {}).get("dest") or ""
        src_blob = devices.get(src_key) or (devices[keys[0]] if keys else None)
        dst_blob = devices.get(dst_key) or (devices[keys[1]] if len(keys) > 1 else None)

        src_label = device_label(src_blob, src_key or "source")
        if self.local:
            src_label = "agent host"
        dst_label = device_label(dst_blob, dst_key or "destination")
        nodes = [{"label": src_label, "ip": None, "kind": "source"}]
        died = False
        for h in hops:
            label = str(h.get("host") or h.get("ip") or f"hop {h.get('n')}")
            # A hop nobody answered is where the trace ends, however the parser
            # labelled it. Rendering "* * *" as a node -- twice, once per
            # unanswered ttl -- draws silence as if it were equipment.
            if h.get("timeout") or set(label) <= {"*", " "}:
                died = True
                break
            if label == dst_label:
                continue          # the destination answering IS the dest node
            ip = h.get("ip")
            nodes.append({"label": label,
                          "ip": ip if (h.get("host") and ip != label) else None,
                          "kind": "hop"})

        reached = bool(state.get("ping_ok"))
        if reached:
            nodes.append({"label": dst_label, "ip": None, "kind": "dest"})
        else:
            nodes.append({"label": "X", "ip": None, "kind": "dead"})

        line = "  →  ".join(
            n["label"] + (f" ({n['ip']})" if n["ip"] else "") for n in nodes)
        return {"nodes": nodes, "line": line, "reached": reached,
                "truncated": died and not reached}

    def dest_addr(self):
        """The destination's ADDRESS, whatever the user typed.

        Commands are written against addresses; the box has no idea what the
        CMDB calls it. Comparing a device NAME against a ping command never
        matches, so a probe that proved reachability was skipped and the
        destination was mistaken for a transit hop.
        """
        typed = str((self.params or {}).get("dest") or "").strip()
        if re.match(r"^\d{1,3}(\.\d{1,3}){3}$", typed):
            return typed
        rows = [r for r in self.basics if r.get("kind") == "cmdb"]
        for row in reversed(rows):               # the destination is looked up second
            found = re.search(r"managementIp\"?\s*:\s*\"?(\d{1,3}(?:\.\d{1,3}){3})",
                              str(row.get("output") or ""))
            if found:
                return found.group(1)
        return typed

    def path_source(self):
        """The label the paths should call the source.

        Taken from the traceroute's own first node when there is one, so three
        drawings of the same route do not name its ends three different ways.
        """
        nodes = (self.path or {}).get("nodes") or []
        if nodes:
            return nodes[0].get("label") or "source"
        return str((self.params or {}).get("source") or "source")

    def _cmdb_labels(self):
        """The hostnames the CMDB returned, in lookup order: source, then dest."""
        return [str(row.get("device")) for row in self.basics
                if row.get("kind") == "cmdb" and row.get("device")]

    def path_dest(self):
        nodes = (self.path or {}).get("nodes") or []
        if nodes and nodes[-1].get("kind") == "dest":
            return nodes[-1].get("label") or "destination"
        # a trace that never arrived has no destination node, but the CMDB
        # still knows what the far end is called -- and an address where every
        # other node is a hostname reads as a different kind of thing
        labels = self._cmdb_labels()
        if len(labels) > 1:
            return labels[1]
        return str((self.params or {}).get("dest") or "destination")

    def set_path(self, kind, path):
        """Record one instrument's view of the route."""
        if path:
            self.paths[kind] = path

    def from_state(self, state):
        """Sync the timeline with what the graph has actually established."""
        path = self._path_from_state(state)
        if path:
            self.path = path
            self.paths["traceroute"] = dict(
                path, note=path.get("note") or
                ("Live from the source: every hop that answered a probe."
                 if path.get("reached") else
                 "Live from the source. It stops where the probes stopped "
                 "coming back, which is not always where the fault is."))
        self.summary = {"ping_ok": state.get("ping_ok"),
                        "path": state.get("path") or "",
                        "hops": len(state.get("hops") or []),
                        "local": self.local}
        devices = state.get("devices") or {}
        if devices:
            # devices holds EVERY lookup, hits and misses alike. A hit is a
            # record; a miss is the plain "No data found ..." / "Error ..."
            # sentence the CMDB returns. A green tick for a lookup that found
            # nothing is a lie -- mark it. Same test as the rows use, so the
            # stage and the rows below it can never disagree.
            found = [k for k, v in devices.items() if cmdb_record(v)[0]]
            missed = len(devices) - len(found)
            if not found:
                self.set("cmdb", "failed", "no record in CMDB")
                # no record -> no management address and no region, so no
                # device command is possible; the run goes to the policy check
                self.cmdb_miss = True
            elif missed:
                self.set("cmdb", "done",
                         f"{len(found)} found, {missed} not in CMDB")
            else:
                self.set("cmdb", "done",
                         f"{len(found)} device record(s) fetched")
        ping = state.get("ping_ok")
        if ping is True:
            self.set("ping", "done", "Ping result: reachable"
                     + (" (from agent host)" if self.local else ""))
        elif ping is False:
            self.set("ping", "failed", "Ping result: failed"
                     + (" (from agent host)" if self.local else ""))
        hops = state.get("hops") or []
        if hops:
            self.set("trace", "done", f"path contains {len(hops)} hop(s)")
        extra = [c for c in (state.get("commands_run") or [])
                 if "show" in str(c.get("command", "")).lower()]
        if extra:
            self.set("checks", "done", f"{len(extra)} show command(s) run")


# ---- request parsing / report shaping (shared with the old UI's logic) ------
_REQ_RE = re.compile(
    r"([\w.\-]+)\s+to\s+([\w.\-]+)"
    r"(?:\s+(TCP|UDP|HTTP))?"
    r"(?:\s+(\d{1,5}))?",
    re.I)


def parse_request(text: str) -> dict:
    m = _REQ_RE.search(text or "")
    if not m:
        return {}
    src, dst, proto, port = m.groups()
    return {"source": src, "dest": dst,
            "protocol": (proto or "TCP").upper(), "port": port or "22"}


# the fields the Report tab lays out; anything else is prose
REPORT_KEYS = ("source", "destination", "result", "ping", "path", "cause",
               "next_step", "evidence")


def _unwrap(data):
    """{"thought":.., "final":{..}} -> the report inside it."""
    while isinstance(data, dict) and "final" in data:
        inner = data["final"]
        if not isinstance(inner, dict):
            return {"text": str(inner)}
        data = inner
    return data


def as_report(text: str):
    """The final answer as structured data for the Report tab.

    Models rarely hand back the bare object the prompt asks for. They summarise
    in prose first, fence the JSON, wrap it in {"thought":.., "final":..}, or
    all three at once -- and every one of those used to fall through to "show
    the whole blob as text", which is how a finished run still looked like a
    chatbot transcript instead of a report.

    So: take the JSON wherever it is, unwrap the envelope, and only accept it
    as a report if it actually carries report fields. A JSON object that is not
    one -- a tool call the model narrated, say -- stays prose rather than
    rendering as an empty report with every field blank.
    """
    body = str(text or "").strip()
    if not body:
        return None

    if body.startswith("{"):
        try:
            data = json.loads(body)
            if isinstance(data, dict):
                return _unwrap(data)
        except Exception:                       # noqa: BLE001 -- try harder below
            pass

    # prose around it, a ``` fence, smart quotes from a copy-paste: the relay's
    # extractor already handles all of that, so use it rather than a second
    # half-parser that will drift from it
    try:
        from agent.llm.clipboard_llm import _extract_json
        found = _extract_json(body)
    except Exception:                           # noqa: BLE001
        found = None

    if isinstance(found, dict):
        report = _unwrap(found)
        if isinstance(report, dict) and any(k in report for k in REPORT_KEYS):
            return report

    return {"text": body}


# A command can fail two different ways, and both must read as failure: the
# probe ran and reported no reachability, OR the device rejected the syntax.
# The second was missing, so a command the device never even accepted showed a
# green tick with the error text as its "result".
_REJECTED_SYNTAX = (
    r"% invalid input"
    r"|invalid input detected"
    r"|% incomplete command"
    r"|% ambiguous command"
    r"|% unknown command"
    r"|unknown command"
    r"|% type \"help\""
    r"|syntax error"
    r"|invalid command"
    r"|command not found"
    r"|% bad "
    r"|unrecognized command"
    r"|error: unknown"
)

_FAILED_OUTPUT = re.compile(
    r"^\s*(REJECTED|\[error|error:)"
    r"|unknown tool"
    r"|success rate is 0 percent"
    # a real device prints "100.00% packet loss" or "100.0%", not "100%" --
    # matching only the round form read total loss as a success
    r"|(?<![\d.])100(?:\.0+)?% packet loss"
    r"|\b0 (?:packets )?received"
    r"|request timed out"
    r"|destination (host|net) unreachable"
    r"|% (network|destination) .*(not|unreachable)"
    r"|" + _REJECTED_SYNTAX,
    re.I)

_SYNTAX_RE = re.compile(_REJECTED_SYNTAX, re.I)

# Output that cannot be a reachability result for other reasons: the box would
# not let us in, or it answered with a pager instead of a result.
_UNUSABLE = re.compile(
    _REJECTED_SYNTAX
    + r"|% permission denied|permission denied|not authorized|authorization failed"
    + r"|login failed|authentication failed|--more--",
    re.I)


# a line that is only a device prompt: no spaces, ending in # > or $
_BARE_PROMPT = re.compile(r"^\S*[#>$]\s*$")


def echo_parts(command: str):
    """The individual commands inside what the panel shows as one line."""
    return [part.strip() for part in str(command or "").split(";")
            if part.strip()]


def usable_output(body, command: str = "") -> bool:
    """Did this output answer the command at all?

    Separate from "did the probe succeed". A refusal, a permission error, a
    pager prompt, or nothing but the echoed command are all silence -- and
    silence is not evidence that the destination is unreachable.
    """
    text = str(body or "")
    if _UNUSABLE.search(text):
        return False
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if command:
        # drop the device's echo of the command itself. Each command
        # SEPARATELY: a call that carries two of them is shown joined with a
        # semicolon, and the device echoes them one per line -- so matching the
        # joined string matched nothing, and a session that answered with two
        # echoes and no output kept its green tick.
        lines = [ln for ln in lines
                 if not any(part in ln for part in echo_parts(command))]
    # and the bare prompt it prints afterwards -- "SW-01#", "user@host:~$",
    # "RP/0/RSP0/CPU0:CORE-01#". Left in, a session that answered nothing at
    # all still looked like it had said something.
    lines = [ln for ln in lines if not _BARE_PROMPT.match(ln)]
    return bool(lines)


def rejected_syntax(body) -> bool:
    """True when the DEVICE refused the command, rather than answering it.

    Worth separating from a failed probe: a rejected command says nothing about
    reachability, and the right response is a different syntax for that
    platform, not a conclusion.
    """
    return bool(_SYNTAX_RE.search(str(body or "")))


def check_ok(body: str) -> bool:
    return not _FAILED_OUTPUT.search(str(body or ""))


def failed_line(body: str):
    for ln in str(body or "").splitlines():
        if _FAILED_OUTPUT.search(ln):
            return ln.strip()
    return None


def policy_verdict(body: str):
    """(verdict, acl) out of a get_firewall_path reply."""
    import ast
    text = str(body or "")
    data = None
    for loader in (json.loads, ast.literal_eval):
        try:
            got = loader(text)
            if isinstance(got, dict):
                data = got
                break
        except Exception:
            continue
    if data:
        verdict = str(data.get("verdict") or "").upper()
        rules = data.get("blocking_rules") or []
        acl = str(rules[0].get("acl") or "") if rules else ""
        if verdict:
            return verdict, acl

        # No "verdict" key: this is SecureTrack's own payload rather than the
        # summary. The tool may be returning it raw -- read the same facts out
        # of the shape the API actually uses, so the panel and the report work
        # either way instead of reporting UNKNOWN for a perfectly good answer.
        results = data.get("path_calc_results") or data
        allowed = results.get("traffic_allowed")
        if allowed is not None:
            verdict = "ALLOWED" if allowed else "BLOCKED"
            return verdict, acl or _first_denying_rule(results)

    verdict = next((v for v in ("BLOCKED", "ALLOWED") if v in text.upper()),
                   "UNKNOWN")
    m = re.search(r"['\"]acl['\"]\s*:\s*['\"]([^'\"]+)['\"]", text)
    return verdict, (m.group(1) if m else "")


_DENY = ("deny", "drop", "reject")


def _first_denying_rule(node, depth: int = 0) -> str:
    """The name of the first deny rule anywhere in a raw SecureTrack payload.

    Vendors label rules differently -- aclName on a Cisco ACL entry,
    name/ruleIdentifier on a Fortinet policy -- so this takes whichever is
    present rather than insisting on one.
    """
    if depth > 8:
        return ""
    if isinstance(node, dict):
        action = str(node.get("action") or "").lower()
        if action in _DENY:
            for key in ("aclName", "name", "ruleIdentifier", "ruleNumber"):
                if node.get(key):
                    return str(node[key])
        for value in node.values():
            found = _first_denying_rule(value, depth + 1)
            if found:
                return found
    elif isinstance(node, list):
        for item in node[:50]:
            found = _first_denying_rule(item, depth + 1)
            if found:
                return found
    return ""


# ---------------------------------------------------------------- paths -----
# Three different instruments answer "where does the traffic go", and they
# answer differently on purpose:
#
#   traceroute  what the packets DID, live, one probe at a time
#   Tufin       what the topology and the policy SAY should happen
#   deep checks what the source's own forwarding table intends to do next
#
# Showing only the traceroute hides the disagreement, and the disagreement is
# usually the finding: a trace that dies where Tufin says the path is clear is
# a different problem from one that dies where a rule blocks it.

def _line(nodes) -> str:
    return "  \u2192  ".join(
        n["label"] + (f" ({n['ip']})" if n.get("ip") else "") for n in nodes)


# SecureTrack labels some steps with what the hop IS rather than which device
# it is. They belong in the note, not in the chain.
_NOT_A_DEVICE = {"DIRECTLY_CONNECTED", "DIRECTLY CONNECTED", "UNROUTED",
                 "INTERNET", "ANY", "UNKNOWN", "N/A", "NONE"}


def path_from_policy(body, src_label="source", dst_label="destination"):
    """The device chain SecureTrack modelled, as a path."""
    import ast
    data = None
    for loader in (json.loads, ast.literal_eval):
        try:
            got = loader(str(body or ""))
            if isinstance(got, dict):
                data = got
                break
        except Exception:
            continue
    if not isinstance(data, dict):
        return None

    hops = data.get("hops")
    if not hops:
        flat = data.get("device_path") or []
        hops = [[name] for name in flat if name]
    if not hops:
        return None

    # Two different questions, and the drawing has to answer both: does the
    # topology ROUTE it, and does the policy PERMIT it. SecureTrack can model
    # a complete chain end to end and deny the traffic on it -- that is what a
    # firewall is. Reading only the routing drew a green line to a destination
    # nothing reaches, directly contradicting the BLOCKED verdict beside it.
    routed = data.get("reaches_destination")
    if routed is None:
        routed = not (data.get("unrouted_elements")
                      or (data.get("path_calc_results") or {})
                      .get("unrouted_elements"))

    verdict, acl = policy_verdict(str(body or ""))
    if verdict == "BLOCKED":
        reached = False
    elif verdict == "ALLOWED":
        reached = bool(routed)
    else:
        reached = None if routed else False

    ends = {str(src_label).strip().lower(), str(dst_label).strip().lower()}
    nodes = [{"label": src_label, "ip": None, "kind": "source"}]
    for step in hops:
        names = [str(n).strip() for n in
                 (step if isinstance(step, list) else [step]) if n]
        # SecureTrack's chain INCLUDES the two ends, and its own markers for
        # what a hop is rather than which device it is. Drawing those gives
        # "edge-a1 -> EDGE-A1 -> EDGE-A2 -> DIRECTLY_CONNECTED -> edge-a2":
        # the source twice and a routing note dressed up as equipment.
        names = [n for n in names
                 if n.lower() not in ends and n.upper() not in _NOT_A_DEVICE]
        if not names:
            continue
        # One chip per device, in the order SecureTrack walked them. Equal-cost
        # alternatives used to be crammed into a single "A / B" chip: accurate
        # about the topology, unreadable as a path. They are laid out in
        # sequence now and MARKED as alternatives instead of run together.
        for i, name in enumerate(names):
            node = {"label": name, "ip": None, "kind": "hop"}
            if len(names) > 1:
                node["alt"] = f"{i + 1} of {len(names)} at this step"
            nodes.append(node)
    if reached is True:
        nodes.append({"label": dst_label, "ip": None, "kind": "dest"})
    elif reached is False:
        nodes.append({"label": "X", "ip": None, "kind": "dead"})
    else:
        nodes.append({"label": "?", "ip": None, "kind": "unknown"})

    note = str(data.get("path_note") or "")
    if not note:
        modelled = ("Modelled by SecureTrack from the topology and the rules "
                    "-- not a live probe.")
        if verdict == "BLOCKED":
            note = (f"The chain is routed end to end, but the traffic is "
                    f"denied on it{f' by {acl}' if acl else ''} -- so it stops "
                    f"there rather than arriving. " + modelled)
        elif not routed:
            note = ("SecureTrack has no route for this pair: the traffic is "
                    "not delivered anywhere.")
        elif reached is None:
            note = ("SecureTrack modelled the chain but returned no verdict "
                    "for it, so whether the traffic is permitted is unsettled. "
                    + modelled)
        else:
            note = modelled
    return {"nodes": nodes, "line": _line(nodes), "reached": reached,
            "note": note}


# Every platform spells the next hop differently, and a parser that knows only
# one of them draws no path at all on the other four -- silently, because "no
# next hop found" and "this device has no route" look identical from here.
#
#   NX-OS      *via 10.0.0.1, Eth1/3, [200/0], 02:14:31, bgp-65010
#   IOS-XE     nexthop 10.0.0.1 TenGigabitEthernet0/0/3          (show ip cef)
#   IOS/XR     * 10.0.0.1, from 10.0.0.1, via TenGigE0/0/0/1     (show route)
#   IOS-XR     10.0.0.1, from 10.0.0.1                           (no interface)
#   generic    next hop 10.0.0.1, TenGigE0/0/0/1
_IP = r"\d{1,3}(?:\.\d{1,3}){3}"
# an interface name has a digit in it somewhere: Eth1/3, ge-0/0/1, Vlan10,
# TenGigabitEthernet0/0/3, Bundle-Ether90, port1
_IFACE = r"[A-Za-z][\w./:-]*\d[\w./:-]*"

_NEXT_HOP = tuple(re.compile(pattern, re.I | re.M) for pattern in (
    rf"\*?\s*via\s+({_IP})\s*,\s*({_IFACE})",
    rf"next\s*hop\s+({_IP})\s*,?\s*({_IFACE})?",
    rf"^\s*\*?\s*({_IP})\s*,\s*from\s+{_IP}\s*(?:,\s*via\s+({_IFACE}))?",
    rf"\*?\s*via\s+({_IP})()\b",
))

# "Incomplete" as a STATE, not the word appearing anywhere: NX-OS prints
# INCOMPLETE in the MAC column, IOS prints it as the hardware address, IOS-XR
# gives it its own State column
_ARP_INCOMPLETE = re.compile(r"\bincomplete\b", re.I)
_IF_DOWN = re.compile(
    r"^\s*(\S+)\s+is\s+(?:administratively\s+)?down", re.I | re.M)

# positive evidence that the next hop is really there: an ARP entry with a
# hardware address behind it, or an interface the device calls up
_ARP_RESOLVED = re.compile(
    r"\b[0-9a-f]{4}\.[0-9a-f]{4}\.[0-9a-f]{4}\b"          # cisco 0050.56be.1a2b
    r"|\b(?:[0-9a-f]{2}:){5}[0-9a-f]{2}\b", re.I)           # 00:50:56:be:1a:2b
_IF_UP = re.compile(r"^\s*\S+\s+is\s+up,\s*line protocol is up", re.I | re.M)



# "directly connected" / "attached", however the platform spells it: the two
# ends share a subnet and the path between them IS the interface
_CONNECTED = re.compile(
    # "is directly connected" must not match BEFORE the form that names the
    # interface, or the interface is lost and the path cannot be drawn
    r"(?:is\s+)?directly\s+connected(?:\s*,\s*([A-Za-z][\w./:-]*\d[\w./:-]*))?"
    r"|\battached\b(?:\s*,\s*([A-Za-z][\w./:-]*\d[\w./:-]*))?", re.I)


# a route lookup that found nothing, in the words each platform uses
_NO_ROUTE = re.compile(
    r"%?\s*network not in table"
    r"|route not found"
    r"|no route to host"
    r"|no longest match"
    r"|%\s*network not found"
    r"|not in (?:the )?(?:routing|forwarding) table", re.I)

# a forwarding entry that exists but cannot be used
_FIB_BROKEN = re.compile(
    r"incomplete\s*-?\s*drop adjacency|drop adjacency|punt adjacency"
    r"|adjacency\s+incomplete|glean adjacency", re.I)


# A route line for an address the device owns: it is not a way onward, it is
# the device saying "that one is mine". Reading one as a next hop drew the
# source's own management address as the hop after itself.
_OWN_ADDRESS = re.compile(r"(?<![\w-])(local|receive|broadcast)(?![\w-])", re.I)

_PREFIX = re.compile(r"(?:^|[\s,])(\d{1,3}(?:\.\d{1,3}){3})/(\d{1,2})")


def _as_int(addr: str) -> int:
    parts = [int(p) for p in str(addr).split(".")]
    return (parts[0] << 24) | (parts[1] << 16) | (parts[2] << 8) | parts[3]


def _covers(prefix: str, length: int, addr: str) -> bool:
    """Is addr inside prefix/length? Arithmetic, so it holds for every vendor."""
    try:
        if length <= 0:                          # a default route covers all
            return True
        mask = ((1 << length) - 1) << (32 - length)
        return (_as_int(prefix) & mask) == (_as_int(addr) & mask)
    except Exception:                            # noqa: BLE001 -- malformed line
        return False


def _route_blocks(text: str):
    """[(prefix, length, the lines under it)] out of a routing table.

    A table is a list of prefixes, each with its own next hops indented under
    it. Searching the whole dump for the first thing that looks like a next hop
    answers a question nobody asked -- it returns whichever prefix happens to
    be printed first, which in a real run was the source's own /32.
    """
    blocks, prefix, length, lines = [], "", 0, []
    for line in str(text or "").splitlines():
        found = _PREFIX.search(line)
        # a header names a prefix; a next-hop line sits under one
        if found and not re.search(r"\bvia\b", line, re.I):
            if prefix:
                blocks.append((prefix, length, "\n".join(lines)))
            prefix, length, lines = found.group(1), int(found.group(2)), [line]
        elif prefix:
            lines.append(line)
    if prefix:
        blocks.append((prefix, length, "\n".join(lines)))
    return blocks


def route_for(text: str, dest_addr: str):
    """The part of the output that is the route TO dest_addr, or None.

    None means two different things and the caller has to tell them apart:
    no prefixes were recognised at all (so scope nothing, read it as before),
    or prefixes were recognised and none of them covers the destination (which
    is itself the answer -- there is no route).
    """
    if not dest_addr:
        return None
    blocks = _route_blocks(text)
    if not blocks:
        return None
    hits = [b for b in blocks if _covers(b[0], b[1], dest_addr)]
    if not hits:
        return ""                                # recognised, and none matched
    return max(hits, key=lambda b: b[1])[2]      # most specific wins


def pick_next_hop(text: str, src_addr: str = ""):
    """(next hop, egress interface) the source intends to use, or ("", "").

    Skips the two things that are not a way onward: an address the device owns
    (a local, receive or broadcast entry) and the source's own address.
    """
    body = str(text or "")
    for pattern in _NEXT_HOP:
        for found in pattern.finditer(body):
            start = body.rfind("\n", 0, found.start()) + 1
            end = body.find("\n", found.end())
            line = body[start:end if end != -1 else len(body)]
            if _OWN_ADDRESS.search(line):
                continue
            hop = found.group(1)
            if src_addr and hop == str(src_addr):
                continue
            egress = (found.group(2) or "").strip() if found.lastindex else ""
            return hop, egress
    return "", ""


def blockage(text, next_hop="", egress=""):
    """The exact reason nothing gets through, in a few words, or "".

    Ordered by how close the fault is to the source, because that is the order
    an engineer wants it: a dead interface is a more useful answer than "the
    next hop did not answer", and both beat "no route".
    """
    if egress and _IF_DOWN.search(text):
        for found in _IF_DOWN.finditer(text):
            name = found.group(1)
            if egress.lower() in name.lower() or name.lower() in egress.lower():
                return f"{name} is down"
    # "incomplete" appears in BOTH an unresolved ARP entry and a CEF drop
    # adjacency, and they are different findings: decide by the line it is on,
    # not by the word appearing somewhere in the output
    for line in text.splitlines():
        if not _ARP_INCOMPLETE.search(line):
            continue
        if re.search(r"arpa|arp|neighbou?r", line, re.I):
            return (f"{next_hop} never answered ARP" if next_hop
                    else "the next hop never answered ARP")
    if _FIB_BROKEN.search(text):
        return "the forwarding entry is a drop adjacency"
    if _ARP_INCOMPLETE.search(text):
        return (f"{next_hop} never answered ARP" if next_hop
                else "the next hop never answered ARP")
    if _NO_ROUTE.search(text) and not next_hop:
        return "no route to the destination in any table checked"
    return ""


def _connected_path(checks, src_label, dst_label, dest_addr):
    """A path for two ends that share a subnet: source, interface, destination.

    There is no next hop to chase here. The path IS the link, and what proves
    it is the interface state, the ARP entry and the neighbour on the far end
    -- so that is what gets drawn and what gets said.
    """
    text = "\n".join(str(c.get("output") or "") for c in (checks or []))
    found = _CONNECTED.search(text)
    if not found:
        return None
    intf = next((g for g in found.groups() if g), "")
    if not intf:                                  # the interface named nearby
        near = re.search(r"via\s+([A-Za-z][\w./:-]*\d[\w./:-]*)", text, re.I)
        intf = near.group(1) if near else ""
    if not intf:
        return None

    down = any(intf.lower() in m.group(1).lower()
               or m.group(1).lower() in intf.lower()
               for m in _IF_DOWN.finditer(text))
    resolved = bool(_ARP_RESOLVED.search(text))
    up = bool(_IF_UP.search(text))

    nodes = [{"label": src_label, "ip": None, "kind": "source"},
             {"label": intf, "ip": None, "kind": "intf"}]
    if down:
        nodes.append({"label": "X", "ip": None, "kind": "dead",
                      "why": f"{intf} is down"})
        return {"nodes": nodes, "line": _line(nodes), "reached": False,
                "note": (f"The two ends are directly connected through {intf}, "
                         f"and that interface is down. There is no next hop to "
                         f"chase: the link itself is the path, and it is "
                         f"broken.")}

    nodes.append({"label": dst_label, "ip": None,
                  "kind": "dest" if (resolved or up) else "unknown"})
    if resolved or up:
        note = (f"The two ends are directly connected through {intf}: one hop, "
                f"no router in between. ")
        note += ("The ARP entry resolves and the interface is up."
                 if resolved and up else
                 "The ARP entry resolves." if resolved else
                 "The interface is up.")
        return {"nodes": nodes, "line": _line(nodes), "reached": True,
                "note": note}
    nodes[-1] = {"label": "?", "ip": None, "kind": "unknown"}
    return {"nodes": nodes, "line": _line(nodes), "reached": None,
            "note": (f"The two ends are directly connected through {intf}, but "
                     f"nothing here shows the interface up or the neighbour "
                     f"resolved -- check the interface and the ARP entry.")}

def _probe_settled(checks, src_label, dst_label, dest_addr):
    """A path built from a PROBE the deeper checks ran, or None.

    The deeper checks are not limited to reading tables. When one of them pings
    the destination and gets replies -- in a VRF, in an MPLS L3VPN, from a
    different source interface -- that is not evidence about the path, it IS
    the answer: packets went and came back. Reading only the routing output
    ignored the strongest result in the run, and a turn whose own report said
    "3/3 replies" still drew "? unconfirmed".
    """
    target = str(dest_addr or "").strip()
    for check in reversed(checks or []):            # the most recent wins
        cmd = str(check.get("cmd") or "")
        out = str(check.get("output") or "")
        if not re.match(r"^\s*(execute\s+)?ping\b", cmd, re.I):
            continue
        # the command may name the destination by address or by hostname
        named = [n for n in (target, dst_label) if n]
        if named and not any(str(n) in cmd for n in named):
            continue                                # a next-hop ping, not this
        if not check_ok(out) or not usable_output(out, cmd):
            continue
        vrf = re.search(r"\bvrf\s+(\S+)", cmd, re.I)
        context = f" in VRF {vrf.group(1)}" if vrf else ""

        # The hops behind the verdict, from the SAME routing context. A
        # global-table traceroute says nothing about a path that only works
        # inside a VRF: drawing one under the other's verdict would be
        # inventing a route.
        middle, how = _hops_in_context(checks, vrf.group(1) if vrf else "",
                                       dest_addr)
        nodes = ([{"label": src_label, "ip": None, "kind": "source"}]
                 + middle
                 + [{"label": dst_label, "ip": None, "kind": "dest"}])
        note = (f"A ping run during the deeper checks{context} reached the "
                f"destination and got replies. That settles reachability: the "
                f"earlier failure was the wrong routing context, not a broken "
                f"path.")
        if how:
            note += " " + how
        elif not middle:
            note += (" No traceroute was run in that context, so the hops "
                     "between are not shown -- the probe proves it arrives, "
                     "not which way it went.")
        return {"nodes": nodes, "line": _line(nodes), "reached": True,
                "note": note}
    return None


def _hops_in_context(checks, vrf, dest_addr):
    """(hop nodes, how they were established) for one routing context."""
    from agent import vendors

    for check in reversed(checks or []):
        cmd = str(check.get("cmd") or "")
        if not re.match(r"^\s*(traceroute|tracert|tracepath)\b", cmd, re.I):
            continue
        found = re.search(r"\bvrf\s+(\S+)", cmd, re.I)
        if (found.group(1) if found else "") != vrf:
            continue                            # a different table entirely
        hops = vendors.parse_hops(str(check.get("output") or ""))
        nodes, hidden = [], 0
        for hop in hops:
            if hop.get("timeout"):
                hidden += 1
                nodes.append({"label": "hidden hop", "ip": None,
                              "kind": "unknown"})
                continue
            label = str(hop.get("host") or hop.get("ip") or "")
            if not label or label == str(dest_addr):
                continue                        # the destination is drawn once
            nodes.append({"label": label, "ip": None, "kind": "hop"})
        while nodes and nodes[-1]["kind"] == "unknown":
            nodes.pop()                         # trailing silence, after arrival
            hidden -= 1
        if nodes:
            how = "The hops are from a traceroute run in the same context."
            if hidden > 0:
                how += (f" {hidden} of them answered nothing while later hops "
                        f"did -- a device declining to reply, not a break.")
            return nodes, how

    # No trace in that context. The route's own next hop is still more than a
    # bare line, and it is the one thing the checks did establish.
    text = "\n".join(str(c.get("output") or "") for c in (checks or []))
    for pattern in _NEXT_HOP:
        found = pattern.search(text)
        if found:
            egress = (found.group(2) or "").strip() if found.lastindex else ""
            hops = ([{"label": egress, "ip": None, "kind": "intf"}]
                    if egress else [])
            # an ATTACHED route names the destination as its own next hop:
            # adding it here would draw the destination twice, once as a
            # transit hop and once as the end of the path
            if found.group(1) != str(dest_addr):
                hops.append({"label": found.group(1), "ip": None,
                             "kind": "hop"})
            return hops, ("The first hop is the next hop the source's own "
                           "routing named; no traceroute was run in that "
                           "context, so the rest is not shown.")
    return [], ""


def _trace_settled(checks, src_label, dst_label, dest_addr):
    """A path built from a traceroute the deeper checks ran, or None.

    A hop that answers nothing BETWEEN hops that do is a device declining to
    reply -- MPLS transit routinely does -- not a break. Drawing it as the end
    of the path blames a hop that forwarded the packet perfectly well.
    """
    from agent import vendors
    for check in reversed(checks or []):
        cmd = str(check.get("cmd") or "")
        out = str(check.get("output") or "")
        if not re.match(r"^\s*(traceroute|tracert|tracepath)\b", cmd, re.I):
            continue
        hops = vendors.parse_hops(out)
        if not hops:
            continue
        last_answer = max((i for i, h in enumerate(hops)
                           if not h.get("timeout")), default=-1)
        if last_answer < 0:
            continue
        nodes = [{"label": src_label, "ip": None, "kind": "source"}]
        hidden = 0
        for hop in hops[:last_answer + 1]:
            if hop.get("timeout"):
                hidden += 1
                nodes.append({"label": "hidden hop", "ip": None,
                              "kind": "unknown"})
                continue
            label = str(hop.get("host") or hop.get("ip") or "")
            if not label or label == dst_label or label == str(dest_addr):
                continue
            nodes.append({"label": label, "ip": None, "kind": "hop"})

        arrived = bool(dest_addr) and any(
            str(h.get("ip") or h.get("host")) == str(dest_addr)
            for h in hops if not h.get("timeout"))
        trailing = len(hops) - 1 - last_answer
        if arrived:
            nodes.append({"label": dst_label, "ip": None, "kind": "dest"})
            reached = True
        elif trailing:
            nodes.append({"label": "?", "ip": None, "kind": "unknown"})
            reached = None
        else:
            nodes.append({"label": "?", "ip": None, "kind": "unknown"})
            reached = None

        note = "From a traceroute the deeper checks ran"
        vrf = re.search(r"\bvrf\s+(\S+)", cmd, re.I)
        if vrf:
            note += f", inside VRF {vrf.group(1)}"
        note += ". "
        if hidden:
            note += (f"{hidden} hop(s) answered nothing while later ones did: "
                     f"that is a device declining to reply, not a break. ")
        if not arrived and trailing:
            note += ("It stops before the destination answers, so what happens "
                     "past the last replying hop is unsettled.")
        elif arrived:
            note += "The destination answered."
        return {"nodes": nodes, "line": _line(nodes), "reached": reached,
                "note": note.strip()}
    return None


def path_from_checks(checks, src_label="source", dst_label="destination",
                     dest_addr=""):
    """What the deeper checks established about the route.

    In order of what each kind of evidence is worth: a probe that reached the
    destination settles it; a traceroute the checks ran shows the route they
    took; and failing both, the source's own forwarding table says what it
    INTENDS to do next, which is the question a trace full of stars leaves
    open.
    """
    # A Tufin or CMDB reply is a JSON blob full of addresses, and reading one
    # as a forwarding decision the source made would invent a hop nobody saw.
    # Those rows are shown, but they say nothing about the route.
    checks = [c for c in (checks or []) if c.get("kind") != "system"]

    settled = _probe_settled(checks, src_label, dst_label, dest_addr)
    if settled:
        return settled
    # two ends on the same subnet have no next hop to chase: the link is the
    # path, and drawing a next-hop chain for it would be inventing a router
    linked = _connected_path(checks, src_label, dst_label, dest_addr)
    if linked:
        return linked
    traced = _trace_settled(checks, src_label, dst_label, dest_addr)
    if traced:
        return traced

    text = "\n".join(str(c.get("output") or "") for c in (checks or []))
    if not text.strip():
        return None

    # the address the commands ran against: its own address is not a hop
    src_addr = next((str(c.get("device") or "") for c in checks
                     if re.match(r"^\d{1,3}(\.\d{1,3}){3}$",
                                 str(c.get("device") or ""))), "")

    # Read the next hop out of the route to the DESTINATION, not out of
    # whichever prefix the table happened to print first. "show ip route vrf
    # default" is the whole table; the first entry in one real run was the
    # source's own /32, and the path drew the source forwarding to itself.
    scoped = route_for(text, dest_addr)
    if scoped == "":
        # prefixes were recognised and not one of them covers the destination
        nodes = [{"label": src_label, "ip": None, "kind": "source"},
                 {"label": "X", "ip": None, "kind": "dead",
                  "why": "no route to the destination in any table checked"}]
        return {"nodes": nodes, "line": _line(nodes), "reached": False,
                "note": ("Every routing table these checks read was searched "
                         "and none of them has a route that covers the "
                         "destination -- not even a default. Traffic to it is "
                         "dropped at the source.")}

    next_hop, egress = pick_next_hop(scoped if scoped else text, src_addr)
    if not next_hop:
        return None

    unresolved = bool(_ARP_INCOMPLETE.search(text))
    down = [m.group(1) for m in _IF_DOWN.finditer(text)]
    # the route said which interface, or the only interface reported down is
    # the one being asked about: IOS-XR's routing block names no interface at
    # all, so without this the fault is invisible on exactly one platform
    if not egress and len(down) == 1:
        egress = down[0]
    broken = bool(egress) and any(
        egress.lower() in d.lower() or d.lower() in egress.lower() for d in down)

    # The interface the traffic leaves by is a step in the path, not a footnote
    # on the next hop: "source -> Ethernet1/54 -> next hop" is how an engineer
    # draws it, and the interface is usually the thing that turns out to be
    # down.
    nodes = [{"label": src_label, "ip": None, "kind": "source"}]
    if egress:
        nodes.append({"label": egress, "ip": None, "kind": "intf"})

    # An ATTACHED route names the destination as its own next hop. Drawing that
    # as a transit hop and then adding "?" says the traffic reaches the
    # destination and then something unknown happens to it, which cannot be
    # true: there is nothing after the destination.
    if dest_addr and next_hop == str(dest_addr):
        blocked = blockage(text, next_hop, egress)
        if unresolved or broken:
            nodes.append({"label": "X", "ip": None, "kind": "dead",
                          "why": blocked})
            return {"nodes": nodes, "line": _line(nodes), "reached": False,
                    "note": (f"The destination is directly attached out "
                             f"{egress or 'this interface'} -- it is its own "
                             f"next hop, so there is no router in between. "
                             f"{blocked[:1].upper() + blocked[1:]}."
                             if blocked else
                             "The destination is directly attached, and the "
                             "link to it is down.")}
        reached = bool(_ARP_RESOLVED.search(text) or _IF_UP.search(text))
        nodes.append({"label": dst_label, "ip": None,
                      "kind": "dest" if reached else "unknown"})
        return {"nodes": nodes, "line": _line(nodes),
                "reached": True if reached else None,
                "note": (f"The destination is directly attached out "
                         f"{egress or 'this interface'}: it is its own next "
                         f"hop, so the path is one link with no router in "
                         f"between. "
                         + ("The interface is up and the neighbour resolves."
                            if reached else
                            "Nothing here shows that link up or the neighbour "
                            "resolved -- check the interface and the address "
                            "resolution entry."))}

    nodes.append({"label": next_hop, "ip": None, "kind": "hop"})

    # Absence of evidence is not evidence: a route pointing somewhere says
    # what the source INTENDS, not that anything arrives. Only an ARP entry
    # with a real address behind it, or an interface explicitly up, says the
    # next hop is actually there. Without one of those this ends in "?" --
    # drawing it through to the destination would contradict the run that
    # produced it, which is how a panel loses an operator's trust for good.
    confirmed = bool(_ARP_RESOLVED.search(text) or _IF_UP.search(text))

    if unresolved or broken:
        nodes.append({"label": "X", "ip": None, "kind": "dead",
                      "why": blockage(text, next_hop, egress)})
        why = []
        if broken:
            why.append(f"{egress} is down")
        if unresolved:
            why.append(f"{next_hop} never answered ARP")
        note = ("The source forwards toward " + next_hop
                + (f" out {egress}" if egress else "")
                + ", but " + " and ".join(why)
                + " -- so nothing leaves here, whatever the policy allows.")
        reached = False
    elif confirmed:
        nodes.append({"label": dst_label, "ip": None, "kind": "dest"})
        note = ("The source's forwarding entry for the destination is complete: "
                "next hop " + next_hop + (f" out {egress}" if egress else "")
                + ", resolved and up.")
        reached = True
    else:
        nodes.append({"label": "?", "ip": None, "kind": "unknown"})
        note = ("The source forwards toward " + next_hop
                + (f" out {egress}" if egress else "")
                + ". Nothing in these checks confirms or denies what happens "
                  "after that hop -- to settle it, look at the next hop itself.")
        reached = None

    return {"nodes": nodes, "line": _line(nodes),
            "reached": reached, "note": note}
