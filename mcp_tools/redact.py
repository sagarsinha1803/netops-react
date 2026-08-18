"""Build a known-safe record out of an untrusted API response.

The CMDB sometimes returns credentials, and the key names are not known in
advance. A denylist cannot help with that: anything nobody thought of gets
through. So this works the other way round -- the caller names the handful of
scalars it wants, and NOTHING else is copied. A new field appearing in the API
is absent by construction rather than leaking by default.

Three rules, in order:

  1. only keys the caller asked for are considered;
  2. only scalars are kept -- a nested object is never copied wholesale, which
     is how credentials arrived inside remoteManagement;
  3. anything whose key or value looks like a secret is dropped even if the
     caller asked for it, and never enters the returned dict.

Credentials dropped here never reach the agent process at all, so they cannot
land in the graph state, the checkpoint database, last_prompt.txt or the model.
"""
import re

# Key names that must never be returned, whatever the caller asked for.
_SECRET_KEY = re.compile(
    r"pass(word|wd|phrase|code)?|pwd|secret|token|api[_-]?key|credential|cred\b|"
    r"login|user(name|id)?|acct|account[_-]?id|auth|psk|community|"
    r"priv(ate)?[_-]?key|ssh[_-]?key|enable[_-]?pass|bearer|session|cookie|"
    r"(?:^|[^a-z])pw(?:[^a-z]|$)",     # bare "pw", without matching "power"
    re.I)

# Values that look like key material regardless of what the key is called.
_SECRET_VALUE = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|"
    r"^[A-Za-z0-9+/]{40,}={0,2}$|"          # long base64 blob
    r"^[A-Fa-f0-9]{40,}$",                   # long hex blob
    re.M)

_SCALARS = (str, int, float, bool)
MAX_LEN = 200


# Internal identifiers: database keys, asset ids, correlation ids. Useless to
# the model and a handle onto other systems, so they go too. Anchored, because a
# bare "id" substring also appears in "valid", "invalid" and "width".
_ID_KEY = re.compile(
    r"^id$|_id$|kearid|uuid|guid|serial|assettag", re.I)
# camelCase ids -- kearId, deviceId. Case-SENSITIVE on purpose: folding case
# here would make "valid" and "invalid" match, since both end in "id".
_ID_CAMEL = re.compile(r"[a-z]Id$")


def is_secret_key(key: str) -> bool:
    return bool(_SECRET_KEY.search(str(key)))


def is_id_key(key: str) -> bool:
    text = str(key)
    return bool(_ID_KEY.search(text) or _ID_CAMEL.search(text))


def is_secret_value(value) -> bool:
    return isinstance(value, str) and bool(_SECRET_VALUE.search(value.strip()))


def safe_scalar(key, value):
    """The value if it is a plain, non-secret, non-identifier scalar."""
    if value is None or isinstance(value, bool):
        return value if not (is_secret_key(key) or is_id_key(key)) else None
    if not isinstance(value, _SCALARS):
        return None                     # dicts and lists are never copied
    if is_secret_key(key) or is_id_key(key) or is_secret_value(value):
        return None
    text = str(value)
    return text[:MAX_LEN] if len(text) > MAX_LEN else value


def path(data, *keys, default=None):
    """Follow a nested path, stepping into the first element of any list.

    The CMDB nests almost everything -- brand is {'name': 'Cisco'}, the OS is
    two levels down, remoteManagement is a list of dicts -- so the fields worth
    keeping have to be reached explicitly rather than copied.
    """
    node = data
    for key in keys:
        if isinstance(node, list):
            node = node[0] if node else None
        if not isinstance(node, dict):
            return default
        node = node.get(key)
    if isinstance(node, (dict, list)):
        return default                  # a path must end at a scalar
    value = safe_scalar(keys[-1], node)
    return default if value is None else value


def pick(raw: dict, fields) -> dict:
    """Copy only the named scalar fields, dropping anything secret-looking."""
    if not isinstance(raw, dict):
        return {}
    out = {}
    for field in fields:
        value = safe_scalar(field, raw.get(field))
        if value is not None and value != "":
            out[field] = value
    return out


def find_scalar(raw, keys, want=None):
    """First non-secret scalar found under any of `keys`, at any depth.

    For values that live inside a nested object -- the management address under
    remoteManagement, say -- where the surrounding object must not be copied.
    `want` is an optional predicate the value has to satisfy.
    """
    wanted = {k.lower() for k in keys}

    def walk(node, depth=0):
        if depth > 6:
            return None
        if isinstance(node, dict):
            for key, value in node.items():
                if (str(key).lower() in wanted
                        and (v := safe_scalar(key, value)) is not None
                        and (want is None or want(v))):
                    return v
            for value in node.values():
                if (found := walk(value, depth + 1)) is not None:
                    return found
        elif isinstance(node, list):
            for item in node[:20]:
                if (found := walk(item, depth + 1)) is not None:
                    return found
        return None

    return walk(raw)


_IPV4 = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")


def looks_like_ip(value) -> bool:
    return bool(_IPV4.match(str(value).strip()))
