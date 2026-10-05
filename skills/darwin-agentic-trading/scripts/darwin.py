#!/usr/bin/env python3
"""
darwin.py: the official Darwin Finance agent skill helper.

Pairs an AI agent with its user's Darwin account (RFC 8628 device flow), keeps
the agent's API key in the operating system's secret store, and adds that key
to later API calls, so the key never appears in the conversation.

    python3 darwin.py pair start --client-name "Claude Code"
    python3 darwin.py pair wait
    python3 darwin.py pair reconnect --client-name "Claude Code"
    python3 darwin.py import ~/Downloads/darwin-agent-key-my-agent.json
    python3 darwin.py call GET /api/agent/v1/grant
    python3 darwin.py status [--check]
    python3 darwin.py forget (--agent-id ID | --pending | --all)

Every command prints exactly ONE line of JSON on stdout (`--help` and
`--version` are the only exceptions: they print plain text).

Security properties (each one is tested in tests/test_darwin.py):
  * Talks only to https://darwin.finance and https://beta.darwin.finance, on
    the default port, under /api/agent/. Redirects are refused, never followed.
  * A key is bound to the realm that issued it and is only ever sent there.
  * The key and the pairing device code are never printed, logged, put in a
    file the helper does not control, or passed on any command line. Every
    string the helper prints is scrubbed of token-shaped values as well.
  * The secret store is checked (write, read back, delete a probe) BEFORE a
    pairing starts, because Darwin hands the key over exactly once.
  * Non-GET calls are never retried automatically.

Stdlib only. Python 3.8+. Official source: https://github.com/DarwinFinance/agent-skills
"""
import argparse
import base64
import http.client as _httpclient
import json
import os
import re
import secrets
import shutil
import ssl
import stat
import subprocess
import sys
import time
import unicodedata
import urllib.error
import urllib.request

__version__ = "1.0.0"

# ── Realms ────────────────────────────────────────────────────────────────────
REALM_HOSTS = {"prod": "darwin.finance", "beta": "beta.darwin.finance"}
HOST_TO_REALM = {v: k for k, v in REALM_HOSTS.items()}

SERVICE = "finance.darwin.agent-skill"
PAIR_PATH = "/api/agent/v1/pair"
TOKEN_PATH = "/api/agent/v1/pair/token"
HELLO_PATH = "/api/agent/v1/hello"
GRANT_PATH = "/api/agent/v1/grant"

DEVICE_CODE_PREFIX = "darwinAI_pair_"
KEY_RE = re.compile(r"^(?:darwinAI_agent_|agt_)[A-Za-z0-9_-]{20,200}$")
DEVICE_CODE_RE = re.compile(r"^darwinAI_pair_[A-Za-z0-9_-]{40,114}$")
USER_CODE_RE = re.compile(r"^[BCDFGHJKLMNPQRSTVWXZ]{4}-[BCDFGHJKLMNPQRSTVWXZ]{4}$")
AGENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
API_PATH_RE = re.compile(r"/api/agent/[A-Za-z0-9/_.~-]*")
API_QUERY_RE = re.compile(r"[A-Za-z0-9_.~%:,=&+-]*")

PAIR_MAX_LIFETIME_SECS = 25 * 60
DEFAULT_WAIT_SECS = 480
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
KEY_FILE_MAX_BYTES = 4096
HTTP_TIMEOUT = 30

# Anything shaped like a Darwin credential or device code is scrubbed from
# every string this program prints, whatever its source.
_SECRET_SHAPE = re.compile(r"(darwinAI_(?:agent|pair|api|mcp)_|agt_|mbt_|mcpt_)[A-Za-z0-9_-]{8,}")
_KNOWN_SECRETS = set()


class HelperError(Exception):
    """An expected failure: printed as {"status":"error","error":code,...}."""

    def __init__(self, code, detail="", **extra):
        Exception.__init__(self, code)
        self.code = code
        self.detail = detail
        self.extra = extra


# ── Output ────────────────────────────────────────────────────────────────────
def remember_secret(value):
    if isinstance(value, str) and len(value) >= 8:
        _KNOWN_SECRETS.add(value)


def redact(text):
    if not isinstance(text, str):
        return text
    for s in _KNOWN_SECRETS:
        if s in text:
            text = text.replace(s, "[REDACTED]")
    return _SECRET_SHAPE.sub(lambda m: m.group(1) + "[REDACTED]", text)


def redact_obj(obj):
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, list):
        return [redact_obj(v) for v in obj]
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            lk = str(k).lower()
            if lk in ("access_token", "device_code", "authorization"):
                out[redact(str(k))] = "[REDACTED]"
            else:
                out[redact(str(k))] = redact_obj(v)
        return out
    return obj


def emit(obj, stream=None):
    stream = stream or sys.stdout
    stream.write(json.dumps(redact_obj(obj), ensure_ascii=False, separators=(",", ":")) + "\n")
    stream.flush()


# ── Client name (mirrors the server's sanitizeClientName) ─────────────────────
_CLIENT_NAME_ALLOWED = re.compile(r"^[A-Za-z0-9 .\-_()'+/:#@,]+$")


def _fold_accents(s):
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def brand_skeleton(name):
    s = _fold_accents(name).lower()
    s = re.sub(r"[l1|!]", "i", s)
    return re.sub(r"[^a-z]", "", s)


def validate_client_name(raw):
    if not isinstance(raw, str) or not raw.strip():
        raise HelperError("invalid_client_name", "Pass --client-name with your own app or agent name, e.g. \"Claude Code\".")
    if len(raw) > 1000:
        raise HelperError("invalid_client_name", "client name is too long (max 60 characters).")
    cleaned = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", raw)).strip()
    if len(cleaned) > 60:
        raise HelperError("invalid_client_name", "client name is too long (max 60 characters).")
    if not _CLIENT_NAME_ALLOWED.match(_fold_accents(cleaned)):
        raise HelperError(
            "invalid_client_name",
            "client name may use only Latin letters, digits, spaces and . - _ ( ) ' + / : # @ ,",
        )
    if "darwin" in brand_skeleton(cleaned):
        raise HelperError(
            "client_name_impersonates_darwin",
            "Darwin refuses client names that contain \"Darwin\". Use your own app name, e.g. \"Claude Code\".",
        )
    return cleaned


def ua_host(name):
    s = re.sub(r"[^A-Za-z0-9 ._-]", "", name or "").strip()[:40]
    return s or "unknown"


_VENDORS = (
    ("claude", ("claude", "anthropic")),
    ("chatgpt", ("codex", "chatgpt", "openai", "gpt")),
    ("gemini", ("gemini", "google")),
    ("grok", ("grok", "xai")),
    ("cursor", ("cursor",)),
    ("copilot", ("copilot",)),
    ("windsurf", ("windsurf", "codeium")),
    ("cline", ("cline",)),
    ("ollama", ("ollama",)),
)


def client_vendor(name):
    low = (name or "").lower()
    for vendor, needles in _VENDORS:
        if any(n in low for n in needles):
            return vendor
    return "custom"


def host_agent_name(fallback=None):
    env = os.environ.get("DARWIN_HOST_AGENT")
    return env if env else (fallback or "unknown")


# ── HTTP ──────────────────────────────────────────────────────────────────────
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        raise HelperError("redirect_refused", "Darwin answered with a redirect; this helper never follows one.", http_status=code)


def build_opener():
    ctx = ssl.create_default_context()
    return urllib.request.build_opener(_NoRedirect(), urllib.request.HTTPSHandler(context=ctx))


_OPENER = None


def opener():
    global _OPENER
    if _OPENER is None:
        _OPENER = build_opener()
    return _OPENER


def realm_origin(realm):
    host = REALM_HOSTS.get(realm)
    if not host:
        raise HelperError("invalid_realm", "realm must be prod or beta")
    return "https://" + host


def parse_realm(value):
    if value is None:
        return None
    v = value.strip().lower()
    if v in REALM_HOSTS:
        return v
    v = re.sub(r"^https://", "", v).rstrip("/")
    if v in HOST_TO_REALM:
        return HOST_TO_REALM[v]
    raise HelperError("invalid_realm", "realm must be prod (darwin.finance) or beta (beta.darwin.finance)")


