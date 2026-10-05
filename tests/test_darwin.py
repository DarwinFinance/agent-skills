"""Tests for skills/darwin-agentic-trading/scripts/darwin.py (stdlib unittest only).

Run: python3 -m unittest discover -s tests -v
"""
import contextlib
import http.server
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
from email.message import Message
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", "skills", "darwin-agentic-trading", "scripts", "darwin.py")


def load_module():
    spec = importlib.util.spec_from_file_location("darwin_helper", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


D = load_module()


def read_json(path):
    with open(path) as f:
        return json.load(f)

KEY = "darwinAI_agent_" + "K" * 43
KEY2 = "darwinAI_agent_" + "Q" * 43
DEVICE = "darwinAI_pair_" + "d" * 43
GRANT = "agr_test123"
PROD = "https://darwin.finance"
BETA = "https://beta.darwin.finance"


# ── fakes ─────────────────────────────────────────────────────────────────────
class FakeStore(object):
    name = "fake-store"
    description = "a fake test store"
    data = {}
    fail_put_after = None  # fail every put once this many puts have succeeded
    puts = 0

    def put(self, account, secret):
        if FakeStore.fail_put_after is not None and FakeStore.puts >= FakeStore.fail_put_after:
            raise D.StoreError("disk full")
        FakeStore.puts += 1
        FakeStore.data[account] = secret

    def get(self, account):
        return FakeStore.data.get(account)

    def delete(self, account):
        FakeStore.data.pop(account, None)


class BrokenStore(FakeStore):
    name = "broken-store"

    def put(self, account, secret):
        raise D.StoreError("locked")


class FakeResponse(object):
    def __init__(self, status, body, ctype="application/json"):
        self.status = status
        self.headers = Message()
        if ctype:
            self.headers["Content-Type"] = ctype
        self._body = body if isinstance(body, bytes) else json.dumps(body).encode()

    def read(self, n=-1):
        return self._body if n < 0 else self._body[:n]

    def close(self):
        pass


class FakeOpener(object):
    """Scripted responses keyed by (method, url). Values: list of (status, body[, ctype]) or exceptions."""

    def __init__(self, script):
        self.script = {k: list(v) for k, v in script.items()}
        self.requests = []

    def open(self, req, timeout=None):
        url = req.full_url
        method = req.get_method()
        body = json.loads(req.data.decode()) if req.data else None
        self.requests.append({"method": method, "url": url, "headers": dict(req.header_items()), "body": body})
        queue = self.script.get((method, url))
        if not queue:
            raise AssertionError("unexpected request %s %s" % (method, url))
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, BaseException):
            raise item
        status, payload = item[0], item[1]
        ctype = item[2] if len(item) > 2 else "application/json"
        resp = FakeResponse(status, payload, ctype)
        if status >= 400:
            raise urllib.error.HTTPError(url, status, "err", resp.headers, io.BytesIO(resp._body))
        return resp


class FakeClock(object):
    def __init__(self):
        self.t = 1_800_000_000.0

    def time(self):
        return self.t

    def sleep(self, s):
        self.t += max(0, s)


def pair_ok(mode=None, host=PROD):
    body = {
        "device_code": DEVICE, "user_code": "BCDF-GHJK", "verification_uri": host + "/agents/connect",
        "verification_uri_complete": host + "/agents/connect?code=BCDF-GHJK", "expires_in": 600, "interval": 5,
    }
    if mode:
        body["mode"] = mode
    return (200, body)


ADDR = "3FqARyyLHpV6rbwRbVW3hVTz8vrK49wBWPV4hHAGDzoD"
LEAD_IN = "Your Darwin agent's Solana address (send any Solana-based asset to fund the account either from Darwin's UI or from somewhere else):"
ADDR_BLOCK = LEAD_IN + "\n\n```\n" + ADDR + "\n```"


def token_ok(host=PROD):
    return (200, {"access_token": KEY, "token_type": "Bearer", "expires_at": None, "expires_in": None,
                  "agent": {"name": "my-agent", "solanaAddress": ADDR, "agentPageUrl": host + "/agent-account/" + GRANT,
                            "manageUrl": host + "/agent-account/" + GRANT + "/manage"},
                  "key_name": "Paired: Claude Code"})


HELLO = (200, {"welcome": "Welcome! You are trading as my-agent.", "ok": True, "agent": "my-agent"})


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.state_dir = os.path.join(self.tmp, "state")
        self.env = mock.patch.dict(os.environ, {"DARWIN_SKILL_STATE_DIR": self.state_dir}, clear=False)
        self.env.start()
        os.environ.pop("DARWIN_HOST_AGENT", None)
        FakeStore.data = {}
        FakeStore.fail_put_after = None
        FakeStore.puts = 0
        D._BACKEND_FACTORIES = [FakeStore]
        self.clock = FakeClock()
        self.time_patch = mock.patch.object(D, "time", self.clock)
        self.time_patch.start()
        D._KNOWN_SECRETS.clear()
        self.outputs = []

    def tearDown(self):
        self.time_patch.stop()
        self.env.stop()
        D._OPENER = None
        D._BACKEND_FACTORIES = None
        shutil.rmtree(self.tmp, ignore_errors=True)
        # 🔴 Global invariant: no secret ever reached stdout or stderr in any test.
        for out, err in self.outputs:
            for secret in (KEY, KEY2, DEVICE, "K" * 20, "d" * 20):
                self.assertNotIn(secret, out)
                self.assertNotIn(secret, err)

    def use(self, script):
        self.opener = FakeOpener(script)
        D._OPENER = self.opener
        return self.opener

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        code = None
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = D.main(list(argv))
            except SystemExit as e:
                code = e.code
        self.outputs.append((out.getvalue(), err.getvalue()))
        lines = [l for l in out.getvalue().splitlines() if l.strip()]
        self.assertEqual(len(lines), 1, "exactly one JSON line expected, got %r" % out.getvalue())
        return code, json.loads(lines[0])

    def start(self, **kw):
        self.use({("POST", PROD + "/api/agent/v1/pair"): [pair_ok()]})
        code, out = self.run_cli("pair", "start", "--client-name", "Claude Code")
        self.assertEqual(out["status"], "show_user", out)
        return out


