import json
import os
import shutil
import socket
import stat
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "bin" / "mate-msg"
TOKEN = "s3cret-token-xyz"


class Stub:
    def __init__(self, behavior):
        self.behavior = behavior
        self.requests = []
        self.store = {}
        stub = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("content-length", 0))
                body = json.loads(self.rfile.read(n))
                stub.requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
                stub.behavior(stub, self, body, len(stub.requests))

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def reply(self, h, code, obj):
        data = json.dumps(obj).encode()
        h.send_response(code)
        h.send_header("content-type", "application/json")
        h.send_header("content-length", str(len(data)))
        h.end_headers()
        h.wfile.write(data)

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class MateMsgTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="mate-msg-test-"))
        self.home = self.tmp / "home"
        self.home.mkdir()
        stubs = self.tmp / "stubs"
        stubs.mkdir()
        self.calls = self.tmp / "calls.log"
        self.calls.write_text("")
        for name in ("ssh", "scp", "adw-mux"):
            f = stubs / name
            f.write_text(
                "#!/usr/bin/env bash\n"
                f'echo "{name} $*" >> "{self.calls}"\n'
                'case "$*" in\n'
                '  *"adw-mux list --json"*) echo \'{"agents":[{"name":"first-mate","pane_id":"t12"}]}\' ;;\n'
                "  *mktemp*) echo /tmp/orch-mate-msg.AbC123 ;;\n"
                "esac\n"
            )
            f.chmod(f.stat().st_mode | stat.S_IXUSR)
        self.stubs = stubs
        self.servers = []

    def tearDown(self):
        for s in self.servers:
            s.close()

    def serve(self, behavior):
        s = Stub(behavior)
        self.servers.append(s)
        return s

    def run_script(self, url, *args, **extra):
        env = {
            "PATH": f"{self.stubs}:{os.environ['PATH']}",
            "HOME": str(self.home),
            "AW_URL": url,
            "AW_TOKEN": TOKEN,
            "MATE_MSG_RETRY_MS": "1",
            "MATE_MSG_TIMEOUT": "2",
            "ORCH_HUB": "hub.invalid",
            "TMPDIR": str(self.tmp),
        }
        env.update(extra)
        p = subprocess.run(["bash", str(SCRIPT), *args], env=env, capture_output=True, text=True, timeout=60)
        log = self.home / ".cache" / "mate-msg.log"
        return p, (log.read_text() if log.exists() else "")

    def ssh_calls(self):
        return [l for l in self.calls.read_text().splitlines() if l]

    @staticmethod
    def ok(stub, h, body, n):
        stub.store.setdefault(body["idempotency_key"], {"id": f"m{len(stub.store) + 1}", "body": body})
        stub.reply(h, 200, {"id": stub.store[body["idempotency_key"]]["id"]})

    def test_happy_path(self):
        s = self.serve(self.ok)
        p, log = self.run_script(s.url, "hello mate")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout.strip(), "sent to first-mate")
        self.assertEqual(len(s.requests), 1)
        r = s.requests[0]
        self.assertEqual(r["path"], "/v1/messages/send")
        self.assertEqual(r["headers"]["authorization"], f"Bearer {TOKEN}")
        self.assertEqual(r["headers"]["accept"], "application/json")
        b = r["body"]
        self.assertEqual(
            {k: v for k, v in b.items() if k != "idempotency_key"},
            {"type": "inform", "to_kind": "agent", "to": "first-mate", "text": "hello mate", "refs": []},
        )
        self.assertRegex(b["idempotency_key"], r"^[\x21-\x7e]{1,128}$")
        for blob in (p.stdout, p.stderr, log):
            self.assertNotIn(TOKEN, blob)
        self.assertEqual(self.ssh_calls(), [])

    def test_config_file_and_to(self):
        s = self.serve(self.ok)
        cfg = self.home / ".config" / "aw"
        cfg.mkdir(parents=True)
        (cfg / "config").write_text(f'AW_URL={s.url}\nAW_TOKEN="{TOKEN}"\n')
        env = {"AW_URL": "", "AW_TOKEN": ""}
        p, _ = self.run_script("", "--to", "alice/other", "x", **env)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(s.requests[0]["body"]["to"], "alice/other")
        self.assertEqual(s.requests[0]["headers"]["authorization"], f"Bearer {TOKEN}")

    def test_spaced_config(self):
        s = self.serve(self.ok)
        cfg = self.home / ".config" / "aw"
        cfg.mkdir(parents=True)
        (cfg / "config").write_text(f"# c\n  AW_URL = {s.url} \nAW_TOKEN = {TOKEN}\n")
        p, _ = self.run_script("", "x", AW_URL="", AW_TOKEN="")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(s.requests[0]["headers"]["authorization"], f"Bearer {TOKEN}")

    def test_explicit_to_refused_no_fallback(self):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        p, _ = self.run_script(f"http://127.0.0.1:{port}", "--to", "alice/other", "hi")
        self.assertEqual(p.returncode, 1)
        self.assertIn("unreachable before submission", p.stderr)
        self.assertEqual(self.ssh_calls(), [])

    def test_env_to_refused_no_fallback(self):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        p, _ = self.run_script(f"http://127.0.0.1:{port}", "hi", ORCH_MATE_TO="alice/other")
        self.assertEqual(p.returncode, 1)
        self.assertIn("unreachable before submission", p.stderr)
        self.assertEqual(self.ssh_calls(), [])

    def test_missing_config(self):
        p, _ = self.run_script("", "x", AW_TOKEN="")
        self.assertEqual(p.returncode, 1)
        self.assertIn("AW_URL/AW_TOKEN", p.stderr)

    def test_drop_after_commit_is_exactly_once(self):
        def behavior(stub, h, body, n):
            stub.store.setdefault(body["idempotency_key"], {"id": "orig", "body": body})
            if n == 1:
                h.close_connection = True
                h.connection.shutdown(socket.SHUT_RDWR)
                return
            stub.reply(h, 200, {"id": stub.store[body["idempotency_key"]]["id"]})

        s = self.serve(behavior)
        p, _ = self.run_script(s.url, "once only")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(len(s.store), 1)
        self.assertEqual(len(s.requests), 2)
        self.assertEqual(s.requests[0]["body"]["idempotency_key"], s.requests[1]["body"]["idempotency_key"])
        self.assertEqual(self.ssh_calls(), [])

    def test_refused_dies_never_falls_back(self):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        p, log = self.run_script(f"http://127.0.0.1:{port}", "hi")
        self.assertEqual(p.returncode, 1)
        self.assertIn("unreachable before submission", p.stderr)
        self.assertEqual(self.ssh_calls(), [])
        self.assertNotIn(TOKEN, p.stdout + p.stderr + log)

    def test_refused_retries_same_key_then_dies(self):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        p, _ = self.run_script(f"http://127.0.0.1:{port}", "hi")
        self.assertEqual(p.returncode, 1)
        self.assertIn("server unreachable before submission:", p.stderr)
        self.assertNotIn("unconfirmed", p.stderr)

    def test_first_attempt_curl_7_then_200_sends_once_same_key(self):
        s = self.serve(self.ok)
        real = shutil.which("curl")
        cnt = self.tmp / "curl.count"
        keys = self.tmp / "curl.keys"
        shim = self.stubs / "curl"
        shim.write_text(
            "#!/usr/bin/env bash\n"
            f'n=$(cat "{cnt}" 2>/dev/null || echo 0); echo $((n + 1)) > "{cnt}"\n'
            'for a in "$@"; do case "$a" in @*) jq -r .idempotency_key "${a#@}" >> '
            f'"{keys}" ;; esac; done\n'
            '[[ "$n" != 0 ]] || { echo "refused" >&2; exit 7; }\n'
            f'exec {real} "$@"\n'
        )
        shim.chmod(0o755)
        p, _ = self.run_script(s.url, "hi")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(cnt.read_text().strip(), "2")
        seen = keys.read_text().split()
        self.assertEqual(len(seen), 2)
        self.assertEqual(seen[0], seen[1])
        self.assertEqual(len(s.requests), 1)
        self.assertEqual(s.requests[0]["body"]["idempotency_key"], seen[0])
        self.assertEqual(self.ssh_calls(), [])

    def test_no_adw_mux_in_script(self):
        text = SCRIPT.read_text()
        for needle in ("adw-mux", "--pane", "ORCH_MATE_PANE", "ssh ", "scp "):
            self.assertNotIn(needle, text)

    def test_drop_then_refused_never_falls_back(self):
        def behavior(stub, h, body, n):
            stub.store.setdefault(body["idempotency_key"], {"id": "orig", "body": body})
            h.close_connection = True
            h.connection.shutdown(socket.SHUT_RDWR)
            threading.Thread(target=stub.server.shutdown).start()

        s = self.serve(behavior)
        p, log = self.run_script(s.url, "hi")
        s.server.server_close()
        self.assertEqual(p.returncode, 1)
        self.assertEqual(len(s.store), 1)
        self.assertIn("unconfirmed after", p.stderr)
        self.assertEqual(self.ssh_calls(), [])

    def test_non_json_401(self):
        def behavior(stub, h, body, n):
            data = b"unauthorized plain"
            h.send_response(401)
            h.send_header("content-length", str(len(data)))
            h.end_headers()
            h.wfile.write(data)

        s = self.serve(behavior)
        p, _ = self.run_script(s.url, "hi")
        self.assertEqual(p.returncode, 1)
        self.assertIn("401", p.stderr)
        self.assertIn("unauthorized plain", p.stderr)
        self.assertEqual(self.ssh_calls(), [])

    def test_persistent_5xx_unconfirmed(self):
        s = self.serve(lambda stub, h, body, n: stub.reply(h, 503, {"error": "busy"}))
        p, _ = self.run_script(s.url, "hi")
        self.assertEqual(p.returncode, 1)
        self.assertEqual(len(s.requests), 4)
        self.assertEqual(len({r["body"]["idempotency_key"] for r in s.requests}), 1)
        self.assertIn("unconfirmed after 4 attempts (key ", p.stderr)
        self.assertEqual(self.ssh_calls(), [])

    def test_persistent_timeout_unconfirmed(self):
        import time

        s = self.serve(lambda stub, h, body, n: time.sleep(1.6))
        p, _ = self.run_script(s.url, "hi", MATE_MSG_TIMEOUT="1")
        self.assertEqual(p.returncode, 1)
        self.assertEqual(len(s.requests), 4)
        self.assertEqual(len({r["body"]["idempotency_key"] for r in s.requests}), 1)
        self.assertEqual(self.ssh_calls(), [])

    def test_422_no_retry_no_fallback(self):
        s = self.serve(lambda stub, h, body, n: stub.reply(h, 422, {"error": "bad text"}))
        p, _ = self.run_script(s.url, "hi")
        self.assertEqual(p.returncode, 1)
        self.assertEqual(len(s.requests), 1)
        self.assertIn("422", p.stderr)
        self.assertIn("bad text", p.stderr)
        self.assertEqual(self.ssh_calls(), [])

    def test_text_too_long(self):
        s = self.serve(self.ok)
        p, _ = self.run_script(s.url, "x" * 1025)
        self.assertEqual(p.returncode, 2)
        self.assertIn("text too long (1024 bytes max)", p.stderr)
        self.assertEqual(s.requests, [])
        p, _ = self.run_script(s.url, "x" * 1024)
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_dry_run_sends_nothing(self):
        s = self.serve(self.ok)
        p, _ = self.run_script(s.url, "--dry-run", "héllo")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("first-mate", p.stdout)
        self.assertIn("6 bytes", p.stdout)
        self.assertIn("key ", p.stdout)
        self.assertEqual(s.requests, [])
        self.assertEqual(self.ssh_calls(), [])


if __name__ == "__main__":
    unittest.main()
