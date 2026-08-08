import json
import os
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from backend.app.security.canonical import body_sha256_hex, create_canonical_request, verify_signature

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "examples" / "curl" / "zangpu-curl.ps1"
GUIDE = ROOT / "docs" / "api-sdk.md"
SIGNING_VALUE = "zps_curl_test_material_0123456789"


def test_sdk_and_curl_delivery_files_are_secret_free_and_documented() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    guide = GUIDE.read_text(encoding="utf-8")

    assert "$env:ZANGPU_API_SECRET" in source
    assert "curl.exe" in source
    assert "--data-binary" in source
    assert "ZANGPU-HMAC-SHA256" in source
    assert "example-secret" not in (source + guide).lower()
    assert "SDK/cURL" in (ROOT / "README.md").read_text(encoding="utf-8")


@pytest.mark.skipif(
    shutil.which("pwsh") is None or shutil.which("curl.exe") is None,
    reason="Windows PowerShell and curl.exe are required for the executable delivery smoke",
)
def test_powershell_curl_example_sends_a_valid_signed_request() -> None:
    failures: list[str] = []
    pwsh_path = shutil.which("pwsh")
    assert pwsh_path is not None

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            try:
                canonical = create_canonical_request(
                    method="GET",
                    raw_path=self.path,
                    raw_query="",
                    body_hash=body_sha256_hex(b""),
                    key_id=self.headers["x-zangpu-key"],
                    timestamp=self.headers["x-zangpu-timestamp"],
                    nonce=self.headers["x-zangpu-nonce"],
                    request_id=self.headers["x-zangpu-request-id"],
                )
                assert verify_signature(SIGNING_VALUE, canonical, self.headers["x-zangpu-signature"])
            except Exception as exc:
                failures.append(type(exc).__name__)
                self.send_response(401)
                self.end_headers()
                return
            payload = json.dumps({"object": "list", "data": []}).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    environment = os.environ.copy()
    environment["ZANGPU_API_SECRET"] = SIGNING_VALUE
    try:
        result = subprocess.run(  # noqa: S603 - fixed local executable and repository script
            [
                pwsh_path,
                "-NoProfile",
                "-File",
                str(SCRIPT),
                "-BaseUrl",
                f"http://127.0.0.1:{server.server_port}",
                "-KeyId",
                "zpk_curl_0123456789",
                "-Path",
                "/api/v1/external/models",
            ],
            check=False,
            capture_output=True,
            env=environment,
            text=True,
            timeout=20,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"object": "list", "data": []}
    assert failures == []