# ── pair start ────────────────────────────────────────────────────────────────
class PairStart(Base):
    def test_start_returns_link_and_code_and_stores_pending_privately(self):
        out = self.start()
        self.assertEqual(out["url"], PROD + "/agents/connect?code=BCDF-GHJK")
        self.assertEqual(out["user_code"], "BCDF-GHJK")
        self.assertEqual(out["realm"], "prod")
        req = self.opener.requests[0]
        self.assertEqual(req["body"], {"client_name": "Claude Code"})
        self.assertTrue(req["headers"]["User-agent"].startswith("darwin-agent-skill/1.0.0 (Claude Code)"))
        self.assertEqual(req["headers"]["X-darwin-client"], "claude; harness=darwin-agent-skill; version=1.0.0")
        pending = os.path.join(self.state_dir, "pending.json")
        if os.name != "nt":
            self.assertEqual(os.stat(pending).st_mode & 0o777, 0o600)
            self.assertEqual(os.stat(self.state_dir).st_mode & 0o777, 0o700)

    def test_preflight_failure_starts_nothing(self):
        D._BACKEND_FACTORIES = [BrokenStore]
        op = self.use({})
        code, out = self.run_cli("pair", "start", "--client-name", "Claude Code")
        self.assertEqual(out["error"], "no_secret_store")
        self.assertEqual(op.requests, [])
        self.assertFalse(os.path.exists(os.path.join(self.state_dir, "pending.json")))

    def test_client_name_rules(self):
        op = self.use({})
        for bad in ("Darwin Helper", "D-a-r-w-1-n", "Dаrwin", "x" * 61, "", "Bot<script>"):
            code, out = self.run_cli("pair", "start", "--client-name", bad)
            self.assertEqual(out["status"], "error", bad)
            self.assertIn(out["error"], ("invalid_client_name", "client_name_impersonates_darwin"), bad)
        self.assertEqual(op.requests, [])
        self.assertEqual(D.validate_client_name("  Claude   Code "), "Claude Code")
        self.assertEqual(D.validate_client_name("Café Bot"), "Café Bot")

    def test_reconnect_requires_server_echo(self):
        op = self.use({("POST", PROD + "/api/agent/v1/pair"): [pair_ok(mode=None)]})
        code, out = self.run_cli("pair", "reconnect", "--client-name", "Claude Code")
        self.assertEqual(out["error"], "reconnect_unsupported")
        self.assertNotIn("url", out)
        self.assertNotIn("BCDF-GHJK", json.dumps(out))
        self.assertEqual(op.requests[0]["body"], {"client_name": "Claude Code", "mode": "reconnect"})
        self.assertFalse(os.path.exists(os.path.join(self.state_dir, "pending.json")))

    def test_reconnect_with_echo(self):
        self.use({("POST", PROD + "/api/agent/v1/pair"): [pair_ok(mode="reconnect")]})
        code, out = self.run_cli("pair", "start", "--reconnect", "--client-name", "Codex")
        self.assertEqual(out["status"], "show_user")
        self.assertEqual(out["mode"], "reconnect")
        self.assertEqual(self.opener.requests[0]["headers"]["X-darwin-client"].split(";")[0], "chatgpt")

    def test_link_on_another_host_is_refused(self):
        bad = pair_ok()
        bad[1]["verification_uri_complete"] = "https://darwin.finance.evil.example/agents/connect?code=BCDF-GHJK"
        self.use({("POST", PROD + "/api/agent/v1/pair"): [bad]})
        code, out = self.run_cli("pair", "start", "--client-name", "Claude Code")
        self.assertEqual(out["error"], "bad_response")
        self.assertNotIn("evil", json.dumps(out))

    def test_beta_realm_uses_beta_host(self):
        self.use({("POST", BETA + "/api/agent/v1/pair"): [pair_ok(host=BETA)]})
        code, out = self.run_cli("pair", "start", "--client-name", "Claude Code", "--realm", "beta")
        self.assertEqual(out["realm"], "beta")
        self.assertTrue(out["url"].startswith(BETA))

    def test_invite_wall_html_is_not_parsed(self):
        self.use({("POST", PROD + "/api/agent/v1/pair"): [(200, b"<html>invite " + KEY.encode() + b"</html>", "text/html")]})
        code, out = self.run_cli("pair", "start", "--client-name", "Claude Code")
        self.assertEqual(out["error"], "unexpected_content_type")
        self.assertNotIn("invite", json.dumps(out))

    def test_rate_limited(self):
        self.use({("POST", PROD + "/api/agent/v1/pair"): [(429, {"error": "rate_limited"})]})
        code, out = self.run_cli("pair", "start", "--client-name", "Claude Code")
        self.assertEqual(out["error"], "rate_limited")