def check_api_path(path):
    """`/api/agent/<segments>[?query]`. No percent-encoding in the path at all (so no
    encoded or double-encoded dot or slash), no dot segments, no controls."""
    bad = HelperError("invalid_path", "path must start with /api/agent/ (for example /api/agent/v1/grant)")
    if not isinstance(path, str) or len(path) > 2048:
        raise bad
    route, _, query = path.partition("?")
    if not API_PATH_RE.fullmatch(route) or "//" in route:
        raise bad
    if any(seg in (".", "..") for seg in route.split("/")):
        raise bad
    if query and (not API_QUERY_RE.fullmatch(query) or "%25" in query.lower()):
        raise bad
    return path


def http(realm, method, path, *, body=None, key=None, host_agent=None, accept_status=None):
    """One request to one realm. Returns (status, parsed_json). Never follows redirects."""
    url = realm_origin(realm) + check_api_path(path)
    headers = {
        "User-Agent": "darwin-agent-skill/%s (%s)" % (__version__, ua_host(host_agent)),
        "Accept": "application/json",
        "X-Darwin-Client": "%s; harness=darwin-agent-skill; version=%s" % (client_vendor(host_agent), __version__),
    }
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if key is not None:
        remember_secret(key)
        headers["Authorization"] = "Bearer " + key
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        resp = opener().open(req, timeout=HTTP_TIMEOUT)
        status = resp.status if hasattr(resp, "status") else resp.getcode()
    except HelperError:
        raise
    except urllib.error.HTTPError as e:
        resp = e
        status = e.code
        if 300 <= status < 400:
            raise HelperError("redirect_refused", "Darwin answered with a redirect; this helper never follows one.", http_status=status)
    except (urllib.error.URLError, OSError, ssl.SSLError, _httpclient.HTTPException) as e:
        raise HelperError("network_error", "Could not reach %s (%s)." % (REALM_HOSTS[realm], type(e).__name__))
    try:
        ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        raw = resp.read(MAX_RESPONSE_BYTES + 1)
    except (OSError, ValueError, _httpclient.HTTPException) as e:
        raise HelperError("network_error", "The connection to %s dropped (%s)." % (REALM_HOSTS[realm], type(e).__name__))
    finally:
        try:
            resp.close()
        except Exception:
            pass
    if ctype not in ("application/json", "application/problem+json"):
        # An invite wall, an error page, a proxy: never parse or print it.
        raise HelperError("unexpected_content_type", "Darwin did not answer with JSON (HTTP %d). Nothing was read." % status, http_status=status)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise HelperError("response_too_large", "Response larger than %d bytes." % MAX_RESPONSE_BYTES, http_status=status)
    try:
        parsed = json.loads(raw.decode("utf-8")) if raw else {}
    except (ValueError, UnicodeDecodeError):
        raise HelperError("invalid_json", "Darwin's answer was not valid JSON (HTTP %d)." % status, http_status=status)
    return status, parsed


# ── State directory (no secrets except the pending device code) ───────────────
def state_dir():
    override = os.environ.get("DARWIN_SKILL_STATE_DIR")
    if override:
        return override
    if sys.platform == "darwin":
        return os.path.join(os.path.expanduser("~"), "Library", "Application Support", "darwin-agent-skill")
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
        return os.path.join(base, "darwin-agent-skill")
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return os.path.join(base, "darwin-agent-skill")


def _uid():
    return os.getuid() if hasattr(os, "getuid") else None


_REPARSE = 0x400  # FILE_ATTRIBUTE_REPARSE_POINT (Windows symlinks and junctions)


def _is_link(st):
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & _REPARSE)


def _judge(path, st, owners, allow_sticky):
    if st.st_uid not in owners:
        raise HelperError("state_dir_unsafe", "%s is owned by another user; refusing to use it." % path)
    if stat.S_ISDIR(st.st_mode) and st.st_mode & 0o022 and not (allow_sticky and st.st_mode & stat.S_ISVTX):
        raise HelperError("state_dir_unsafe", "%s is writable by other users; refusing to use it." % path)


def walk_safely(path, owners, allow_sticky=True, max_hops=40):
    """Resolve `path` one component at a time, following every symlink hop ourselves.
    Every directory traversed and every symlink met (and so every hop's ancestors) must
    be owned by one of `owners` and not writable by others (sticky dirs like /tmp allowed
    when `allow_sticky`). Missing components end the walk: what does not exist yet will be
    created by us, inside an already-judged directory. Returns the resolved path."""
    # NOT abspath(): it would collapse `..` lexically, before symlinks are resolved.
    raw = path if os.path.isabs(path) else os.path.join(os.getcwd(), path)
    pending = [c for c in raw.split(os.sep) if c]
    cur = os.sep
    _judge(cur, os.lstat(cur), owners, allow_sticky)
    hops = 0
    while pending:
        comp = pending.pop(0)
        if comp == ".":
            continue
        if comp == "..":
            cur = os.path.dirname(cur) or os.sep
            continue
        nxt = os.path.join(cur, comp)
        try:
            st = os.lstat(nxt)
        except FileNotFoundError:
            if any(c in (".", "..") for c in pending):
                raise HelperError("state_dir_unsafe", "%s: `..` after a missing directory; give a plain path." % path)
            return os.path.join(nxt, *pending) if pending else nxt
        except OSError:
            raise HelperError("state_dir_unsafe", "Cannot inspect %s; refusing to use it." % nxt)
        _judge(nxt, st, owners, allow_sticky)
        if stat.S_ISLNK(st.st_mode):
            hops += 1
            if hops > max_hops:
                raise HelperError("state_dir_unsafe", "Too many symlinks under %s." % path)
            target = os.readlink(nxt)
            if os.path.isabs(target):
                cur = os.sep
            pending = [c for c in target.split(os.sep) if c] + pending
            continue
        cur = nxt
    return cur


def check_ancestors(path):
    """POSIX: every directory and symlink hop leading to `path`'s parent is ours or root's.
    Returns `path` re-rooted on its validated, fully resolved parent (use THAT path)."""
    name = os.path.basename(path.rstrip(os.sep))
    if name in ("", ".", ".."):
        raise HelperError("state_dir_unsafe", "%s is not a usable directory name." % path)
    uid = _uid()
    if uid is None:
        return os.path.abspath(path)
    raw = path if os.path.isabs(path) else os.path.join(os.getcwd(), path)
    parent = walk_safely(os.path.dirname(raw.rstrip(os.sep)), (uid, 0))
    return os.path.join(parent, name)


def ensure_private_dir(path):
    """Create (0700) or verify a directory: not a symlink/junction, ours, private, safe ancestors.
    Returns the validated, resolved path; callers use it for every later operation."""
    path = check_ancestors(path)
    try:
        os.makedirs(path, mode=0o700, exist_ok=True)
    except OSError as e:
        raise HelperError("state_dir_unusable", "Cannot create %s (%s)." % (path, type(e).__name__))
    # Validate AGAIN now that every component exists: a parent someone else created between
    # the first check and makedirs is caught here (it is theirs, or writable by others), and a
    # chain that passes now cannot be changed later by anyone but us or root.
    uid = _uid()
    if uid is not None and walk_safely(path, (uid, 0)) != path:
        raise HelperError("state_dir_unsafe", "%s changed while it was being created; refusing to use it." % path)
    st = os.lstat(path)
    if _is_link(st) or not stat.S_ISDIR(st.st_mode):
        raise HelperError("state_dir_unsafe", "%s is a symlink or not a directory; refusing to use it." % path)
    uid = _uid()
    if uid is not None:
        if st.st_uid != uid:
            raise HelperError("state_dir_unsafe", "%s is owned by another user; refusing to use it." % path)
        if st.st_mode & 0o077:
            os.chmod(path, 0o700)
    return path


