"""What has actually worked on this estate's platforms, remembered.

The agent works out each command from the platform in front of it, which is
right -- a syntax table written by hand is out of date the day it is written
and only ever covers the vendors whoever wrote it thought of. But it means
every run rediscovers the same dialect from scratch, and a small model
rediscovers it badly: one real run spent five of its steps permuting the same
wrong shape while the box refused each one.

So the agent keeps a notebook. Nobody fills it in. When a command RUNS and
answers, its shape is written down against the platform it ran on; when a
platform refuses one, that is written down too. At the start of the next
investigation the model is handed the shapes this estate has already accepted
for that kind of box, and the ones it has already rejected.

Three things this is deliberately NOT:

  * it is not the model learning. gpt-4o-mini is the same tomorrow. The
    knowledge lives here, outside it, which is why it survives changing the
    model -- and it is yours rather than the vendor's;
  * it is not a curated table. It knows nothing on day one and only ever
    contains commands YOUR devices answered. Evidence, not opinion;
  * it is not a way around the guards. A remembered command goes through the
    read-only allowlist and the human approval card like any other, so nothing
    can enter the notebook that a human did not approve at least once.

What is stored is the SHAPE, never the command: "show ip route <addr> vrf
<name>", not the address it was run against. That makes an entry reusable on
the next device, and keeps addresses out of a file that lives in the repo.
"""
import json
import os
import re
import sys
import threading

# where the notebook lives; one file per estate, and safe to delete
PATH = os.environ.get(
    "NOTEBOOK_PATH",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "data", "command_notebook.json"))

_LOCK = threading.Lock()

# ---- what a command is FOR -------------------------------------------------
# The notebook is useless keyed on the command alone: "this worked" -- for
# WHAT? These are the questions the ladder asks, matched on the noun the
# command reaches for rather than on a label the model was asked to invent.
# A small model labels its own intent badly; the verb it typed is evidence.
# Order matters: the most specific noun wins. "show ip route <addr> vrf mgmt"
# is a ROUTE lookup that happens to name a context, not a request for the list
# of contexts -- and those are two different questions with two different
# answers, so getting it the wrong way round poisons both entries.
_QUESTIONS = (
    ("a probe to an address", r"^\s*(execute\s+)?(ping|trace(route)?|tracert)\b"),
    ("the platform and version", r"\b(version|inventory|system\s+status)\b"),
    ("the forwarding entry", r"\b(cef|fib|forwarding)\b"),
    ("address resolution for a next hop", r"\b(arp|ndp|neighbou?r-cache)\b"),
    ("the control plane for a prefix", r"\b(bgp|ospf|isis|eigrp|rip)\b"),
    ("the route for an address", r"\b(route|routing-table|iproute)\b"),
    ("the neighbour on an interface", r"\b(cdp|lldp|neighbou?rs?)\b"),
    ("interface state", r"\b(interfaces?|port|controller)\b"),
    ("filters on the path", r"\b(access-list|acl|policy|filter|firewall)\b"),
    # last: only a command that reaches for nothing else is asking for the list
    ("the routing contexts", r"\b(vrf|routing-instance|vpn-instance|"
                             r"virtual-router|route-domain|vdom)s?\b"),
)


def question_of(command: str) -> str:
    """Which of the ladder's questions this command was reaching for."""
    c = str(command or "")
    for name, pattern in _QUESTIONS:
        if re.search(pattern, c, re.I):
            return name
    return ""


# ---- the shape, not the command -------------------------------------------
_IPV4 = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?:/\d{1,2})?\b")
_IPV6 = re.compile(r"\b(?:[0-9a-f]{0,4}:){2,7}[0-9a-f]{0,4}(?:/\d{1,3})?\b", re.I)
_MAC = re.compile(r"\b(?:[0-9a-f]{4}\.){2}[0-9a-f]{4}\b"
                  r"|\b(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}\b", re.I)
# an interface name: a word with a digit in it and usually a slash or a dot
_IFACE = re.compile(r"\b[A-Za-z][A-Za-z-]{1,}\d+(?:[/.:]\d+)*\b")


