"""Reversible IPv4 masking for the clipboard relay.

Real addresses never reach Copilot: every IPv4 literal in the outgoing prompt is
swapped for one in a benchmarking range that does not exist on any network, and
the reply is swapped back before anything is validated or executed.

Two properties make this work with a model in the middle:

  * the stand-ins are REAL-LOOKING addresses, not tokens like DEST_1. Copilot
    markdown-escapes underscores (the same reason _extract_json has to undo
    "get\\_device\\_details"), reformats tokens and writes awkward commands
    around them. Digits and dots survive untouched, and "ping 198.18.0.20
    repeat 3" is natural output for the model.
  * mapping is per /24 with the host octet kept, so two addresses in the same
    real subnet land in the same fake subnet. The escalation checks reason
    about next hops being on a connected subnet, and that survives.

    >>> m = IpMask()
    >>> m.mask("ping 10.10.1.20 via 10.10.1.1")
    'ping 198.18.0.20 via 198.18.0.1'
    >>> m.unmask("ping 198.18.0.20")
    'ping 10.10.1.20'

Limits, stated plainly: this masks ADDRESSES ONLY. Hostnames (APP-SRV-DC1-020,
FW-DC1-EDGE-01), the CMDB owner fields, ACL and VRF names all still go across in
clear, and a hostname usually says more about a device than its address does.
Prefix lengths are preserved rather than remapped, so "172.20.0.0/16" becomes a
/16 around the stand-in -- close enough for the model to see that a route covers
a destination, not a faithful supernet.
"""
import ipaddress
import os
import re
import threading

# RFC 2544 benchmarking range: never routed on the internet, 512 usable /24s.
# Override if 198.18/15 is in use on your estate.
DEFAULT_POOL = os.environ.get("MASK_POOL", "198.18.0.0/15")

# "ip"    -> 198.18.0.20   stand-in addresses (default)
# "label" -> ip4.n0.h20    alphanumeric labels
#
# Labels cannot be confused with a real address and need no pool, but the model
# has to write the command around them: "ping ip4.n0.h20 repeat 3" is not valid
# CLI, and a model that knows ping takes an address may substitute a plausible
# one instead. Whether Copilot copes is an empirical question about Copilot --
# try it before trusting it. The address style asks nothing unusual of the model.
DEFAULT_STYLE = os.environ.get("MASK_STYLE", "ip")

_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")

# MAC addresses in the three forms vendors print: Cisco dotted triplets,
# colon-separated and hyphen-separated. Matched BEFORE IPv6 so a colon MAC is
# never mistaken for an address.
_MAC = re.compile(
    r"\b(?:[0-9A-Fa-f]{4}\.[0-9A-Fa-f]{4}\.[0-9A-Fa-f]{4}"
    r"|[0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5}"
    r"|[0-9A-Fa-f]{2}(?:-[0-9A-Fa-f]{2}){5})\b")

# Deliberately loose: anything hex-and-colon shaped is a candidate, and
# ipaddress decides. A strict IPv6 regex is long, hard to read and still wrong
# at the edges, whereas the parser is right by definition -- and it rejects a
# six-group MAC, which has no "::" and too few groups to be an address.
_IPV6_CANDIDATE = re.compile(
    r"(?<![\w:.])(?=[0-9A-Fa-f]*:)[0-9A-Fa-f:]{2,45}(?:\.\d{1,3}){0,3}"
    r"(?![\w:])")

# RFC 3849 documentation prefix, and the IANA documentation MAC block from
# RFC 7042. Neither exists on a real network.
_V6_PREFIX = "2001:db8"
_MAC_PREFIX = "00005e0053"
# n = subnet index, h = the real host octet. Dots only, no underscores: Copilot
# markdown-escapes underscores, which is why _extract_json has to undo
# "get\\_device\\_details". Matched case-insensitively in case it reformats.
_LABEL = re.compile(r"\bip4\.n(\d+)\.h(\d+)\b", re.I)