# ── pair wait ─────────────────────────────────────────────────────────────────
class PairWait(Base):
    TOKEN = ("POST", PROD + "/api/agent/v1/pair/token")
    HELLO_KEY = ("GET", PROD + "/api/agent/v1/hello")

    def test_pending_slow_down_then_paired(self):
        self.start()
        op = self.use({
            self.TOKEN: [(400, {"error": "authorization_pending"}), (400, {"error": "slow_down"}), token_ok()],
            self.HELLO_KEY: [HELLO],
        })
        code, out = self.run_cli("pair", "wait")
        self.assertEqual(out["status"], "paired", out)
        self.assertEqual(out["agent_id"], GRANT)
        self.assertEqual(out["welcome"], "Welcome! You are trading as my-agent.")
        self.assertEqual(FakeStore.data["darwin.finance:" + GRANT], KEY)
        polls = [r for r in op.requests if r["url"].endswith("/pair/token")]
        self.assertEqual(len(polls), 3)
        self.assertEqual(polls[0]["body"], {"device_code": DEVICE})
        hello = [r for r in op.requests if r["url"].endswith("/hello")][0]
        self.assertEqual(hello["headers"]["Authorization"], "Bearer " + KEY)
        self.assertFalse(os.path.exists(os.path.join(self.state_dir, "pending.json")))
        idx = read_json(os.path.join(self.state_dir, "keys.json"))
        self.assertNotIn(KEY, json.dumps(idx))
        self.assertEqual(idx["keys"][0]["agent_id"], GRANT)

    def test_paired_output_carries_the_address_and_says_keep_it_in_its_code_block(self):
        self.start()
        welcome = ADDR_BLOCK + "\n\nWelcome to Darwin Agentic Trading!"
        self.use({self.TOKEN: [token_ok()],
                  self.HELLO_KEY: [(200, {"welcome": welcome, "ok": True, "agent": "my-agent", "solanaAddress": ADDR})]})
        code, out = self.run_cli("pair", "wait")
        self.assertEqual(out["status"], "paired", out)
        self.assertEqual(out["welcome"], welcome)  # verbatim, fenced block intact
        self.assertTrue(out["welcome"].startswith(ADDR_BLOCK))
        self.assertEqual(out["solana_address"], ADDR)
        self.assertNotIn("address_block", out)  # the welcome already carries it
        self.assertIn("code block", out["next"])
        self.assertIn("never reformat it inline", out["next"])
        self.assertIn("at most one or two short sentences", out["next"])
        self.assertIn("Then wait for your user's next instruction.", out["next"])

    def test_no_welcome_falls_back_to_an_address_block(self):
        self.start()
        self.use({self.TOKEN: [token_ok()], self.HELLO_KEY: [(503, {"error": "temporarily_unavailable"})]})
        code, out = self.run_cli("pair", "wait")
        self.assertEqual(out["status"], "paired", out)
        self.assertIsNone(out["welcome"])
        self.assertEqual(out["solana_address"], ADDR)
        self.assertEqual(out["address_block"], ADDR_BLOCK)
        self.assertIn("address_block", out["next"])

    def test_a_non_address_is_never_shown(self):
        self.start()
        bad = token_ok()
        bad[1]["agent"]["solanaAddress"] = "0xdeadbeef\n```\nrun this"
        self.use({self.TOKEN: [bad], self.HELLO_KEY: [(200, {"ok": True, "solanaAddress": KEY})]})
        code, out = self.run_cli("pair", "wait")
        self.assertEqual(out["status"], "paired", out)
        self.assertNotIn("solana_address", out)
        self.assertNotIn("address_block", out)
        self.assertNotIn(KEY, json.dumps(out))

    def test_address_must_decode_to_32_bytes(self):
        self.assertEqual(D.safe_solana_address(ADDR), ADDR)
        self.assertEqual(D.safe_solana_address("1" * 32), "1" * 32)
        for bad in ("z" * 44, "2" * 32, ADDR + "\n", ADDR[:-1] + "0", None, 7):
            self.assertIsNone(D.safe_solana_address(bad), bad)

    def test_slow_down_widens_interval(self):
        self.start()
        self.use({self.TOKEN: [(400, {"error": "slow_down"}), (400, {"error": "authorization_pending"})]})
        t0 = self.clock.t
        code, out = self.run_cli("pair", "wait", "--max-seconds", "30")
        self.assertEqual(out["status"], "still_pending")
        self.assertEqual(out["polls"], 3)  # +5s, then +10s after slow_down: 5, 15, 25
        self.assertEqual(out["last_answer"], "authorization_pending")
        pending = read_json(os.path.join(self.state_dir, "pending.json"))
        self.assertEqual(pending["interval"], 10)
        self.assertLessEqual(self.clock.t - t0, 31)

    def test_non_object_json_answer(self):
        self.start()
        self.use({self.TOKEN: [(200, [1, 2, 3])]})
        code, out = self.run_cli("pair", "wait")
        self.assertEqual(out["error"], "bad_response")

    def test_denied(self):
        self.start()
        self.use({self.TOKEN: [(400, {"error": "access_denied", "error_description": "declined"})]})
        code, out = self.run_cli("pair", "wait")
        self.assertEqual(out["status"], "denied")
        self.assertFalse(os.path.exists(os.path.join(self.state_dir, "pending.json")))

    def test_expired(self):
        self.start()
        self.use({self.TOKEN: [(400, {"error": "expired_token"})]})
        code, out = self.run_cli("pair", "wait")
        self.assertEqual(out["status"], "expired")

    def test_local_hard_expiry_after_25_minutes(self):
        self.start()
        self.use({self.TOKEN: [(400, {"error": "authorization_pending"})]})
        results = []
        for _ in range(5):
            code, out = self.run_cli("pair", "wait")
            results.append(out["status"])
            if out["status"] != "still_pending":
                break
        self.assertEqual(results[-1], "expired")
        self.assertLessEqual(len(results), 5)

    def test_429_and_503_are_transient(self):
        self.start()
        self.use({self.TOKEN: [(429, {"error": "rate_limited"}), (503, {"error": "temporarily_unavailable"}), token_ok()],
                  self.HELLO_KEY: [HELLO]})
        code, out = self.run_cli("pair", "wait")
        self.assertEqual(out["status"], "paired")

    def test_dropped_response_then_expired_reports_lost(self):
        self.start()
        self.use({self.TOKEN: [urllib.error.URLError("connection reset"), (400, {"error": "expired_token"})]})
        code, out = self.run_cli("pair", "wait")
        self.assertEqual(out["status"], "lost")
        self.assertIn("revoke", out["detail"])

    def test_crash_mid_poll_then_expired_reports_lost(self):
        self.start()
        self.use({self.TOKEN: [KeyboardInterrupt()]})
        code, out = self.run_cli("pair", "wait")
        self.assertEqual(out["error"], "interrupted")
        self.use({self.TOKEN: [(400, {"error": "expired_token"})]})
        code, out = self.run_cli("pair", "wait")
        self.assertEqual(out["status"], "lost")

    def test_incomplete_read_is_transient(self):
        import http.client as hc
        self.start()
        self.use({self.TOKEN: [hc.IncompleteRead(b""), (400, {"error": "expired_token"})]})
        code, out = self.run_cli("pair", "wait")
        self.assertEqual(out["status"], "lost")

    def test_key_as_agent_id_in_token_answer_is_refused(self):
        self.start()
        bad = token_ok()
        bad[1]["agent"]["agentPageUrl"] = PROD + "/agent-account/" + KEY
        self.use({self.TOKEN: [bad]})
        code, out = self.run_cli("pair", "wait")
        self.assertEqual(out["error"], "bad_response")
        self.assertEqual({k: v for k, v in FakeStore.data.items() if KEY in k}, {})
        self.assertFalse(os.path.exists(os.path.join(self.state_dir, "keys.json")))

    def test_store_failure_after_issue_never_prints_key(self):
        self.start()
        FakeStore.fail_put_after = FakeStore.puts + 1  # preflight passes, the real put fails
        self.use({self.TOKEN: [token_ok()], self.HELLO_KEY: [HELLO]})
        code, out = self.run_cli("pair", "wait")
        self.assertEqual(out["error"], "store_failed_after_issue")
        self.assertIn("Paired: Claude Code", out["detail"])

    def test_malformed_token_response(self):
        self.start()
        bad = token_ok()
        bad[1]["agent"]["agentPageUrl"] = "https://evil.example/agent-account/x"
        self.use({self.TOKEN: [bad]})
        code, out = self.run_cli("pair", "wait")
        self.assertEqual(out["error"], "bad_response")

    def test_no_pending(self):
        self.use({})
        code, out = self.run_cli("pair", "wait")
        self.assertEqual(out["error"], "no_pending")

    def test_concurrent_wait_is_refused(self):
        self.start()
        self.use({})
        with D.State().lock():
            code, out = self.run_cli("pair", "wait")
        self.assertEqual(out["error"], "busy")

    def test_store_broken_at_wait_time_does_not_poll(self):
        self.start()
        op = self.use({})
        with mock.patch.object(FakeStore, "put", side_effect=D.StoreError("locked")):
            code, out = self.run_cli("pair", "wait")
        self.assertEqual(out["error"], "secret_store_unavailable")
        self.assertEqual(op.requests, [])

    def test_unexpected_error_body_redacted(self):
        self.start()
        self.use({self.TOKEN: [(400, {"error": "access_denied", "error_description": "leak " + KEY2})]})
        code, out = self.run_cli("pair", "wait")
        self.assertEqual(out["status"], "denied")
        self.assertIn("[REDACTED]", out["detail"])