def shape(command: str) -> str:
    """The command with its arguments replaced by what they stood for.

    "show ip route 10.1.1.1 vrf mgmt" and "show ip route 10.9.9.9 vrf prod"
    are one piece of knowledge, not two -- and neither belongs in a file with
    a real address in it.
    """
    c = " ".join(str(command or "").split())
    c = _MAC.sub("<mac>", c)
    c = _IPV6.sub("<addr>", c)
    c = _IPV4.sub("<addr>", c)
    c = _IFACE.sub("<intf>", c)
    # The word after a keyword that takes a NAME is this estate's, not part of
    # the syntax -- a VRF, an ACL, a route-map. Masking it is what makes an
    # entry reusable on the next device, and it is also what keeps anything
    # identifying out of a file somebody might later decide to commit.
    c = re.sub(r"\b(vrf|routing-instance|vpn-instance|vdom|virtual-router|"
               r"route-domain|access-list|acl|route-map|prefix-list|"
               r"community-list|class-map|policy-map|policy|instance)\s+"
               r"(?!all\b|detail\b|brief\b|summary\b)\S+",
               r"\1 <name>", c, flags=re.I)
    return c.strip()


# ---- the platform a command ran on -----------------------------------------
_FIELDS = ("brand", "vendor", "manufacturer", "brandmodel", "model",
           "hardware", "operatingsystem", "os", "ostype", "osversion",
           "version", "softwareversion", "devicetype")


def _from_record(blob) -> dict:
    """vendor / model / os / version out of a CMDB record, whatever its shape."""
    out = {}
    try:
        data = json.loads(str(blob))
    except Exception:                            # noqa: BLE001 -- prose, not JSON
        return out
    if isinstance(data, dict):
        data = data.get("data") if isinstance(data.get("data"), dict) else data
    if not isinstance(data, dict):
        return out
    for key, value in data.items():
        flat = re.sub(r"[^a-z]", "", str(key).lower())
        if flat in _FIELDS and value:
            out[flat] = str(value).strip()
    return out


# What the box says it is, which beats what the CMDB says it is. Each pattern
# is anchored on wording the platform prints about ITSELF, so a passing
# mention of another vendor in a banner cannot claim the box.
_SAYS = (
    ("cisco-nxos", r"\bnx-?os\b|\bnexus\b"),
    ("cisco-iosxr", r"\bios[ -]?xr\b"),
    ("cisco-iosxe", r"\bios[ -]?xe\b"),
    ("cisco-ios", r"\bcisco ios\b|\bcisco internetwork operating system\b"),
    ("arista-eos", r"\barista\b|\beos\b"),
    ("juniper-junos", r"\bjunos\b|\bjuniper\b"),
    ("huawei-vrp", r"\bvrp\b|\bhuawei\b"),
    ("hp-comware", r"\bcomware\b"),
    ("fortinet-fortios", r"\bfortios\b|\bfortigate\b"),
    ("paloalto-panos", r"\bpan-os\b|\bpalo alto\b"),
    ("checkpoint-gaia", r"\bgaia\b|\bcheck ?point\b"),
    ("f5-tmos", r"\btmos\b|\bbig-?ip\b"),
    ("mikrotik-routeros", r"\brouteros\b|\bmikrotik\b"),
    ("linux", r"\blinux\b|\bubuntu\b|\bdebian\b|\bred ?hat\b"),
)

_VERSION = re.compile(
    r"\b(?:version|release)\s*:?\s*v?(\d+\.\d+)", re.I)