# Stand-in prefixes per entity kind. Hyphens only -- see register().
_PREFIX = {
    "host": "node",     # device hostnames
    "acl": "acl",       # ACL / policy names
    "vrf": "vrf",       # VRF names
    "intf": "intf",     # interface names
    "region": "R",      # CMDB region, passed straight back to the SSH tool
    "label": "mpls",    # MPLS labels
    "rd": "rd",         # route distinguishers / route targets
    "term": "id",
}


def _valid(text: str) -> bool:
    try:
        ipaddress.IPv4Address(text)
        return True
    except ValueError:                       # e.g. a version string "1.2.3.400"
        return False


def _passthrough(text: str) -> bool:
    """Addresses that must survive untouched.

    A subnet mask (255.255.255.0) and an ACL wildcard (0.0.0.255) are not hosts,
    and swapping them turns correct output into nonsense -- as does masking the
    0.0.0.0 of a default route.
    """
    return text.startswith("255.") or text.startswith("0.") or text == "0.0.0.0"


class PoolCollision(RuntimeError):
    """A real address lives inside the stand-in pool, so the map is ambiguous."""


class IpMask:
    """Bidirectional /24-preserving address map, stable for the whole session."""

    def __init__(self, pool: str = DEFAULT_POOL, style: str = DEFAULT_STYLE):
        net = ipaddress.ip_network(pool, strict=False)
        self._first = int(net.network_address)
        self._last = int(net.broadcast_address)
        self._pool = str(net)
        self.style = style if style in ("ip", "label") else "ip"
        self._fwd: dict = {}          # real /24 -> fake /24   (as ints)
        self._rev: dict = {}          # fake /24 -> real /24
        self._idx: dict = {}          # real /24 -> subnet index, for labels
        self._by_idx: dict = {}       # subnet index -> real /24
        self._next = self._first
        self._lock = threading.Lock()
        # named things that are not addresses: hostnames, ACLs, VRFs,
        # interfaces, regions, MPLS labels
        self._terms: dict = {}        # real -> stand-in
        self._rterms: dict = {}       # stand-in -> real
        self._v6: dict = {}           # real IPv6 -> stand-in
        self._rv6: dict = {}
        self._v6_nets: dict = {}      # real /64 -> index
        self._v6_of_net: dict = {}    # index -> addresses seen in it
        self._mac: dict = {}          # real MAC (lowered) -> stand-in
        self._rmac: dict = {}
        self._counts: dict = {}       # kind -> how many issued
        self._term_re = None          # rebuilt when a term is added

    # ---- allocation -------------------------------------------------------
    def _fake_net_for(self, real_net: int) -> int:
        known = self._fwd.get(real_net)
        if known is not None:
            return known
        if self._next <= self._last:
            candidate, self._next = self._next, self._next + 256
            self._fwd[real_net] = candidate
            self._rev[candidate] = real_net
            return candidate
        raise RuntimeError(
            f"stand-in pool {self._pool} exhausted after {len(self._fwd)} "
            "subnets - widen MASK_POOL")

    def _index_for(self, real_net: int) -> int:
        known = self._idx.get(real_net)
        if known is not None:
            return known
        idx = len(self._idx)
        self._idx[real_net] = idx
        self._by_idx[idx] = real_net
        return idx

    # ---- one address ------------------------------------------------------
    def mask_ip(self, text: str) -> str:
        if _passthrough(text) or not _valid(text):
            return text
        value = int(ipaddress.IPv4Address(text))
        if self.style == "label":
            real_net, host = value & 0xFFFFFF00, value & 0xFF
            with self._lock:
                idx = self._index_for(real_net)
            return f"ip4.n{idx}.h{host}"
        # A real address inside the pool would be indistinguishable from a
        # stand-in, and unmask would hand it back as the wrong device. Refuse
        # rather than guess -- checked on the value itself, so it does not
        # depend on whether that subnet happens to have been allocated yet.
        if self._first <= value <= self._last:
            raise PoolCollision(
                f"{text} is inside the stand-in pool {self._pool}, so the map "
                "would be ambiguous. Set MASK_POOL to a range your network "
                "does not use.")
        real_net, host = value & 0xFFFFFF00, value & 0xFF
        with self._lock:
            fake_net = self._fake_net_for(real_net)
        return str(ipaddress.IPv4Address(fake_net | host))

    def unmask_ip(self, text: str) -> str:
        if not _valid(text):
            return text
        value = int(ipaddress.IPv4Address(text))
        with self._lock:
            real_net = self._rev.get(value & 0xFFFFFF00)
        if real_net is None:
            # not one of ours: the model invented it. Leave it fake -- it stays
            # unroutable, the reviewer sees it in the approval prompt, and it
            # cannot resolve to one of your devices.
            return text
        return str(ipaddress.IPv4Address(real_net | (value & 0xFF)))

    def unmask_label(self, match) -> str:
        idx, host = int(match.group(1)), int(match.group(2))
        with self._lock:
            real_net = self._by_idx.get(idx)
        if real_net is None or host > 255:
            return match.group(0)          # invented by the model: leave it
        return str(ipaddress.IPv4Address(real_net | host))

    # ---- IPv6 -------------------------------------------------------------
    def mask_v6(self, text: str) -> str:
        """Stand-in inside 2001:db8::/32, keeping same-/64 addresses together.

        The interface identifier is NOT carried over: on many links it is an
        EUI-64 with the MAC embedded, so preserving it would leak the hardware
        address through the address.
        """
        raw = str(text).strip()
        try:
            addr = ipaddress.IPv6Address(raw.split("%")[0])
        except ValueError:
            return text
        if raw in self._v6:
            return self._v6[raw]
        with self._lock:
            if raw in self._v6:
                return self._v6[raw]
            prefix = int(addr) >> 64
            if prefix not in self._v6_nets:
                self._v6_nets[prefix] = len(self._v6_nets)
            net_idx = self._v6_nets[prefix]
            host_idx = sum(1 for v in self._v6_of_net.get(net_idx, []))
            self._v6_of_net.setdefault(net_idx, []).append(raw)
            stand_in = f"{_V6_PREFIX}:{net_idx:x}::{host_idx + 1:x}"
            self._v6[raw] = stand_in
            self._rv6[stand_in] = raw
        return stand_in

    def unmask_v6(self, text: str) -> str:
        raw = str(text).strip()
        hit = self._rv6.get(raw) or self._rv6.get(raw.lower())
        return hit if hit else text

    # ---- MAC addresses ----------------------------------------------------
    def mask_mac(self, text: str) -> str:
        """Stand-in from the documentation OUI, in the format it arrived in."""
        raw = str(text).strip()
        if raw.lower() in self._rmac:
            return text                      # already a stand-in
        key = raw.lower()
        if key in self._mac:
            return self._mac[key]
        with self._lock:
            if key in self._mac:
                return self._mac[key]
            index = len(self._mac) % 256
            digits = f"{_MAC_PREFIX}{index:02x}"
            if "." in raw:
                stand_in = f"{digits[0:4]}.{digits[4:8]}.{digits[8:12]}"
            elif "-" in raw:
                stand_in = "-".join(digits[i:i + 2] for i in range(0, 12, 2)).upper()
            else:
                stand_in = ":".join(digits[i:i + 2] for i in range(0, 12, 2))
            self._mac[key] = stand_in
            self._rmac[stand_in.lower()] = raw
        return stand_in

    def unmask_mac(self, text: str) -> str:
        hit = self._rmac.get(str(text).strip().lower())
        return hit if hit else text

    # ---- named entities ---------------------------------------------------
    def register(self, value, kind: str = "term") -> str:
        """Give a real name a stand-in, or return the one it already has.

        Stand-ins use hyphens and never underscores: Copilot markdown-escapes
        underscores, which is the same reason _extract_json has to undo
        "get\\_device\\_details". Short and distinctive so reversal cannot be
        ambiguous, and so the model copies them through verbatim rather than
        treating them as prose.
        """
        text = str(value or "").strip()
        if not text or len(text) < 2:
            return text
        if text in self._terms:
            return self._terms[text]
        # already a stand-in (a reply quoting one back) -- leave it alone
        if text in self._rterms:
            return text
        with self._lock:
            if text in self._terms:
                return self._terms[text]
            prefix = _PREFIX.get(kind, "id")
            self._counts[kind] = self._counts.get(kind, 0) + 1
            stand_in = f"{prefix}-{self._counts[kind]}"
            self._terms[text] = stand_in
            self._rterms[stand_in] = text
            self._term_re = None
        return stand_in

    def _terms_pattern(self):
        """Longest first, so DC1-EDGE-01 is not half-replaced by DC1."""
        if self._term_re is None and self._terms:
            ordered = sorted(self._terms, key=len, reverse=True)
            self._term_re = re.compile(
                "|".join(re.escape(t) for t in ordered))
        return self._term_re

    def _rterms_pattern(self):
        if not self._rterms:
            return None
        ordered = sorted(self._rterms, key=len, reverse=True)
        return re.compile("|".join(re.escape(t) for t in ordered), re.I)

    def terms(self):
        """Real -> stand-in, for auditing what was substituted."""
        return dict(self._terms)

    # ---- whole documents --------------------------------------------------
    def mask(self, text: str) -> str:
        # MAC first: a colon-separated MAC is hex-and-colon shaped, so letting
        # the IPv6 pass see it first would risk a mis-parse.
        out = _MAC.sub(lambda m: self.mask_mac(m.group(0)), text or "")
        out = _IPV6_CANDIDATE.sub(lambda m: self.mask_v6(m.group(0)), out)
        out = _IPV4.sub(lambda m: self.mask_ip(m.group(0)), out)
        pattern = self._terms_pattern()
        if pattern:
            out = pattern.sub(lambda m: self._terms[m.group(0)], out)
        return out

    def unmask(self, text: str) -> str:
        """Reverse both forms regardless of the current style.

        A conversation can span a style change (the map is process-wide), and a
        reply may quote an older turn, so both are always accepted.
        """
        out = _LABEL.sub(self.unmask_label, text or "")
        out = _MAC.sub(lambda m: self.unmask_mac(m.group(0)), out)
        out = _IPV6_CANDIDATE.sub(lambda m: self.unmask_v6(m.group(0)), out)
        out = _IPV4.sub(lambda m: self.unmask_ip(m.group(0)), out)
        pattern = self._rterms_pattern()
        if pattern:
            # case-insensitively, because a model may retype "acl-2" as "ACL-2"
            out = pattern.sub(lambda m: self._rterms.get(
                m.group(0), self._rterms.get(m.group(0).lower(), m.group(0))), out)
        return out

    # ---- introspection ----------------------------------------------------
    def pairs(self):
        """Real /24 -> stand-in, for auditing what was substituted."""
        if self.style == "label":
            return sorted(
                (str(ipaddress.IPv4Address(net)), f"ip4.n{idx}.h*")
                for net, idx in self._idx.items())
        return sorted(
            (str(ipaddress.IPv4Address(r)), str(ipaddress.IPv4Address(f)))
            for r, f in self._fwd.items())

    def __len__(self):
        return len(self._idx) if self.style == "label" else len(self._fwd)


_SESSION = None
_SESSION_LOCK = threading.Lock()


def session_mask():
    """The process-wide map.

    It cannot live on the ClipboardLLM instance: bind_tools() returns a
    model_copy and build_agent() runs on every message, so a per-instance map
    would be discarded mid-conversation and the same address would get a new
    stand-in on the next turn.
    """
    global _SESSION
    with _SESSION_LOCK:
        if _SESSION is None:
            _SESSION = IpMask()
        return _SESSION


def enabled() -> bool:
    return os.environ.get("MASK_IPS", "1").lower() not in ("0", "false", "no")