def _check_target(path):
    """An existing state file we are about to read or replace: regular, ours, not a link."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    uid = _uid()
    if _is_link(st):
        raise HelperError("state_file_unsafe", "%s is a symlink; refusing to use it." % path)
    if not stat.S_ISREG(st.st_mode) or (uid is not None and st.st_uid != uid):
        raise HelperError("state_file_unsafe", "%s is not a private file owned by you; refusing to use it." % path)
    return st


_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_BINARY = getattr(os, "O_BINARY", 0)


def write_private_json(path, obj):
    """Atomically replace `path` with obj, through an exclusively-created 0600 temp file."""
    d = os.path.dirname(path)
    tmp = os.path.join(d, ".%s.%d.%s.tmp" % (os.path.basename(path), os.getpid(), secrets.token_hex(4)))
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW | _O_BINARY, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(json.dumps(obj).encode("utf-8"))
            f.flush()
            os.fsync(f.fileno())
        _check_target(path)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_private_json(path, max_bytes=65536):
    """Read a file only if it is a regular, private file owned by us. Missing → None."""
    if _check_target(path) is None:
        return None
    try:
        fd = os.open(path, os.O_RDONLY | _O_NOFOLLOW | _O_BINARY)
    except FileNotFoundError:
        return None
    except OSError:
        if os.path.islink(path):
            raise HelperError("state_file_unsafe", "%s is a symlink; refusing to use it." % path)
        raise HelperError("state_file_unreadable", "Cannot read %s." % path)
    try:
        st = os.fstat(fd)
        uid = _uid()
        if not stat.S_ISREG(st.st_mode):
            raise HelperError("state_file_unsafe", "%s is not a regular file." % path)
        if uid is not None and (st.st_uid != uid or st.st_mode & 0o077):
            raise HelperError("state_file_unsafe", "%s is not a private file owned by you; refusing to use it." % path)
        with os.fdopen(fd, "rb") as f:
            fd = None
            raw = f.read(max_bytes + 1)
    finally:
        if fd is not None:
            os.close(fd)
    if len(raw) > max_bytes:
        raise HelperError("state_file_unsafe", "%s is unexpectedly large." % path)
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise HelperError("state_file_corrupt", "%s is not valid JSON; run `forget --pending` or `forget --all`." % path)


def remove_quietly(path):
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


class StateLock(object):
    """An exclusive, non-blocking lock on the state directory."""

    def __init__(self, directory):
        self.path = os.path.join(directory, ".lock")
        self.fd = None

    def __enter__(self):
        _check_target(self.path)
        self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT | _O_NOFOLLOW | _O_BINARY, 0o600)
        st = os.fstat(self.fd)
        uid = _uid()
        if not stat.S_ISREG(st.st_mode) or (uid is not None and (st.st_uid != uid or st.st_mode & 0o077)):
            os.close(self.fd)
            self.fd = None
            raise HelperError("state_file_unsafe", "%s is not a private lock file owned by you." % self.path)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self.fd)
            self.fd = None
            raise HelperError("busy", "Another darwin.py command is running (probably `pair wait`). Wait for it to finish.")
        return self

    def __exit__(self, *exc):
        if self.fd is not None:
            try:
                if os.name == "nt":
                    import msvcrt

                    try:
                        os.lseek(self.fd, 0, 0)
                        msvcrt.locking(self.fd, msvcrt.LK_UNLCK, 1)
                    except OSError:
                        pass
                else:
                    import fcntl

                    fcntl.flock(self.fd, fcntl.LOCK_UN)
            finally:
                os.close(self.fd)
                self.fd = None
        return False


class State(object):
    def __init__(self):
        self.dir = ensure_private_dir(state_dir())
        self.pending_path = os.path.join(self.dir, "pending.json")
        self.index_path = os.path.join(self.dir, "keys.json")

    def lock(self):
        return StateLock(self.dir)

    def pending(self):
        p = read_private_json(self.pending_path)
        if p is None:
            return None
        if not isinstance(p, dict) or p.get("v") != 1 or not DEVICE_CODE_RE.match(str(p.get("device_code", ""))):
            raise HelperError("state_file_corrupt", "The pending pairing file is malformed; run `forget --pending`.")
        remember_secret(p["device_code"])
        return p

    def save_pending(self, p):
        write_private_json(self.pending_path, p)

    def clear_pending(self):
        remove_quietly(self.pending_path)

    def index(self):
        idx = read_private_json(self.index_path)
        if idx is None:
            return []
        if not isinstance(idx, dict) or not isinstance(idx.get("keys"), list):
            raise HelperError("state_file_corrupt", "The key index is malformed; run `forget --all`.")
        return [e for e in idx["keys"] if isinstance(e, dict) and e.get("realm") in REALM_HOSTS and AGENT_ID_RE.match(str(e.get("agent_id", "")))]

    def save_index(self, entries):
        write_private_json(self.index_path, {"v": 1, "keys": entries})


def account_name(realm, agent_id):
    return "%s:%s" % (REALM_HOSTS[realm], agent_id)


# ── Secret stores ─────────────────────────────────────────────────────────────
class StoreError(Exception):
    pass


class MacKeychain(object):
    name = "macos-keychain"
    description = "the macOS Keychain"

    def __init__(self, service=SERVICE):
        import ctypes
        import ctypes.util

        self.ct = ctypes
        self.service = service
        sec = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/Security.framework/Security")
        cf = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        self.sec, self.cf = sec, cf
        vp = ctypes.c_void_p
        cf.CFStringCreateWithCString.restype = vp
        cf.CFStringCreateWithCString.argtypes = [vp, ctypes.c_char_p, ctypes.c_uint32]
        cf.CFDataCreate.restype = vp
        cf.CFDataCreate.argtypes = [vp, ctypes.c_char_p, ctypes.c_long]
        cf.CFDictionaryCreate.restype = vp
        cf.CFDictionaryCreate.argtypes = [vp, ctypes.POINTER(vp), ctypes.POINTER(vp), ctypes.c_long, vp, vp]
        cf.CFDataGetLength.restype = ctypes.c_long
        cf.CFDataGetLength.argtypes = [vp]
        cf.CFDataGetBytePtr.restype = vp
        cf.CFDataGetBytePtr.argtypes = [vp]
        cf.CFRelease.restype = None
        cf.CFRelease.argtypes = [vp]
        for fn in ("SecItemAdd", "SecItemCopyMatching"):
            getattr(sec, fn).restype = ctypes.c_int32
            getattr(sec, fn).argtypes = [vp, ctypes.POINTER(vp)]
        sec.SecItemDelete.restype = ctypes.c_int32
        sec.SecItemDelete.argtypes = [vp]
        sec.SecItemUpdate.restype = ctypes.c_int32
        sec.SecItemUpdate.argtypes = [vp, vp]

        def const(lib, name):
            return vp.in_dll(lib, name).value

        self.k = {n: const(sec, n) for n in (
            "kSecClass", "kSecClassGenericPassword", "kSecAttrService", "kSecAttrAccount", "kSecAttrLabel",
            "kSecValueData", "kSecReturnData", "kSecMatchLimit", "kSecMatchLimitOne",
        )}
        self.k["kCFBooleanTrue"] = const(cf, "kCFBooleanTrue")
        self.key_cb = ctypes.addressof(ctypes.c_byte.in_dll(cf, "kCFTypeDictionaryKeyCallBacks"))
        self.val_cb = ctypes.addressof(ctypes.c_byte.in_dll(cf, "kCFTypeDictionaryValueCallBacks"))

    def _str(self, s):
        return self.cf.CFStringCreateWithCString(None, s.encode("utf-8"), 0x08000100)

    def _dict(self, pairs):
        ct = self.ct
        n = len(pairs)
        keys = (ct.c_void_p * n)(*[k for k, _ in pairs])
        vals = (ct.c_void_p * n)(*[v for _, v in pairs])
        return self.cf.CFDictionaryCreate(None, keys, vals, n, self.key_cb, self.val_cb)

    def _query(self, account, extra):
        owned = [self._str(self.service), self._str(account)]
        k = self.k
        pairs = [(k["kSecClass"], k["kSecClassGenericPassword"]), (k["kSecAttrService"], owned[0]), (k["kSecAttrAccount"], owned[1])]
        pairs += extra
        return self._dict(pairs), owned

    def _release(self, objs):
        for o in objs:
            if o:
                self.cf.CFRelease(o)

    def put(self, account, secret):
        """Add, or replace in place (never delete-then-add: a failed add would lose the old key)."""
        data_bytes = secret.encode("utf-8")
        data = self.cf.CFDataCreate(None, data_bytes, len(data_bytes))
        label = self._str("Darwin agent key (%s)" % account)
        q, owned = self._query(account, [(self.k["kSecValueData"], data), (self.k["kSecAttrLabel"], label)])
        try:
            rc = self.sec.SecItemAdd(q, None)
            if rc == -25299:  # errSecDuplicateItem: update the existing item's data
                match, owned2 = self._query(account, [])
                attrs = self._dict([(self.k["kSecValueData"], data)])
                try:
                    rc = self.sec.SecItemUpdate(match, attrs)
                finally:
                    self._release([match, attrs] + owned2)
        finally:
            self._release([q, data, label] + owned)
        if rc != 0:
            raise StoreError("Keychain write failed (%d)" % rc)

    def get(self, account):
        ct = self.ct
        k = self.k
        q, owned = self._query(account, [(k["kSecReturnData"], k["kCFBooleanTrue"]), (k["kSecMatchLimit"], k["kSecMatchLimitOne"])])
        out = ct.c_void_p()
        try:
            rc = self.sec.SecItemCopyMatching(q, ct.byref(out))
        finally:
            self._release([q] + owned)
        if rc == -25300:  # errSecItemNotFound
            return None
        if rc != 0 or not out.value:
            raise StoreError("SecItemCopyMatching failed (%d)" % rc)
        try:
            n = self.cf.CFDataGetLength(out.value)
            ptr = self.cf.CFDataGetBytePtr(out.value)
            return ct.string_at(ptr, n).decode("utf-8")
        finally:
            self.cf.CFRelease(out.value)

    def delete(self, account):
        q, owned = self._query(account, [])
        try:
            rc = self.sec.SecItemDelete(q)
        finally:
            self._release([q] + owned)
        if rc not in (0, -25300):
            raise StoreError("SecItemDelete failed (%d)" % rc)


class WindowsCredentials(object):
    name = "windows-credential-manager"
    description = "Windows Credential Manager"

    def __init__(self, service=SERVICE):
        import ctypes
        from ctypes import wintypes

        self.ct = ctypes
        self.service = service

        class FILETIME(ctypes.Structure):
            _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

        class CREDENTIALW(ctypes.Structure):
            _fields_ = [
                ("Flags", wintypes.DWORD), ("Type", wintypes.DWORD), ("TargetName", wintypes.LPWSTR),
                ("Comment", wintypes.LPWSTR), ("LastWritten", FILETIME), ("CredentialBlobSize", wintypes.DWORD),
                ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)), ("Persist", wintypes.DWORD),
                ("AttributeCount", wintypes.DWORD), ("Attributes", ctypes.c_void_p),
                ("TargetAlias", wintypes.LPWSTR), ("UserName", wintypes.LPWSTR),
            ]

        self.CRED = CREDENTIALW
        adv = ctypes.WinDLL("advapi32", use_last_error=True)
        adv.CredWriteW.argtypes = [ctypes.POINTER(CREDENTIALW), wintypes.DWORD]
        adv.CredWriteW.restype = wintypes.BOOL
        adv.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.POINTER(CREDENTIALW))]
        adv.CredReadW.restype = wintypes.BOOL
        adv.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        adv.CredDeleteW.restype = wintypes.BOOL
        adv.CredFree.argtypes = [ctypes.c_void_p]
        adv.CredFree.restype = None
        self.adv = adv

    def _target(self, account):
        return "%s:%s" % (self.service, account)

    def put(self, account, secret):
        ct = self.ct
        blob = secret.encode("utf-8")
        buf = (ct.c_ubyte * len(blob)).from_buffer_copy(blob)
        cred = self.CRED()
        cred.Type = 1  # CRED_TYPE_GENERIC
        cred.TargetName = self._target(account)
        cred.CredentialBlobSize = len(blob)
        cred.CredentialBlob = ct.cast(buf, ct.POINTER(ct.c_ubyte))
        cred.Persist = 2  # CRED_PERSIST_LOCAL_MACHINE
        cred.UserName = account
        if not self.adv.CredWriteW(ct.byref(cred), 0):
            raise StoreError("CredWriteW failed (%d)" % ct.get_last_error())

    def get(self, account):
        ct = self.ct
        p = ct.POINTER(self.CRED)()
        if not self.adv.CredReadW(self._target(account), 1, 0, ct.byref(p)):
            err = ct.get_last_error()
            if err == 1168:  # ERROR_NOT_FOUND
                return None
            raise StoreError("CredReadW failed (%d)" % err)
        try:
            c = p.contents
            return ct.string_at(c.CredentialBlob, c.CredentialBlobSize).decode("utf-8")
        finally:
            self.adv.CredFree(p)

    def delete(self, account):
        ct = self.ct
        if not self.adv.CredDeleteW(self._target(account), 1, 0):
            err = ct.get_last_error()
            if err != 1168:
                raise StoreError("CredDeleteW failed (%d)" % err)


class LinuxSecretTool(object):
    """libsecret through `secret-tool`. The secret only ever crosses a pipe (stdin/stdout)."""

    name = "linux-secret-service"
    description = "the Linux Secret Service (secret-tool)"

    def __init__(self, service=SERVICE):
        # Only a system-installed binary: a secret-tool found through a writable PATH
        # entry would receive the key.
        self.exe = None
        for cand in ("/usr/bin/secret-tool", "/bin/secret-tool", "/usr/local/bin/secret-tool"):
            try:
                # Every directory and symlink hop on the way, and the binary itself, must be
                # root's and not writable by anyone else (no sticky exception here).
                real = walk_safely(cand, (0,), allow_sticky=False)
                st = os.stat(real)
            except (OSError, HelperError):
                continue
            if stat.S_ISREG(st.st_mode) and st.st_uid == 0 and not st.st_mode & 0o022:
                self.exe = real
                break
        if not self.exe:
            raise StoreError("no root-owned secret-tool in /usr/bin, /bin or /usr/local/bin")
        self.service = service

    def _run(self, args, stdin=None):
        try:
            return subprocess.run([self.exe] + args, input=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise StoreError("secret-tool failed (%s)" % type(e).__name__)

    def put(self, account, secret):
        r = self._run(["store", "--label=Darwin agent key (%s)" % account, "service", self.service, "account", account], stdin=secret.encode("utf-8"))
        if r.returncode != 0:
            raise StoreError("secret-tool store failed (%d)" % r.returncode)

    def get(self, account):
        r = self._run(["lookup", "service", self.service, "account", account])
        if r.returncode != 0 or not r.stdout:
            return None
        return r.stdout.decode("utf-8").rstrip("\n")

    def delete(self, account):
        self._run(["clear", "service", self.service, "account", account])


def _tmpfs_mounts():
    try:
        with open("/proc/mounts", "r") as f:
            lines = f.read().splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        parts = line.split()
        if len(parts) >= 3:
            out.append((parts[1].replace("\\040", " "), parts[2]))
    return out


def is_ram_backed(path):
    """True when the deepest mount containing `path` is tmpfs/ramfs (Linux)."""
    real = os.path.realpath(path)
    best, fstype = "", None
    for mnt, fs in _tmpfs_mounts():
        if (real == mnt or real.startswith(mnt.rstrip("/") + "/")) and len(mnt) > len(best):
            best, fstype = mnt, fs
    return fstype in ("tmpfs", "ramfs")


class RamFileStore(object):
    """Memory-only fallback: owner-only files on a RAM-backed tmpfs, gone at reboot."""

    name = "memory-only"
    description = "memory only (a RAM-backed file, lost at reboot); no OS secret store is available"

    def __init__(self, base=None):
        if os.name == "nt" or sys.platform == "darwin":
            raise StoreError("no RAM-backed directory on this platform")
        candidates = [base] if base else [os.environ.get("XDG_RUNTIME_DIR"), "/dev/shm"]
        chosen = None
        for c in candidates:
            if c and os.path.isdir(c) and is_ram_backed(c):
                chosen = c
                break
        if not chosen:
            raise StoreError("no RAM-backed (tmpfs) directory found")
        self.dir = ensure_private_dir(os.path.join(chosen, "darwin-agent-skill-%s" % (_uid() if _uid() is not None else "u")))

    def _path(self, account):
        return os.path.join(self.dir, base64.urlsafe_b64encode(account.encode("utf-8")).decode("ascii").rstrip("=") + ".key")

    def put(self, account, secret):
        write_private_json(self._path(account), {"v": 1, "secret": secret})

    def get(self, account):
        try:
            data = read_private_json(self._path(account), max_bytes=4096)
        except HelperError as e:
            raise StoreError(e.code)
        if not isinstance(data, dict) or not isinstance(data.get("secret"), str):
            return None
        return data["secret"]

    def delete(self, account):
        remove_quietly(self._path(account))


_BACKEND_FACTORIES = None  # tests replace this with a list of callables


def backend_factories():
    if _BACKEND_FACTORIES is not None:
        return list(_BACKEND_FACTORIES)
    if sys.platform == "darwin":
        return [MacKeychain]
    if os.name == "nt":
        return [WindowsCredentials]
    return [LinuxSecretTool, RamFileStore]


def preflight(backend):
    """Write, read back and delete a probe secret. Raises StoreError on any mismatch."""
    account = "probe:%s" % secrets.token_hex(6)
    value = "probe_" + secrets.token_urlsafe(24)
    try:
        backend.put(account, value)
        if backend.get(account) != value:
            raise StoreError("read-back mismatch")
    finally:
        try:
            backend.delete(account)
        except Exception:
            pass
    if backend.get(account) is not None:
        raise StoreError("delete did not remove the probe")


def choose_backend():
    """The first secret store that passes the probe, plus why the others failed."""
    reasons = []
    for factory in backend_factories():
        try:
            b = factory()
            preflight(b)
            return b
        except Exception as e:  # any backend failure just means "try the next"
            reasons.append("%s: %s" % (getattr(factory, "name", getattr(factory, "__name__", "store")), redact(str(e))[:120]))
    raise HelperError(
        "no_secret_store",
        "No usable secret store: nothing was started, so no key can be lost. "
        "Tried: %s. On Linux install libsecret's `secret-tool` with a running keyring, "
        "or run where /dev/shm (tmpfs) is writable." % "; ".join(reasons or ["none"]),
    )


def backend_by_name(name):
    for factory in backend_factories():
        if getattr(factory, "name", None) == name:
            try:
                return factory()
            except Exception as e:
                raise HelperError("secret_store_unavailable", "%s is not available now (%s)." % (name, redact(str(e))[:120]))
    raise HelperError("secret_store_unavailable", "The secret store %s is not available on this machine." % name)


# ── Key index ─────────────────────────────────────────────────────────────────
def safe_text(value, cap=80):
    """Server- or file-supplied text that may be printed or persisted: no controls,
    capped, and never anything secret-shaped (a hostile server could echo the key)."""
    if not isinstance(value, str):
        return None
    v = re.sub(r"[\x00-\x1f\x7f]", " ", value)[:cap]
    for sec in _KNOWN_SECRETS:
        if sec in value:
            return "[REDACTED]"
    return redact(v)


def record_key(state, realm, agent_id, agent_name, backend, host_agent, source):
    agent_name = safe_text(agent_name, 80)
    entries = [e for e in state.index() if not (e["realm"] == realm and e["agent_id"] == agent_id)]
    entries.append({
        "realm": realm, "agent_id": agent_id, "agent": agent_name, "stored_in": backend.name,
        "host_agent": ua_host(host_agent), "source": source, "stored_at": int(time.time()),
    })
    state.save_index(entries)


def store_key(backend, realm, agent_id, key):
    account = account_name(realm, agent_id)
    backend.put(account, key)
    if backend.get(account) != key:
        raise StoreError("read-back mismatch")


def select_entry(state, realm=None, agent_id=None):
    entries = state.index()
    if realm:
        entries = [e for e in entries if e["realm"] == realm]
    if agent_id:
        entries = [e for e in entries if e["agent_id"] == agent_id]
    if not entries:
        raise HelperError("no_key", "No stored Darwin key matches. Pair first: `pair start --client-name \"<your app>\"`.")
    if len(entries) > 1:
        raise HelperError("ambiguous_key", "Several keys are stored; pass --agent-id (and --realm). See `status`.",
                          agents=[{"realm": e["realm"], "agent_id": e["agent_id"], "agent": e.get("agent")} for e in entries])
    return entries[0]


def load_key(entry):
    backend = backend_by_name(entry.get("stored_in", ""))
    try:
        key = backend.get(account_name(entry["realm"], entry["agent_id"]))
    except Exception as e:
        raise HelperError("secret_store_unavailable", "Could not read the key from %s (%s)." % (backend.description, redact(str(e))[:120]))
    if not key or not KEY_RE.match(key):
        raise HelperError("key_missing", "The stored key is gone (memory-only keys vanish at reboot). Pair again or import a key file.")
    remember_secret(key)
    return key


def safe_agent_id(value):
    """An agent id is an identifier, never anything secret-shaped (a hostile answer could
    put the key there, and ids go into keys.json and secret-store account names)."""
    if not isinstance(value, str) or not AGENT_ID_RE.match(value):
        return None
    if _SECRET_SHAPE.search(value) or any(sec in value or value in sec for sec in _KNOWN_SECRETS if len(value) >= 8):
        return None
    return value


def agent_id_from_url(url, realm):
    if not isinstance(url, str):
        return None
    prefix = realm_origin(realm) + "/agent-account/"
    if not url.startswith(prefix):
        return None
    tail = url[len(prefix):].split("?")[0].split("#")[0].strip("/")
    return safe_agent_id(tail)


# A Solana public key in base58: 32-44 characters, no 0, O, I or l.
_SOLANA_ADDRESS_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
# Darwin's welcome opens with the same lead-in; used only when there is no welcome to show.
ADDRESS_LEAD_IN = "Your Darwin agent's Solana address (send any Solana-based asset to fund the account either from Darwin's UI or from somewhere else):"
SHOW_WELCOME = ("Show `welcome` to your user verbatim, before anything else. It starts with the agent's Solana address "
                "alone in a code block: keep the address inside that code block exactly as sent (never reformat it inline) "
                "so your user's chat app shows a copy button next to it.")
AFTER_SETUP = ("After that, say at most one or two short sentences: where the key lives, and an invitation to explore "
               "their agent's page on Darwin, naming the agent by its full name and linking its page from the welcome. "
               "Do not list limits, grant ids, internal ids, setup notes or API details unless your user asks. "
               "Then wait for your user's next instruction.")
SHOW_ADDRESS_BLOCK = ("There is no `welcome` to show, so show your user `address_block` verbatim first, keeping the address "
                      "inside its code block (never inline) so their chat app shows a copy button next to it.")


_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _b58_len(value):
    """Decoded byte length of a base58 string (alphabet already checked)."""
    n = 0
    for ch in value:
        n = n * 58 + _B58.index(ch)
    body = (n.bit_length() + 7) // 8
    return (len(value) - len(value.lstrip("1"))) + body


def safe_solana_address(value):
    """A server-supplied wallet address that may be shown: a Solana public key (base58 decoding to
    exactly 32 bytes) and nothing else."""
    if not isinstance(value, str) or not _SOLANA_ADDRESS_RE.fullmatch(value) or _b58_len(value) != 32:
        return None
    if any(sec in value or value in sec for sec in _KNOWN_SECRETS):
        return None
    return value


def address_block(address):
    """The lead-in, a blank line, then the address ALONE in a fenced code block (chat UIs add a copy button)."""
    return "%s\n\n```\n%s\n```" % (ADDRESS_LEAD_IN, address)


def say_hello(realm, key, host_agent):
    """GET /hello: returns (welcome or None, agent name or None, Solana address or None, error or None).
    Failures are reported, not fatal."""
    try:
        status, body = http(realm, "GET", HELLO_PATH, key=key, host_agent=host_agent)
    except HelperError as e:
        return None, None, None, e.code
    if status != 200 or not isinstance(body, dict):
        return None, None, None, "hello_http_%d" % status
    welcome = body.get("welcome") if isinstance(body.get("welcome"), str) else None
    if welcome is not None:
        welcome = "[REDACTED]" if any(sec in welcome for sec in _KNOWN_SECRETS) else redact(welcome[:4000])
    agent = body.get("agent") if isinstance(body.get("agent"), str) else None
    return welcome, agent, safe_solana_address(body.get("solanaAddress")), None


def with_address(out, welcome, address):
    """Adds the agent's Solana address to a command's output: `solana_address`, and, when there is
    no welcome to carry it, `address_block` (the same fenced block Darwin's welcome opens with)."""
    if address:
        out["solana_address"] = address
        if welcome is None:
            out["address_block"] = address_block(address)
    return out


# ── Commands ──────────────────────────────────────────────────────────────────
CLI_HINT = ("The Darwin CLI is installed; you can use `darwin` instead of this helper. "
            "`darwin login --from-skill` moves this key into it.")


def cli_hint():
    """C.70: a one-line pointer to the Darwin CLI when a `darwin` executable is on PATH.

    Never runs it, and never points at a copy inside the current directory tree (a workspace can put
    its own `darwin` first on PATH). The helper keeps working exactly as before either way.
    """
    try:
        found = shutil.which("darwin")
        if not found:
            return None
        real = os.path.realpath(found)
        here = os.path.realpath(os.getcwd())
        if real == here or real.startswith(here + os.sep):
            return None
        return CLI_HINT
    except Exception:
        return None


def cmd_pair_start(args):
    client = validate_client_name(args.client_name)
    realm = parse_realm(args.realm) or "prod"
    mode = "reconnect" if args.reconnect else "new"
    state = State()
    with state.lock():
        backend = choose_backend()  # BEFORE anything is started: the key is handed over once.
        body = {"client_name": client}
        if mode == "reconnect":
            body["mode"] = "reconnect"
        status, resp = http(realm, "POST", PAIR_PATH, body=body, host_agent=host_agent_name(client))
        if not isinstance(resp, dict):
            resp = {}
        if status != 200:
            err = resp.get("error") if isinstance(resp, dict) else None
            detail = resp.get("error_description") if isinstance(resp, dict) else None
            if status == 429 or err == "rate_limited":
                raise HelperError("rate_limited", "Too many pairing requests from this network. Wait a few minutes.")
            raise HelperError(err if isinstance(err, str) and re.match(r"^[a-z_]{1,40}$", err) else "pair_start_failed",
                              detail if isinstance(detail, str) else "Darwin refused the pairing request (HTTP %d)." % status, http_status=status)
        device_code = resp.get("device_code")
        user_code = resp.get("user_code")
        url = resp.get("verification_uri_complete")
        if not isinstance(device_code, str) or not DEVICE_CODE_RE.match(device_code):
            raise HelperError("bad_response", "Darwin's pairing answer had no valid device code.")
        remember_secret(device_code)
        if not isinstance(user_code, str) or not USER_CODE_RE.match(user_code):
            raise HelperError("bad_response", "Darwin's pairing answer had no valid user code.")
        expected = realm_origin(realm) + "/agents/connect?"
        if not isinstance(url, str) or not url.startswith(expected) or len(url) > 300 or any(c in url for c in " \"'<>\\"):
            raise HelperError("bad_response", "Darwin's pairing link was not on %s; not showing it." % REALM_HOSTS[realm])
        if mode == "reconnect" and resp.get("mode") != "reconnect":
            # An older server ignores `mode` and would create a NEW agent instead.
            raise HelperError("reconnect_unsupported",
                              "Darwin did not confirm reconnect mode, so no link is shown. Ask your user to create a new key "
                              "on the agent's Manage tab and use `import`, or pair a new agent with `pair start`.")
        try:
            interval = int(resp.get("interval", 5))
        except (TypeError, ValueError):
            interval = 5
        interval = min(max(interval, 1), 60)
        try:
            expires_in = int(resp.get("expires_in", 600))
        except (TypeError, ValueError):
            expires_in = 600
        now = time.time()
        replaced = state.pending() is not None
        state.save_pending({
            "v": 1, "realm": realm, "mode": mode, "device_code": device_code, "user_code": user_code, "url": url,
            "interval": interval, "next_poll_at": now + interval, "created_at": now,
            "hard_expires_at": now + PAIR_MAX_LIFETIME_SECS + 30, "client_name": client, "backend": backend.name,
            "transport_errors": 0,
        })
    out = {
        "status": "show_user", "url": url, "user_code": user_code, "expires_in": expires_in, "realm": realm, "mode": mode,
        "key_storage": backend.description,
        "next": "Show the user the url and user_code now. Then run `pair wait` (re-run it if it returns still_pending; approval can take up to 25 minutes).",
    }
    hint = cli_hint()
    if hint:
        out["cli"] = hint
    if replaced:
        out["replaced_previous_pairing"] = True
    emit(out)
    return 0


def _lost_hint(p):
    if p.get("mode") == "reconnect":
        # A reconnect key's name carries the pairing's code (server: reconnectKeyLabel).
        return ("If your user had already approved, a new key may have been issued but its delivery was lost. Ask them to revoke "
                "the key whose name starts with \"Reconnect %s\" on the agent's Manage tab (the \"Agent reconnected\" email "
                "names it exactly), then reconnect again. Their other keys are unaffected." % p.get("user_code", ""))
    return ("If your user had already approved, the key may have been issued but its delivery was lost. Ask them to revoke the key "
            "whose name starts with \"Paired: %s\" on the agent's Manage tab (the \"New agent connected\" email names the agent), "
            "then pair again." % p.get("client_name", "agent"))


def cmd_pair_wait(args):
    state = State()
    max_secs = max(5, min(int(args.max_seconds), 540))
    with state.lock():
        p = state.pending()
        if p is None:
            raise HelperError("no_pending", "No pairing is waiting. Start one with `pair start --client-name \"<your app>\"`.")
        realm = p["realm"] if p.get("realm") in REALM_HOSTS else None
        if realm is None:
            raise HelperError("state_file_corrupt", "The pending pairing has no valid realm; run `forget --pending`.")
        backend = backend_by_name(p.get("backend", ""))
        try:
            preflight(backend)
        except Exception as e:
            raise HelperError("secret_store_unavailable", "The secret store failed its check (%s); not collecting the key now. Fix it and re-run `pair wait`." % redact(str(e))[:120])
        host = host_agent_name(p.get("client_name"))
        if p.get("in_flight"):
            # A previous `pair wait` died mid-poll: its answer (maybe the key) was never handled.
            p["transport_errors"] = int(p.get("transport_errors", 0)) + 1
            p["in_flight"] = False
            state.save_pending(p)
        deadline = time.time() + max_secs
        while True:
            now = time.time()
            if now >= p.get("hard_expires_at", 0):
                state.clear_pending()
                out = {"status": "expired", "detail": "The pairing code expired. Start again with `pair start` if your user still wants to connect."}
                if p.get("transport_errors"):
                    out = {"status": "lost", "detail": _lost_hint(p)}
                emit(out)
                return 0
            wait_for = max(0.0, p.get("next_poll_at", now) - now)
            if now + wait_for >= deadline:
                state.save_pending(p)
                emit({"status": "still_pending", "user_code": p["user_code"], "url": p["url"],
                      "polls": int(p.get("polls", 0)), "last_answer": p.get("last_answer"),
                      "next": "Your user has not answered yet. Run `pair wait` again."})
                return 0
            if wait_for:
                time.sleep(wait_for)
            p["in_flight"] = True
            state.save_pending(p)
            try:
                status, resp = http(realm, "POST", TOKEN_PATH, body={"device_code": p["device_code"]}, host_agent=host)
            except HelperError as e:
                p["in_flight"] = False
                if e.code in ("network_error", "unexpected_content_type", "invalid_json", "response_too_large"):
                    p["transport_errors"] = int(p.get("transport_errors", 0)) + 1
                    p["next_poll_at"] = time.time() + min(60, p["interval"] * 2)
                    state.save_pending(p)
                    continue
                raise
            if status == 200:
                return _finish_pairing(state, p, realm, backend, host, resp if isinstance(resp, dict) else {})
            p["in_flight"] = False
            if not isinstance(resp, dict):
                resp = {}
            p["polls"] = int(p.get("polls", 0)) + 1
            err = resp.get("error")
            p["last_answer"] = err if isinstance(err, str) and re.match(r"^[a-z_]{1,40}$", err) else ("ok" if status == 200 else "http_%d" % status)
            if err == "authorization_pending":
                p["next_poll_at"] = time.time() + p["interval"]
            elif err == "slow_down":
                p["interval"] = min(p["interval"] + 5, 60)
                p["next_poll_at"] = time.time() + p["interval"]
            elif err == "expired_token":
                state.clear_pending()
                out = {"status": "expired", "detail": "The pairing code expired or was already used. Start again with `pair start` if your user still wants to connect."}
                if p.get("transport_errors"):
                    out["status"] = "lost"
                    out["detail"] = _lost_hint(p)
                emit(out)
                return 0
            elif err == "access_denied":
                state.clear_pending()
                emit({"status": "denied", "detail": safe_text(resp.get("error_description"), 400) or "Your user declined. Do not retry on your own; ask your user what they want to do."})
                return 0
            elif status == 429 or err == "rate_limited":
                p["next_poll_at"] = time.time() + 60
            elif status >= 500 or err == "temporarily_unavailable":
                p["next_poll_at"] = time.time() + min(30, p["interval"] * 2)
            else:
                state.clear_pending()
                raise HelperError(err if isinstance(err, str) and re.match(r"^[a-z_]{1,40}$", err) else "pair_wait_failed",
                                  "Darwin answered HTTP %d; the pairing was abandoned." % status, http_status=status)
            state.save_pending(p)


def _finish_pairing(state, p, realm, backend, host, resp):
    """The key is in memory only. The pending file (marked in_flight) is kept until the key
    is durably stored, so a crash here is reported as `lost` by the next `pair wait`."""
    token = resp.get("access_token")
    if isinstance(token, str):
        remember_secret(token)
    key_name = safe_text(resp.get("key_name"), 80) or "Paired: %s" % p.get("client_name", "agent")
    agent = resp.get("agent") if isinstance(resp.get("agent"), dict) else {}
    agent_id = agent_id_from_url(agent.get("agentPageUrl"), realm)
    manage = agent.get("manageUrl") if isinstance(agent.get("manageUrl"), str) else None
    if not isinstance(token, str) or not KEY_RE.match(token) or not agent_id:
        state.clear_pending()
        raise HelperError("bad_response", "Darwin's key answer was malformed; nothing was stored. Ask your user to revoke the key named "
                          "\"%s\" on the agent's Manage tab, then pair again." % key_name, manage_url=manage)
    try:
        store_key(backend, realm, agent_id, token)
    except Exception:
        state.clear_pending()
        raise HelperError("store_failed_after_issue",
                          "The key was issued but could not be stored, and it is NOT shown here. Ask your user to revoke the key named "
                          "\"%s\" on the agent's Manage tab, then pair again." % key_name, manage_url=manage)
    agent_name = safe_text(agent.get("name"), 80)
    record_key(state, realm, agent_id, agent_name, backend, host, "pairing")
    state.clear_pending()  # only now: the key is stored and indexed
    welcome, hello_agent, hello_address, hello_err = say_hello(realm, token, host)
    hello_agent = safe_text(hello_agent, 80)
    # The key answer's own address first (it names the agent this key was minted for), else /hello's.
    address = safe_solana_address(agent.get("solanaAddress")) or hello_address
    show = SHOW_WELCOME if welcome is not None else (SHOW_ADDRESS_BLOCK if address else "")
    out = {"status": "paired", "agent": agent_name or hello_agent, "agent_id": agent_id, "realm": realm, "mode": p.get("mode", "new"),
           "stored_in": backend.description, "key_name": key_name, "welcome": welcome,
           "next": ("%s %s Before your first order, run `call GET /api/agent/v1/grant` and read %s/agents/docs/llms.txt." % (
               show, AFTER_SETUP, realm_origin(realm))).strip()}
    with_address(out, welcome, address)
    if hello_err:
        out["hello_error"] = hello_err
    emit(out)
    return 0


_HAS_DIRFD = os.open in getattr(os, "supports_dir_fd", set()) and os.unlink in getattr(os, "supports_dir_fd", set())


class KeyFile(object):
    """A key file opened through a handle on its PARENT directory (POSIX), so the file that
    is later deleted is exactly the one that was read, even if a path component is swapped."""

    def __init__(self, path):
        self.path = path
        self.name = os.path.basename(path)
        self.dfd = None
        self.identity = None

    def close(self):
        if self.dfd is not None:
            os.close(self.dfd)
            self.dfd = None

    def _lstat(self):
        if self.dfd is not None:
            return os.stat(self.name, dir_fd=self.dfd, follow_symlinks=False)
        return os.lstat(self.path)

    def read(self):
        if _HAS_DIRFD:
            try:
                self.dfd = os.open(os.path.dirname(self.path) or ".", os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            except OSError:
                raise HelperError("key_file_unreadable", "Cannot open the folder holding that key file.")
        try:
            st = self._lstat()
        except OSError:
            raise HelperError("key_file_unreadable", "Cannot find that key file.")
        if _is_link(st):
            raise HelperError("key_file_unsafe", "The key file is a symlink; refusing to read it.")
        if not stat.S_ISREG(st.st_mode):
            raise HelperError("key_file_unsafe", "The key file is not a regular file.")
        uid = _uid()
        if uid is not None and st.st_uid != uid:
            raise HelperError("key_file_unsafe", "The key file is owned by another user; refusing to read it.")
        if st.st_size > KEY_FILE_MAX_BYTES:
            raise HelperError("key_file_invalid", "That is not a Darwin key file (too large).")
        try:
            flags = os.O_RDONLY | _O_NOFOLLOW | _O_BINARY
            fd = os.open(self.name, flags, dir_fd=self.dfd) if self.dfd is not None else os.open(self.path, flags)
        except OSError:
            raise HelperError("key_file_unreadable", "Cannot open that key file.")
        with os.fdopen(fd, "rb") as f:
            fst = os.fstat(f.fileno())
            if (fst.st_dev, fst.st_ino) != (st.st_dev, st.st_ino):
                raise HelperError("key_file_unsafe", "The key file changed while it was being opened.")
            raw = f.read(KEY_FILE_MAX_BYTES + 1)
        self.identity = (st.st_dev, st.st_ino)
        return raw

    def delete(self):
        """Delete the file read, if that name still names it. Returns None or a reason.
        Never in a folder other users can write to (they could swap the name in between)."""
        try:
            dst = os.fstat(self.dfd) if self.dfd is not None else os.stat(os.path.dirname(self.path) or ".")
            uid = _uid()
            if uid is not None and (dst.st_uid != uid or dst.st_mode & 0o022):
                return "shared_folder"
            st = self._lstat()
            if _is_link(st) or (st.st_dev, st.st_ino) != self.identity:
                return "file_changed"
            if self.dfd is not None:
                os.unlink(self.name, dir_fd=self.dfd)
            else:
                os.unlink(self.path)
            return None
        except OSError as e:
            return type(e).__name__


def _parse_key_file(raw):
    if len(raw) > KEY_FILE_MAX_BYTES:
        raise HelperError("key_file_invalid", "That is not a Darwin key file (too large).")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise HelperError("key_file_invalid", "That is not a Darwin key file (not JSON).")
    # `type(...) is int`: JSON true and 1.0 compare equal to 1 in Python, and are not version 1.
    if not isinstance(data, dict) or data.get("type") != "darwin-agent-key" or type(data.get("version")) is not int or data.get("version") != 1:
        raise HelperError("key_file_invalid", "That is not a Darwin key file (type/version).")
    key = data.get("key")
    if isinstance(key, str):
        remember_secret(key)
    realm_host = data.get("realm")
    if not isinstance(realm_host, str) or realm_host not in HOST_TO_REALM:
        raise HelperError("key_file_invalid", "The key file names an unknown realm; only darwin.finance and beta.darwin.finance are accepted.")
    if not isinstance(key, str) or not KEY_RE.match(key):
        raise HelperError("key_file_invalid", "The key file has no valid Darwin agent key.")
    agent_id = safe_agent_id(data.get("agent_id"))
    if not agent_id:
        raise HelperError("key_file_invalid", "The key file has no valid agent_id.")
    return HOST_TO_REALM[realm_host], agent_id, key


def cmd_import(args):
    kf = KeyFile(os.path.abspath(os.path.expanduser(args.file)))
    try:
        return _import(args, kf)
    finally:
        kf.close()


def _import(args, kf):
    realm, agent_id, key = _parse_key_file(kf.read())
    # 🔴 The file's `realm` is unsigned. It must AGREE with the realm the agent was told
    # to use (prod unless `--realm beta` was passed explicitly), so an edited file can
    # never on its own make the helper send a key somewhere other than intended.
    wanted = parse_realm(args.realm) or "prod"
    if wanted != realm:
        raise HelperError("realm_mismatch", "The key file says %s, but this import is for %s. Nothing was sent. "
                          "If the key really is for %s, re-run with --realm %s." % (REALM_HOSTS[realm], REALM_HOSTS[wanted], REALM_HOSTS[realm], realm))
    host = host_agent_name(args.client_name)
    state = State()
    with state.lock():
        backend = choose_backend()
        status, body = http(realm, "GET", GRANT_PATH, key=key, host_agent=host)
        if status in (401, 403):
            raise HelperError("key_rejected", "Darwin did not accept this key (revoked, stopped, or limits changed). Nothing was stored.", http_status=status)
        if status != 200 or not isinstance(body, dict):
            raise HelperError("key_check_failed", "Could not verify the key (HTTP %d). Nothing was stored." % status, http_status=status)
        grant = body.get("grant") if isinstance(body.get("grant"), dict) else {}
        if grant.get("id") != agent_id:
            raise HelperError("agent_mismatch", "The key belongs to a different agent than the file says. Nothing was stored.")
        try:
            store_key(backend, realm, agent_id, key)
        except Exception as e:
            raise HelperError("store_failed", "Could not store the key in %s (%s). Nothing was imported; the key file was kept." % (backend.description, redact(str(e))[:80]))
        welcome, agent_name, hello_address, hello_err = say_hello(realm, key, host)
        # /grant's own `wallet` (this key's agent) else /hello's.
        address = safe_solana_address(grant.get("wallet")) or hello_address
        record_key(state, realm, agent_id, agent_name, backend, host, "key_file")
    deleted = False
    delete_error = None
    if not args.keep_file:
        delete_error = kf.delete()
        deleted = delete_error is None
    out = {"status": "imported", "agent": agent_name, "agent_id": agent_id, "realm": realm, "stored_in": backend.description,
           "key_file_deleted": deleted, "welcome": welcome,
           "next": ("%s Then run `call GET /api/agent/v1/grant`." % (
               SHOW_WELCOME if welcome is not None else (SHOW_ADDRESS_BLOCK if address else ""))).strip()}
    with_address(out, welcome, address)
    if deleted:
        out["detail"] = "The key file was deleted after import."
    elif args.keep_file:
        out["detail"] = "The key file was kept (--keep-file). Tell your user to delete it when it is no longer needed."
    elif delete_error == "shared_folder":
        out["detail"] = "The key file is in a folder other users can write to, so it was not deleted automatically. Tell your user to delete it."
    else:
        out["detail"] = "Could not delete the key file (%s). Tell your user to delete it." % delete_error
    if hello_err:
        out["hello_error"] = hello_err
    emit(out)
    return 0


_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")


def cmd_call(args):
    method = args.method.upper()
    if method not in _METHODS:
        raise HelperError("invalid_method", "METHOD must be one of %s." % ", ".join(_METHODS))
    path = check_api_path(args.path)
    body = None
    if args.json_file and args.body is not None:
        raise HelperError("invalid_body", "Pass the body inline or with --json-file, not both.")
    raw = args.body
    if args.json_file:
        try:
            with open(os.path.expanduser(args.json_file), "rb") as f:
                raw = f.read(1024 * 1024 + 1).decode("utf-8")
        except (OSError, UnicodeDecodeError):
            raise HelperError("invalid_body", "Cannot read --json-file.")
    if raw is not None:
        try:
            body = json.loads(raw)
        except ValueError:
            raise HelperError("invalid_body", "The body is not valid JSON.")
    if body is not None and method == "GET":
        raise HelperError("invalid_body", "A GET has no body.")
    state = State()
    entry = select_entry(state, parse_realm(args.realm), args.agent_id)
    key = load_key(entry)
    status, resp = http(entry["realm"], method, path, body=body, key=key, host_agent=host_agent_name(entry.get("host_agent")))
    out = {"status": status, "ok": 200 <= status < 300, "realm": entry["realm"], "agent_id": entry["agent_id"], "body": resp}
    if status == 401:
        out["hint"] = ("Darwin no longer accepts this key (revoked, agent stopped, or its limits changed). "
                       "Pair again, reconnect with `pair reconnect`, or import a new key file.")
    if method != "GET" and status >= 500:
        out["hint"] = "Not retried automatically. Check the outcome (see llms.txt for this endpoint's idempotency rules) before retrying."
    emit(out)
    return 0 if out["ok"] else 1


def cmd_status(args):
    state = State()
    entries = state.index()
    p = state.pending()
    keys = []
    for e in entries:
        item = {"realm": e["realm"], "agent_id": e["agent_id"], "agent": e.get("agent"), "stored_in": e.get("stored_in"),
                "source": e.get("source"), "stored_at": e.get("stored_at")}
        if args.check:
            try:
                key = load_key(e)
                code, _ = http(e["realm"], "GET", GRANT_PATH, key=key, host_agent=host_agent_name(e.get("host_agent")))
                item["check"] = "ok" if code == 200 else ("rejected" if code in (401, 403) else "http_%d" % code)
            except HelperError as he:
                item["check"] = he.code
        keys.append(item)
    out = {"status": "ok", "version": __version__, "keys": keys, "pending": None}
    hint = cli_hint()
    if hint:
        out["cli"] = hint
    if p:
        out["pending"] = {"realm": p.get("realm"), "mode": p.get("mode"), "user_code": p.get("user_code"), "url": p.get("url"),
                          "expires_at": int(p.get("hard_expires_at", 0))}
    emit(out)
    return 0


def cmd_forget(args):
    state = State()
    with state.lock():
        removed = []
        if args.pending or args.all:
            if state.pending() is not None or os.path.exists(state.pending_path):
                state.clear_pending()
                removed.append("pending_pairing")
        if args.all or args.agent_id:
            keep = []
            for e in state.index():
                match = args.all or (e["agent_id"] == args.agent_id and (not args.realm or e["realm"] == parse_realm(args.realm)))
                if not match:
                    keep.append(e)
                    continue
                try:
                    backend_by_name(e.get("stored_in", "")).delete(account_name(e["realm"], e["agent_id"]))
                except Exception:
                    pass
                removed.append("%s:%s" % (e["realm"], e["agent_id"]))
            state.save_index(keep)
        if not (args.pending or args.all or args.agent_id):
            raise HelperError("invalid_arguments", "Say what to forget: --agent-id ID, --pending, or --all.")
    emit({"status": "forgotten", "removed": removed,
          "next": "Forgetting only deletes the local copy. Tell your user to revoke the key on the agent's Manage tab if it should stop working."})
    return 0


# ── CLI ───────────────────────────────────────────────────────────────────────
class _Parser(argparse.ArgumentParser):
    def error(self, message):
        emit({"status": "error", "error": "usage", "detail": redact(message)})
        sys.exit(2)


def build_parser():
    p = _Parser(prog="darwin.py", description="Darwin agent skill helper (official). One JSON line per command.")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd")

    pair = sub.add_parser("pair", help="pair with your user's Darwin account")
    psub = pair.add_subparsers(dest="pair_cmd")
    for name in ("start", "reconnect"):
        s = psub.add_parser(name)
        s.add_argument("--client-name", required=True, help="your own app or agent name, e.g. \"Claude Code\"")
        s.add_argument("--realm", default="prod", help="prod (darwin.finance, default) or beta")
        if name == "start":
            s.add_argument("--reconnect", action="store_true", help="give a new key to one of your user's EXISTING agents")
    w = psub.add_parser("wait")
    w.add_argument("--max-seconds", default=DEFAULT_WAIT_SECS, type=int)

    imp = sub.add_parser("import", help="import a key file downloaded from the agent's Manage tab")
    imp.add_argument("file")
    imp.add_argument("--realm", default=None)
    imp.add_argument("--keep-file", action="store_true")
    imp.add_argument("--client-name", default=None, help="your app name (for the User-Agent only)")

    c = sub.add_parser("call", help="call the Darwin agent API with the stored key")
    c.add_argument("method")
    c.add_argument("path")
    c.add_argument("body", nargs="?", default=None)
    c.add_argument("--json-file", default=None)
    c.add_argument("--realm", default=None)
    c.add_argument("--agent-id", default=None)

    st = sub.add_parser("status")
    st.add_argument("--check", action="store_true")

    f = sub.add_parser("forget")
    f.add_argument("--agent-id", default=None)
    f.add_argument("--realm", default=None)
    f.add_argument("--pending", action="store_true")
    f.add_argument("--all", action="store_true")
    return p


def harden_process():
    """Linux: mark the process non-dumpable (no core file, no same-user ptrace attach)."""
    if sys.platform.startswith("linux"):
        try:
            import ctypes

            ctypes.CDLL(None).prctl(4, 0, 0, 0, 0)  # PR_SET_DUMPABLE = 4
        except Exception:
            pass


def main(argv=None):
    harden_process()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.cmd == "pair":
            if args.pair_cmd == "start":
                return cmd_pair_start(args)
            if args.pair_cmd == "reconnect":
                args.reconnect = True
                return cmd_pair_start(args)
            if args.pair_cmd == "wait":
                return cmd_pair_wait(args)
            raise HelperError("usage", "pair start | pair wait | pair reconnect")
        if args.cmd == "import":
            return cmd_import(args)
        if args.cmd == "call":
            return cmd_call(args)
        if args.cmd == "status":
            return cmd_status(args)
        if args.cmd == "forget":
            return cmd_forget(args)
        raise HelperError("usage", "commands: pair start|wait|reconnect, import, call, status, forget")
    except HelperError as e:
        out = {"status": "error", "error": e.code, "detail": e.detail}
        out.update(e.extra)
        emit(out)
        return 1
    except KeyboardInterrupt:
        emit({"status": "error", "error": "interrupted", "detail": "Stopped. A pending pairing can be resumed with `pair wait`."})
        return 130
    except Exception as e:  # never a traceback: it could carry request data
        emit({"status": "error", "error": "internal_error", "detail": "Unexpected %s. Nothing secret was printed." % type(e).__name__})
        return 1


if __name__ == "__main__":
    sys.exit(main())