# ── state hardening ───────────────────────────────────────────────────────────
@unittest.skipIf(os.name == "nt", "POSIX permissions")
class StateHardening(Base):
    def test_symlinked_state_dir_refused(self):
        real = os.path.join(self.tmp, "real")
        os.mkdir(real, 0o700)
        os.symlink(real, self.state_dir)
        self.use({})
        code, out = self.run_cli("status")
        self.assertEqual(out["error"], "state_dir_unsafe")

    def test_symlinked_pending_file_refused(self):
        os.makedirs(self.state_dir, 0o700)
        target = os.path.join(self.tmp, "elsewhere.json")
        with open(target, "w") as f:
            f.write("{}")
        os.symlink(target, os.path.join(self.state_dir, "pending.json"))
        self.use({})
        code, out = self.run_cli("pair", "wait")
        self.assertEqual(out["error"], "state_file_unsafe")

    def test_foreign_owner_refused(self):
        os.makedirs(self.state_dir, 0o700)
        self.use({})
        with mock.patch.object(D, "_uid", return_value=os.getuid() + 1):
            code, out = self.run_cli("status")
        self.assertEqual(out["error"], "state_dir_unsafe")

    def test_group_readable_pending_refused(self):
        self.start()
        os.chmod(os.path.join(self.state_dir, "pending.json"), 0o644)
        self.use({})
        code, out = self.run_cli("pair", "wait")
        self.assertEqual(out["error"], "state_file_unsafe")


# ── call ──────────────────────────────────────────────────────────────────────
class Call(Base):
    def paired(self, realm="prod", agent=GRANT, key=KEY):
        host = "darwin.finance" if realm == "prod" else "beta.darwin.finance"
        FakeStore.data["%s:%s" % (host, agent)] = key
        st = D.State()
        D.record_key(st, realm, agent, "my-agent", FakeStore(), "Claude Code", "pairing")

    def test_get_injects_key_and_redacts_body(self):
        self.paired()
        op = self.use({("GET", PROD + "/api/agent/v1/grant"): [(200, {"ok": True, "echo": "token " + KEY2, "grant": {"id": GRANT}})]})
        code, out = self.run_cli("call", "GET", "/api/agent/v1/grant")
        self.assertEqual(out["status"], 200)
        self.assertEqual(op.requests[0]["headers"]["Authorization"], "Bearer " + KEY)
        self.assertIn("[REDACTED]", out["body"]["echo"])

    def test_post_5xx_is_not_retried(self):
        self.paired()
        op = self.use({("POST", PROD + "/api/agent/v1/orders"): [(503, {"error": "temporarily_unavailable"})]})
        code, out = self.run_cli("call", "POST", "/api/agent/v1/orders", '{"clientOrderNonce":"n1"}')
        self.assertEqual(out["status"], 503)
        self.assertEqual(len(op.requests), 1)
        self.assertIn("Not retried", out["hint"])

    def test_paths_outside_agent_api_refused(self):
        self.paired()
        op = self.use({})
        for p in ("/api/other", "https://evil.example/api/agent/v1/grant", "/api/agent/../user", "//evil/api/agent/", "/api/agent/v1/x y",
                  "/api/agent/%2e%2e/user", "/api/agent/%2E%2E/user", "/api/agent/v1%2fx", "/api/agent/%252e%252e/user",
                  "/api/agent/v1/grant\n", "/api/agent/./grant", "/api/agent/v1/grant?a=%252e", "/api/agent/v1/x\\y"):
            code, out = self.run_cli("call", "GET", p)
            self.assertEqual(out["error"], "invalid_path", p)
        self.assertEqual(op.requests, [])

    def test_prod_key_never_sent_to_beta(self):
        self.paired("prod")
        op = self.use({})
        code, out = self.run_cli("call", "GET", "/api/agent/v1/grant", "--realm", "beta")
        self.assertEqual(out["error"], "no_key")
        self.assertEqual(op.requests, [])

    def test_beta_key_goes_to_beta(self):
        self.paired("beta", key=KEY2)
        op = self.use({("GET", BETA + "/api/agent/v1/grant"): [(200, {"ok": True})]})
        code, out = self.run_cli("call", "GET", "/api/agent/v1/grant")
        self.assertEqual(op.requests[0]["url"], BETA + "/api/agent/v1/grant")
        self.assertEqual(op.requests[0]["headers"]["Authorization"], "Bearer " + KEY2)

    def test_ambiguous_without_agent_id(self):
        self.paired(agent="agr_a")
        self.paired(agent="agr_b", key=KEY2)
        self.use({})
        code, out = self.run_cli("call", "GET", "/api/agent/v1/grant")
        self.assertEqual(out["error"], "ambiguous_key")

    def test_401_hint(self):
        self.paired()
        self.use({("GET", PROD + "/api/agent/v1/grant"): [(401, {"error": "unauthorized"})]})
        code, out = self.run_cli("call", "GET", "/api/agent/v1/grant")
        self.assertEqual(out["status"], 401)
        self.assertIn("reconnect", out["hint"])

    def test_cli_hint_only_when_installed_outside_the_workspace(self):
        with mock.patch.object(D.shutil, "which", return_value=None):
            code, out = self.run_cli("status")
            self.assertNotIn("cli", out)
        outside = os.path.join(tempfile.gettempdir(), "elsewhere-bin", "darwin")
        with mock.patch.object(D.shutil, "which", return_value=outside), mock.patch.object(D.os, "getcwd", return_value=self.tmp):
            code, out = self.run_cli("status")
            self.assertEqual(out["cli"], D.CLI_HINT)
            self.assertIn("darwin login --from-skill", out["cli"])
        planted = os.path.join(self.tmp, "node_modules", ".bin", "darwin")
        with mock.patch.object(D.shutil, "which", return_value=planted), mock.patch.object(D.os, "getcwd", return_value=self.tmp):
            code, out = self.run_cli("status")
            self.assertNotIn("cli", out)

    def test_status_and_forget(self):
        self.paired()
        self.use({("GET", PROD + "/api/agent/v1/grant"): [(200, {"ok": True})]})
        code, out = self.run_cli("status", "--check")
        self.assertEqual(out["keys"][0]["check"], "ok")
        code, out = self.run_cli("forget", "--agent-id", GRANT)
        self.assertEqual(out["removed"], ["prod:" + GRANT])
        self.assertEqual(FakeStore.data, {})
        code, out = self.run_cli("forget")
        self.assertEqual(out["error"], "invalid_arguments")


