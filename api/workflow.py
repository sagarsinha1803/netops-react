"""The workflow panel's state, ported from the Chainlit UI minus Chainlit.

Pure state: the WebSocket layer decides when to push a snapshot. No polling
route, no element re-mounting, no publish bookkeeping -- the React client gets
the whole snapshot on every change and renders it in place, which is the entire
reason for leaving Chainlit.
"""
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
            # record (dict, so its text starts with "{"); a miss is the plain
            # "No data found ..." / "Error ..." sentence the CMDB returns. A
            # green tick for a lookup that found nothing is a lie -- mark it.
            found = [k for k, v in devices.items()
                     if str(v).strip().startswith("{")]
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


_FAILED_OUTPUT = re.compile(
    r"^\s*(REJECTED|\[error|error:)"
    r"|unknown tool"
    r"|success rate is 0 percent"
    r"|100% packet loss"
    r"|request timed out"
    r"|destination (host|net) unreachable"
    r"|% (network|destination) .*(not|unreachable)",
    re.I)


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
        verdict = str(data.get("verdict") or "UNKNOWN").upper()
        rules = data.get("blocking_rules") or []
        acl = str(rules[0].get("acl") or "") if rules else ""
        return verdict, acl
    verdict = next((v for v in ("BLOCKED", "ALLOWED") if v in text.upper()),
                   "UNKNOWN")
    m = re.search(r"['\"]acl['\"]\s*:\s*['\"]([^'\"]+)['\"]", text)
    return verdict, (m.group(1) if m else "")
