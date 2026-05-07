from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

REPORT_PATH = Path("runtime/reports/mim_mobile_web_runtime_probe.json")


def _command_output(argv: list[str]) -> dict[str, object]:
    completed = subprocess.run(argv, capture_output=True, text=True)
    return {
        "argv": argv,
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


def _http_probe(url: str) -> dict[str, object]:
    try:
        with urllib.request.urlopen(url, timeout=20) as response:
            body = response.read().decode("utf-8", "replace")
            return {
                "url": url,
                "ok": True,
                "status": getattr(response, "status", None),
                "headers": dict(response.headers.items()),
                "body": body,
            }
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        return {
            "url": url,
            "ok": False,
            "status": exc.code,
            "headers": dict(exc.headers.items()),
            "body": body,
            "error": str(exc),
        }
    except Exception as exc:
        return {
            "url": url,
            "ok": False,
            "error": repr(exc),
        }


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _http_probe_no_redirect(url: str) -> dict[str, object]:
    opener = urllib.request.build_opener(_NoRedirectHandler)
    request = urllib.request.Request(url, headers={"Cache-Control": "no-cache"})
    try:
        with opener.open(request, timeout=20) as response:
            body = response.read().decode("utf-8", "replace")
            return {
                "url": url,
                "ok": True,
                "status": getattr(response, "status", None),
                "headers": dict(response.headers.items()),
                "body": body,
            }
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        return {
            "url": url,
            "ok": False,
            "status": exc.code,
            "headers": dict(exc.headers.items()),
            "body": body,
            "error": str(exc),
        }
    except Exception as exc:
        return {
            "url": url,
            "ok": False,
            "error": repr(exc),
        }


def main() -> None:
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "service_status": _command_output(
            ["systemctl", "--user", "--no-pager", "--full", "status", "mim-mobile-web.service"]
        ),
        "service_journal": _command_output(
            [
                "journalctl",
                "--user",
                "-u",
                "mim-mobile-web.service",
                "-n",
                "120",
                "--no-pager",
            ]
        ),
        "root": _http_probe("http://127.0.0.1:18001/"),
        "favicon": _http_probe("http://127.0.0.1:18001/favicon.ico"),
        "mim": _http_probe("http://127.0.0.1:18001/mim"),
        "openapi": _http_probe("http://127.0.0.1:18001/openapi.json"),
        "remote_apex": _http_probe_no_redirect("https://mimtod.com/?probe=20260420c"),
        "remote_www": _http_probe_no_redirect("https://www.mimtod.com/?probe=20260420c"),
        "remote_mim": _http_probe_no_redirect("https://mim.mimtod.com/?probe=20260420c"),
    }
    REPORT_PATH.write_text(json.dumps(report, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