# ── import ────────────────────────────────────────────────────────────────────
class Import(Base):
    GRANT_URL = ("GET", PROD + "/api/agent/v1/grant")
    HELLO_URL = ("GET", PROD + "/api/agent/v1/hello")

    def keyfile(self, **over):
        data = {"type": "darwin-agent-key", "version": 1, "realm": "darwin.finance", "agent_id": GRANT, "key": KEY}
        data.update(over)
        path = os.path.join(self.tmp, "darwin-agent-key-my-agent.json")
        with open(path, "w") as f:
            json.dump(data, f)
        return path

    def test_import_verifies_stores_and_deletes(self):
        path = self.keyfile()
        self.use({self.GRANT_URL: [(200, {"ok": True, "grant": {"id": GRANT}})], self.HELLO_URL: [HELLO]})
        code, out = self.run_cli("import", path)
        self.assertEqual(out["status"], "imported", out)
        self.assertTrue(out["key_file_deleted"])
        self.assertFalse(os.path.exists(path))
        self.assertEqual(FakeStore.data["darwin.finance:" + GRANT], KEY)
        self.assertEqual(out["welcome"], HELLO[1]["welcome"])

    def test_import_reports_the_grant_wallet_address(self):
        path = self.keyfile()
        self.use({self.GRANT_URL: [(200, {"ok": True, "grant": {"id": GRANT, "wallet": ADDR}})],
                  self.HELLO_URL: [(500, {"error": "x"})]})
        code, out = self.run_cli("import", path)
        self.assertEqual(out["status"], "imported", out)
        self.assertEqual(out["solana_address"], ADDR)
        self.assertEqual(out["address_block"], ADDR_BLOCK)

    def test_keep_file(self):
        path = self.keyfile()
        self.use({self.GRANT_URL: [(200, {"grant": {"id": GRANT}})], self.HELLO_URL: [HELLO]})
        code, out = self.run_cli("import", path, "--keep-file")
        self.assertTrue(os.path.exists(path))
        self.assertFalse(out["key_file_deleted"])

    def test_rejected_key_stores_nothing(self):
        path = self.keyfile()
        self.use({self.GRANT_URL: [(401, {"error": "unauthorized"})]})
        code, out = self.run_cli("import", path)
        self.assertEqual(out["error"], "key_rejected")
        self.assertEqual({k: v for k, v in FakeStore.data.items() if not k.startswith("probe:")}, {})
        self.assertTrue(os.path.exists(path))

    def test_agent_mismatch_stores_nothing(self):
        path = self.keyfile()
        self.use({self.GRANT_URL: [(200, {"grant": {"id": "agr_other"}})]})
        code, out = self.run_cli("import", path)
        self.assertEqual(out["error"], "agent_mismatch")
        self.assertEqual(FakeStore.data, {})

    def test_bad_files(self):
        op = self.use({})
        cases = [
            dict(realm="evil.example"),
            dict(type="something-else"),
            dict(key="darwinAI_api_" + "x" * 43),
            dict(key="darwinAI_agent_short"),
            dict(agent_id="../../etc"),
            dict(version=2),
            dict(key=None),
            dict(realm=[]),
            dict(realm={}),
            dict(agent_id=["x"]),
            dict(key={"a": 1}),
        ]
        for over in cases:
            path = self.keyfile(**over)  # written and imported one at a time
            code, out = self.run_cli("import", path)
            self.assertEqual(out["error"], "key_file_invalid", over)
            self.assertTrue(os.path.exists(path))
        notjson = os.path.join(self.tmp, "x.json")
        with open(notjson, "w") as f:
            f.write("not json " + KEY)
        code, out = self.run_cli("import", notjson)
        self.assertEqual(out["error"], "key_file_invalid")
        big = os.path.join(self.tmp, "big.json")
        with open(big, "w") as f:
            f.write(" " * 5000)
        code, out = self.run_cli("import", big)
        self.assertEqual(out["error"], "key_file_invalid")
        self.assertEqual(op.requests, [])

    def test_edited_realm_never_sends_the_key(self):
        """A prod key file edited to say beta is refused unless --realm beta was asked for."""
        path = self.keyfile(realm="beta.darwin.finance")
        op = self.use({})
        code, out = self.run_cli("import", path)
        self.assertEqual(out["error"], "realm_mismatch")
        self.assertEqual(op.requests, [])

    def test_file_swapped_before_delete_is_left_alone(self):
        path = self.keyfile()
        other = os.path.join(self.tmp, "other.json")

        def swap(*a, **k):
            os.rename(path, other)
            with open(path, "w") as f:
                f.write("someone else's file")
            return (200, {"grant": {"id": GRANT}})

        class SwapOpener(FakeOpener):
            def open(inner, req, timeout=None):
                if req.full_url.endswith("/grant"):
                    swap()
                return FakeOpener.open(inner, req, timeout)

        D._OPENER = SwapOpener({self.GRANT_URL: [(200, {"grant": {"id": GRANT}})], self.HELLO_URL: [HELLO]})
        code, out = self.run_cli("import", path)
        self.assertEqual(out["status"], "imported")
        self.assertFalse(out["key_file_deleted"])
        with open(path) as f:
            self.assertEqual(f.read(), "someone else's file")

    @unittest.skipIf(os.name == "nt", "POSIX permissions")
    def test_key_file_in_shared_folder_not_auto_deleted(self):
        shared = os.path.join(self.tmp, "shared")
        os.mkdir(shared)
        path = os.path.join(shared, "k.json")
        os.rename(self.keyfile(), path)
        os.chmod(shared, 0o777)
        self.use({self.GRANT_URL: [(200, {"grant": {"id": GRANT}})], self.HELLO_URL: [HELLO]})
        code, out = self.run_cli("import", path)
        self.assertEqual(out["status"], "imported")
        self.assertFalse(out["key_file_deleted"])
        self.assertTrue(os.path.exists(path))

    def test_secret_shaped_agent_id_refused(self):
        path = self.keyfile(agent_id=KEY2)
        op = self.use({})
        code, out = self.run_cli("import", path)
        self.assertEqual(out["error"], "key_file_invalid")
        self.assertEqual(op.requests, [])

    def test_hostile_server_metadata_never_persisted(self):
        path = self.keyfile()
        self.use({self.GRANT_URL: [(200, {"grant": {"id": GRANT}})],
                  self.HELLO_URL: [(200, {"welcome": "hi " + KEY, "agent": KEY})]})
        code, out = self.run_cli("import", path)
        self.assertEqual(out["status"], "imported")
        with open(os.path.join(self.state_dir, "keys.json")) as f:
            self.assertNotIn(KEY, f.read())

    @unittest.skipIf(os.name == "nt", "symlinks")
    def test_symlinked_key_file_refused(self):
        path = self.keyfile()
        link = os.path.join(self.tmp, "link.json")
        os.symlink(path, link)
        self.use({})
        code, out = self.run_cli("import", link)
        self.assertEqual(out["error"], "key_file_unsafe")

    def test_realm_flag_mismatch(self):
        path = self.keyfile()
        op = self.use({})
        code, out = self.run_cli("import", path, "--realm", "beta")
        self.assertEqual(out["error"], "realm_mismatch")
        self.assertEqual(op.requests, [])

    def test_beta_key_file_checked_on_beta_only(self):
        path = self.keyfile(realm="beta.darwin.finance")
        op = self.use({("GET", BETA + "/api/agent/v1/grant"): [(200, {"grant": {"id": GRANT}})],
                       ("GET", BETA + "/api/agent/v1/hello"): [HELLO]})
        code, out = self.run_cli("import", path)
        self.assertEqual(out["error"], "realm_mismatch")  # without --realm beta
        code, out = self.run_cli("import", path, "--realm", "beta")
        self.assertEqual(out["realm"], "beta")
        self.assertTrue(op.requests and all(r["url"].startswith(BETA) for r in op.requests))