def platform_of(record="", version_output="") -> str:
    """The notebook's key for a device: a family and a major version.

    Keyed loosely on purpose. Too precise -- an exact build string -- and the
    notebook never matches anything twice. Too loose -- the vendor alone --
    and it hands NX-OS syntax to an IOS-XR box. Family plus major version is
    the level at which a CLI dialect is actually stable.
    """
    text = str(version_output or "")
    family = ""
    for name, pattern in _SAYS:
        if re.search(pattern, text, re.I):
            family = name
            break

    fields = _from_record(record)
    if not family:
        blob = " ".join(fields.get(k, "") for k in
                        ("brand", "vendor", "manufacturer", "operatingsystem",
                         "os", "ostype", "devicetype"))
        for name, pattern in _SAYS:
            if re.search(pattern, blob, re.I):
                family = name
                break
    if not family:
        return ""

    found = _VERSION.search(text)
    major = found.group(1) if found else ""
    if not major:
        for key in ("osversion", "version", "softwareversion"):
            got = re.match(r"v?(\d+\.\d+)", fields.get(key, ""))
            if got:
                major = got.group(1)
                break
    return f"{family} {major}".strip()


# ---- the notebook itself ---------------------------------------------------
class Notebook:
    """platform -> question -> {"worked": [shape], "refused": [shape]}."""

    def __init__(self, path=None):
        self.path = path or PATH
        self.data = {}
        self.load()

    def load(self):
        try:
            with open(self.path, encoding="utf-8") as fh:
                got = json.load(fh)
            if isinstance(got, dict):
                self.data = got
        except FileNotFoundError:
            self.data = {}
        except Exception as e:                   # noqa: BLE001 -- never fatal
            print(f"[notebook] could not read {self.path}: {e}", file=sys.stderr)
            self.data = {}

    def save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
                json.dump(self.data, fh, indent=2, sort_keys=True)
                fh.write("\n")
            os.replace(tmp, self.path)
        except Exception as e:                   # noqa: BLE001 -- never fatal
            print(f"[notebook] could not write {self.path}: {e}", file=sys.stderr)

    def record(self, platform: str, command: str, worked: bool) -> bool:
        """Write down that this shape did or did not answer on this platform."""
        question = question_of(command)
        form = shape(command)
        if not platform or not question or not form:
            return False
        with _LOCK:
            entry = self.data.setdefault(platform, {}).setdefault(
                question, {"worked": [], "refused": []})
            here, there = ("worked", "refused") if worked else ("refused", "worked")
            # the newest verdict wins: an OS upgrade that changes a syntax
            # shows up as the old shape starting to fail, and this is how the
            # notebook lets go of it
            if form in entry[there]:
                entry[there].remove(form)
            if form in entry[here]:
                return False                     # already known, nothing new
            entry[here].append(form)
            del entry[here][:-6]                 # keep the six most recent
        self.save()
        return True

    def hints(self, platform: str, limit: int = 12) -> str:
        """What to tell the model about this platform, or "".

        Short on purpose. It is a reminder of what this estate has already
        accepted, not a manual -- the model still decides what to ask and why.
        """
        entry = self.data.get(platform) or {}
        if not entry:
            return ""
        lines, refused = [], []
        for question, forms in entry.items():
            for form in (forms.get("worked") or [])[-2:]:
                lines.append(f"  {question}: {form}")
            for form in (forms.get("refused") or [])[-1:]:
                refused.append(f"  {form}")
        if not lines and not refused:
            return ""
        out = [f"COMMANDS THAT HAVE ALREADY WORKED ON {platform.upper()} IN "
               f"THIS ESTATE. Written down from earlier runs on real devices, "
               f"so prefer these shapes -- but they are a starting point, not "
               f"a rule: if one does not fit the question, work the syntax out "
               f"yourself."]
        out.extend(lines[:limit])
        if refused:
            out.append("Refused by this platform before -- do not spend a "
                       "step on them again:")
            out.extend(refused[:limit])
        return "\n".join(out)


class _Disabled(Notebook):
    """Remembers nothing and suggests nothing, for NOTEBOOK=0."""

    def __init__(self):
        self.path, self.data = "", {}

    def record(self, platform, command, worked):
        return False

    def hints(self, platform, limit=12):
        return ""


_SHARED = None
ENABLED = os.environ.get("NOTEBOOK", "1").lower() not in ("0", "false", "no")


def shared() -> Notebook:
    """One notebook per process, loaded on first use."""
    global _SHARED
    if _SHARED is None:
        _SHARED = Notebook() if ENABLED else _Disabled()
    return _SHARED
