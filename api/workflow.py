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
        self.basics = []          # the CMDB / ping / traceroute of the main run
        self.summary = {}         # ping_ok, path, hops -- parsed from raw CLI
        self.report = None        # final answer, for the Report tab
        self.deep_report = None   # the deeper-checks answer, appended below it
        self.path = {"nodes": [], "line": "", "reached": None}
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
        return {"title": self.title, "steps": steps, "params": self.params,
                "checks": self.checks, "basics": self.basics,
                "summary": self.summary, "report": self.report,
                "deepReport": self.deep_report, "path": self.path,
                "local": self.local, "cmdbMiss": self.cmdb_miss,
                "maxDeep": C.DEEP_MAX_LOOPS}

    # ---- mutation ----------------------------------------------------------
    def reset(self, params=None, scope="path"):
        for k in self.state:
            self.state[k] = {"status": "pending", "detail": ""}
        self.scope = scope
        self.local = False
        self.cmdb_miss = False
        self.path = {"nodes": [], "line": "", "reached": None}
        self.basics = []
        self.checks = []
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
        from agent.constants import POLICY_TOOL_NAMES
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

    def add_check(self, command, device, region=None, thought=""):
        self.checks.append({"cmd": command, "device": device, "region": region,
                            "status": "running", "detail": "", "thought": thought,
                            "output": "", "step": len(self.checks) + 1})
        return len(self.checks) - 1

    def finish_check(self, idx, ok, detail="", output=""):
        if 0 <= idx < len(self.checks):
            self.checks[idx]["status"] = "done" if ok else "failed"
            self.checks[idx]["detail"] = detail
            if output:
                self.checks[idx]["output"] = output[:2000]

    def add_basic(self, command, device=None, region=None, thought="", kind="cmdb"):
        self.basics.append({"cmd": command, "device": device, "region": region,
                            "status": "running", "detail": "", "thought": thought,
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
            if h.get("timeout"):
                died = True
                break
            label = str(h.get("host") or h.get("ip") or f"hop {h.get('n')}")
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

    def from_state(self, state):
        """Sync the timeline with what the graph has actually established."""
        path = self._path_from_state(state)
        if path:
            self.path = path
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


def as_report(text: str):
    """The final answer as structured data for the Report tab."""
    body = str(text or "").strip()
    if not body:
        return None
    if body.startswith("{"):
        try:
            d = json.loads(body)
            if isinstance(d, dict):
                # The relay unwraps {"thought":.., "final":..} itself, but an
                # API model handed the same schema often returns the whole
                # envelope -- and then the raw JSON was shown as the answer.
                if "final" in d:
                    inner = d["final"]
                    return inner if isinstance(inner, dict) else {"text": str(inner)}
                return d
        except Exception:
            pass
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
    r"|100% packet loss"
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
        # drop the device's echo of the command itself
        lines = [ln for ln in lines if command.strip() not in ln]
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