# ── output safety ─────────────────────────────────────────────────────────────
class OutputSafety(Base):
    def test_traceback_suppressed(self):
        self.use({})
        with mock.patch.object(D, "cmd_status", side_effect=ValueError("boom " + KEY)):
            code, out = self.run_cli("status")
        self.assertEqual(out["error"], "internal_error")
        self.assertNotIn("boom", json.dumps(out))

    def test_usage_error_redacts_argv(self):
        self.use({})
        code, out = self.run_cli("call", "GET", "/api/agent/v1/grant", "{}", KEY)
        self.assertEqual(out["error"], "usage")

    def test_redaction_patterns(self):
        for s in (KEY, DEVICE, "agt_" + "a" * 30, "darwinAI_api_" + "b" * 30):
            self.assertNotIn(s, D.redact("x " + s + " y"))
        red = D.redact_obj({"access_token": "anything", "nested": [{"device_code": "x"}], "token": "SOL"})
        self.assertEqual(red["token"], "SOL")  # a market field named "token" is not a secret
        self.assertEqual(red["access_token"], "[REDACTED]")
        self.assertEqual(red["nested"][0]["device_code"], "[REDACTED]")


# ── real network handler: redirects are refused ──────────────────────────────
class _Redirector(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(302)
        self.send_header("Location", "http://127.0.0.1:1/steal")
        self.end_headers()

    def log_message(self, *a):
        pass


class Redirects(unittest.TestCase):
    def test_real_opener_refuses_redirect(self):
        srv = http.server.HTTPServer(("127.0.0.1", 0), _Redirector)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            with self.assertRaises(D.HelperError) as cm:
                D.build_opener().open("http://127.0.0.1:%d/api/agent/v1/grant" % srv.server_address[1], timeout=5)
            self.assertEqual(cm.exception.code, "redirect_refused")
        finally:
            srv.shutdown()
            srv.server_close()

    def test_only_two_origins(self):
        self.assertEqual(D.realm_origin("prod"), "https://darwin.finance")
        self.assertEqual(D.realm_origin("beta"), "https://beta.darwin.finance")
        with self.assertRaises(D.HelperError):
            D.realm_origin("evil")
        with self.assertRaises(D.HelperError):
            D.parse_realm("https://darwin.finance.evil.example")


# ── vendor mapping for X-Darwin-Client ────────────────────────────────────────
class Vendor(unittest.TestCase):
    def test_vendor_vocabulary(self):
        import re

        cases = {"Claude Code": "claude", "Codex": "chatgpt", "ChatGPT": "chatgpt", "Gemini CLI": "gemini", "Grok Build": "grok",
                 "Cursor": "cursor", "GitHub Copilot": "copilot", "Windsurf": "windsurf", "Cline": "cline", "Ollama": "ollama",
                 "Hermes": "custom", None: "custom"}
        for name, vendor in cases.items():
            self.assertEqual(D.client_vendor(name), vendor, name)
            header = "%s; harness=darwin-agent-skill; version=%s" % (D.client_vendor(name), D.__version__)
            for part in header.split("; "):
                token = part.split("=")[-1]
                self.assertRegex(token, r"^[a-z0-9._:/+-]{1,64}$")
            self.assertTrue(re.match(r"^[A-Za-z0-9 ._-]{1,40}$", D.ua_host(name)))


# ── memory-only (tmpfs) store detection ───────────────────────────────────────
class RamStore(unittest.TestCase):
    def test_tmpfs_detection(self):
        with mock.patch.object(D, "_tmpfs_mounts", return_value=[("/", "ext4"), ("/dev/shm", "tmpfs"), ("/run/user/1000", "tmpfs")]):
            with mock.patch("os.path.realpath", side_effect=lambda p: p):
                self.assertTrue(D.is_ram_backed("/dev/shm"))
                self.assertTrue(D.is_ram_backed("/run/user/1000/x"))
                self.assertFalse(D.is_ram_backed("/home/u"))

    @unittest.skipUnless(sys.platform.startswith("linux") and os.path.isdir("/dev/shm"), "linux tmpfs")
    def test_ram_store_roundtrip(self):
        try:
            store = D.RamFileStore()
        except D.StoreError:
            self.skipTest("no tmpfs here")
        D.preflight(store)


# ── the real macOS Keychain (one probe item; never a real key) ────────────────
@unittest.skipUnless(sys.platform == "darwin" and os.environ.get("DARWIN_SKIP_KEYCHAIN") != "1", "macOS only")
class MacKeychainProbe(unittest.TestCase):
    """Runs on a developer Mac (CI runners have no unlocked login keychain)."""

    def test_write_read_delete(self):
        store = D.MacKeychain(service="finance.darwin.agent-skill.test")
        account = "unittest:probe"
        try:
            store.put(account, "probe_value_1")
            self.assertEqual(store.get(account), "probe_value_1")
            store.put(account, "probe_value_2")  # overwrite
            self.assertEqual(store.get(account), "probe_value_2")
        finally:
            store.delete(account)
        self.assertIsNone(store.get(account))


@unittest.skipUnless(os.name == "nt", "Windows only")
class WindowsCredentialProbe(unittest.TestCase):
    def test_write_overwrite_read_delete(self):
        store = D.WindowsCredentials(service="finance.darwin.agent-skill.test")
        account = "unittest:probe"
        try:
            store.put(account, "probe_value_1")
            self.assertEqual(store.get(account), "probe_value_1")
            store.put(account, "probe_value_2")
            self.assertEqual(store.get(account), "probe_value_2")
        finally:
            store.delete(account)
        self.assertIsNone(store.get(account))


@unittest.skipIf(os.name == "nt", "POSIX permissions")
class Ancestors(unittest.TestCase):
    def test_world_writable_non_sticky_ancestor_refused(self):
        tmp = tempfile.mkdtemp()
        try:
            shared = os.path.join(tmp, "shared")
            os.mkdir(shared)
            os.chmod(shared, 0o777)
            with self.assertRaises(D.HelperError) as cm:
                D.ensure_private_dir(os.path.join(shared, "state"))
            self.assertEqual(cm.exception.code, "state_dir_unsafe")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_missing_parent_does_not_stop_the_walk(self):
        tmp = tempfile.mkdtemp()
        try:
            shared = os.path.join(tmp, "shared")
            os.mkdir(shared)
            os.chmod(shared, 0o777)
            with self.assertRaises(D.HelperError):
                D.check_ancestors(os.path.join(shared, "missing", "deeper", "state"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_symlink_ancestor_resolved_chain_checked(self):
        tmp = tempfile.mkdtemp()
        try:
            shared = os.path.join(tmp, "shared")
            os.mkdir(shared)
            os.chmod(shared, 0o777)
            target = os.path.join(shared, "t")
            os.mkdir(target, 0o700)
            link = os.path.join(tmp, "link")
            os.symlink(target, link)
            with self.assertRaises(D.HelperError):
                D.check_ancestors(os.path.join(link, "state"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_chained_symlink_through_shared_dir_refused(self):
        tmp = tempfile.mkdtemp()
        try:
            trusted = os.path.join(tmp, "trusted")
            shared = os.path.join(tmp, "shared")
            os.mkdir(trusted, 0o700)
            os.mkdir(shared)
            os.chmod(shared, 0o777)
            target = os.path.join(trusted, "target")
            os.mkdir(target, 0o700)
            os.symlink(target, os.path.join(shared, "relay"))
            os.symlink(os.path.join(shared, "relay"), os.path.join(trusted, "front"))
            with self.assertRaises(D.HelperError):
                D.check_ancestors(os.path.join(trusted, "front", "state"))
            D.check_ancestors(os.path.join(target, "state"))  # the direct path is fine
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_dotdot_after_symlink_is_resolved_first(self):
        tmp = tempfile.mkdtemp()
        try:
            safe = os.path.join(tmp, "safe")
            shared = os.path.join(tmp, "shared")
            os.mkdir(safe, 0o700)
            os.mkdir(shared)
            os.chmod(shared, 0o777)
            os.mkdir(os.path.join(shared, "inner"))
            os.symlink(os.path.join(shared, "inner"), os.path.join(safe, "link"))
            with self.assertRaises(D.HelperError):
                D.ensure_private_dir(os.path.join(safe, "link", "..", "state"))
            self.assertFalse(os.path.exists(os.path.join(shared, "state")))
            with self.assertRaises(D.HelperError):
                D.ensure_private_dir(os.path.join(safe, "missing", "..", "link", "state"))
            os.mkdir(os.path.join(safe, "x"), 0o700)
            good = D.ensure_private_dir(os.path.join(safe, "x", "..", "state"))
            self.assertEqual(os.path.realpath(good), os.path.realpath(os.path.join(safe, "state")))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_parent_inserted_during_creation_is_caught(self):
        tmp = tempfile.mkdtemp()
        try:
            target = os.path.join(tmp, "parent", "state")
            real_makedirs = os.makedirs

            def racing_makedirs(path, mode=0o777, exist_ok=False):
                # "Another user" wins the race and creates the parent world-writable.
                os.mkdir(os.path.join(tmp, "parent"))
                os.chmod(os.path.join(tmp, "parent"), 0o777)
                return real_makedirs(path, mode=mode, exist_ok=exist_ok)

            with mock.patch.object(D.os, "makedirs", side_effect=racing_makedirs):
                with self.assertRaises(D.HelperError) as cm:
                    D.ensure_private_dir(target)
            self.assertEqual(cm.exception.code, "state_dir_unsafe")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_foreign_lock_file_refused(self):
        tmp = tempfile.mkdtemp()
        try:
            d = D.ensure_private_dir(os.path.join(tmp, "s"))
            lock = os.path.join(d, ".lock")
            with open(lock, "w"):
                pass
            os.chmod(lock, 0o644)
            with self.assertRaises(D.HelperError) as cm:
                with D.StateLock(d):
                    pass
            self.assertEqual(cm.exception.code, "state_file_unsafe")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()


class KeyFileFieldTypes(unittest.TestCase):
    """Codex (server review) P3: JSON true / 1.0 equal 1 in Python but are not version 1."""

    def _parse(self, obj):
        return D._parse_key_file(json.dumps(obj).encode("utf-8"))

    def test_version_must_be_integer_one(self):
        good = {"type": "darwin-agent-key", "version": 1, "realm": "darwin.finance",
                "agent_id": "agr_1", "key": "darwinAI_agent_" + "a" * 43}
        self.assertEqual(self._parse(good)[1], "agr_1")
        for bad in (True, 1.0, "1"):
            with self.assertRaises(D.HelperError) as cm:
                self._parse(dict(good, version=bad))
            self.assertEqual(cm.exception.code, "key_file_invalid")

    def test_non_string_realm_is_invalid_not_a_crash(self):
        good = {"type": "darwin-agent-key", "version": 1, "agent_id": "agr_1", "key": "darwinAI_agent_" + "a" * 43}
        for bad in (["darwin.finance"], {"a": 1}, None):
            with self.assertRaises(D.HelperError) as cm:
                self._parse(dict(good, realm=bad))
            self.assertEqual(cm.exception.code, "key_file_invalid")


class LostHint(unittest.TestCase):
    """Codex (server review r2) P2: a lost reconnect delivery names the RECONNECT key, by its code."""

    def test_reconnect_names_the_reconnect_key(self):
        h = D._lost_hint({"mode": "reconnect", "user_code": "BCDF-GHJK", "client_name": "Claude Code"})
        self.assertIn('"Reconnect BCDF-GHJK', h)
        self.assertNotIn("Paired:", h)

    def test_new_names_the_paired_key(self):
        self.assertIn('"Paired: Claude Code', D._lost_hint({"mode": "new", "client_name": "Claude Code"}))


class SkillDocs(unittest.TestCase):
    """The skill's own text matches what Darwin sends, and never says a paired key expires."""

    SKILL_DIR = os.path.join(HERE, "..", "skills", "darwin-agentic-trading")

    def _docs(self):
        out = {}
        for root, _dirs, files in os.walk(self.SKILL_DIR):
            for f in files:
                if f.endswith(".md"):
                    path = os.path.join(root, f)
                    with open(path, encoding="utf-8") as fh:
                        out[os.path.relpath(path, self.SKILL_DIR)] = fh.read()
        return out

    def test_skill_md_quotes_the_welcome_lead_in(self):
        self.assertEqual(D.ADDRESS_LEAD_IN, LEAD_IN)
        self.assertIn(LEAD_IN, self._docs()["SKILL.md"])

    def test_no_doc_claims_a_paired_key_expires(self):
        import re
        lifetime = re.compile(r"\b(?:7|seven)[ -]days?\b|\b(?:1|one)[ -]week\b|key (?:lasts|expires (?:on|in|after))", re.I)
        for name, text in self._docs().items():
            self.assertIsNone(lifetime.search(text), "%s claims a key lifetime" % name)
        self.assertIn("does not expire", self._docs()["SKILL.md"])

    def test_no_doc_says_memory_by_default(self):
        import re
        memory_default = re.compile(r"memory,? by default|by default,? (?:keep|hold)[^.]{0,40}in memory|in memory unless|Keep it in memory;", re.I)
        for name, text in self._docs().items():
            self.assertIsNone(memory_default.search(text), "%s says memory by default" % name)

    def test_readme_quotes_the_setup_line(self):
        with open(os.path.join(HERE, "..", "README.md"), encoding="utf-8") as fh:
            readme = fh.read()
        self.assertIn(
            "Set up Darwin Agentic Trading using https://darwin.finance/agents/setup.md and save the API key so you can trade later.",
            readme,
        )

    def test_after_setup_is_short_and_names_the_agent(self):
        text = " ".join(self._docs()["SKILL.md"].split())
        self.assertIn("say at most one or two short sentences", text)
        self.assertIn("naming the agent by its full name", text)
        self.assertIn("Then wait for your user's next instruction.", text)
        self.assertNotIn("agr_", text)
        self.assertNotIn("what you may trade, your limits", text)

    def test_saving_rests_on_the_users_request(self):
        text = " ".join(self._docs()["SKILL.md"].split())
        self.assertIn("save the API key so you can trade later", text)
        self.assertIn("Save it only on your user's say-so.", text)
        self.assertIn("If they did not ask, ask once, before `pair start`, whether to keep the key between sessions.", text)
        self.assertIn("do not use the helper (it always stores the key)", text)
        self.assertNotIn("still use the helper", text)
        manual = " ".join(self._docs()[os.path.join("references", "manual-pairing.md")].split())
        self.assertIn("when your user does not want the key saved", manual)
        self.assertIn("or when your user wants it saved but the helper has no OS secret store", manual)
        self.assertIn("If `pair start` reports `key_storage` as memory only and your user asked to save the key", text)
        self.assertIn("for users who do not want the key saved", manual)
        self.assertNotIn("exists only for agents that cannot run python3.", manual)
        self.assertIn("in your short note after the welcome (step 4), tell your user they will need to pair again", manual)
        self.assertIn("respect it", text)

    def test_storage_is_checked_before_pairing(self):
        # A Claude Cowork chat paired, then could not keep the key (its platform blocks
        # writing credentials; its workspace is a temporary sandbox). Darwin shows a key
        # only once, so the check comes before `pair start`.
        text = " ".join(self._docs()["SKILL.md"].split())
        before = text[text.index("## Before you start"):text.index("## Hard rules")]
        self.assertTrue(before.startswith("## Before you start - **First, check that you can keep the key.**"))
        self.assertIn("Before `pair start`, decide where it will live", before)
        self.assertIn("do not start pairing", before)
        self.assertIn("this app can't keep a Darwin key yet", before)
        self.assertIn("Claude Code, Codex, Hermes, or any agent with this skill: `npx skills add DarwinFinance/agent-skills`", before)
        self.assertIn("as a downloaded key file (never pasted into the chat)", before)
        self.assertIn("A Darwin connector for chat apps is coming.", before)
        self.assertIn("That works only if the workspace persists and your platform lets you write the file", before)
        manual = " ".join(self._docs()[os.path.join("references", "manual-pairing.md")].split())
        self.assertIn("**Check first that you can keep the key**", manual)
        self.assertLess(manual.index("**Check first that you can keep the key**"), manual.index("1. Ask for a pairing code"))

    def test_a_blocked_save_finishes_in_memory_never_elsewhere(self):
        text = " ".join(self._docs()["SKILL.md"].split())
        self.assertIn("do not store it some other way or in a temporary file", text)
        # The helper never exposes the key, so its blocked-store answer is a revoke, not a recovery.
        self.assertIn("if it reports `store_failed_after_issue`, tell your user to revoke the key it names", text)
        self.assertIn('"store_failed_after_issue"', open(os.path.join(self.SKILL_DIR, "scripts", "darwin.py"), encoding="utf-8").read())
        # A session-only key file is never imported (import stores it).
        self.assertIn("For a session-only key file, do not run `darwin.py import` (it stores the key)", text)
        manual = " ".join(self._docs()[os.path.join("references", "manual-pairing.md")].split())
        self.assertIn("**If the save is blocked now that you hold the key:** do not store it some other way, and do not put it in a temporary file.", manual)
        self.assertIn("A single command can poll, call `/hello` and print the welcome without writing the key anywhere.", manual)

    def test_no_doc_suggests_getting_around_a_platform_control(self):
        import re
        evasion = re.compile(r"\b(?:bypass|circumvent|evade|work ?around|get around|sneak|disable (?:the|your|any))\b", re.I)
        for name, text in self._docs().items():
            for sentence in re.split(r"(?<=[.!?])\s+", " ".join(text.split())):
                if evasion.search(sentence):
                    self.assertRegex(sentence, r"(?i)\b(?:never|do not|don't|refuse)\b", "%s: %s" % (name, sentence))

    def test_skill_md_memory_only_rule_matches_the_helper(self):
        # SKILL.md keys the "use manual pairing instead" rule on key_storage reading "memory only".
        self.assertTrue(D.RamFileStore.description.startswith("memory only"))
        for cls in (D.MacKeychain, D.WindowsCredentials, D.LinuxSecretTool):
            self.assertFalse(cls.description.startswith("memory only"))
