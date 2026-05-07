import json
import os
import socketserver
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
WATCH_SCRIPT = ROOT / "scripts" / "watch_tod_console_probe.sh"


class _ConsoleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"tod console ok")

    def log_message(self, format, *args):
        return


class TodConsoleProbeTest(unittest.TestCase):
    def test_watch_tod_console_probe_writes_supplemental_probe_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            shared_dir = root / "shared"
            log_dir = root / "logs"
            shared_dir.mkdir(parents=True, exist_ok=True)
            log_dir.mkdir(parents=True, exist_ok=True)

            class _ThreadedHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
                daemon_threads = True

            server = _ThreadedHTTPServer(("127.0.0.1", 0), _ConsoleHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                console_url = f"http://127.0.0.1:{server.server_port}"
                completed = subprocess.run(
                    ["bash", str(WATCH_SCRIPT)],
                    cwd=ROOT,
                    env={
                        **os.environ,
                        "SHARED_DIR": str(shared_dir),
                        "LOG_DIR": str(log_dir),
                        "TOD_CONSOLE_URL": console_url,
                        "RUN_ONCE": "1",
                    },
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

                probe = json.loads(
                    (shared_dir / "TOD_CONSOLE_PROBE.latest.json").read_text(encoding="utf-8")
                )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

        self.assertEqual(probe["status"], "reachable")
        self.assertEqual(probe["http_status"], 200)
        self.assertFalse(bool(probe["authority"]["authoritative"]))
        self.assertEqual(probe["authority"]["role"], "supplemental_liveness_evidence_only")