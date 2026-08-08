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
SCRIPT = ROOT / "load" / "k6" / "signed-api.js"
SIGNING_MODULE = ROOT / "load" / "k6" / "signing.js"
RUNNER = ROOT / "load" / "k6" / "run.ps1"
GUIDE = ROOT / "docs" / "load-testing.md"
REPORT = ROOT / "load" / "k6" / "result-template.md"
SIGNING_VALUE = "zps_k6_test_material_0123456789"


def _k6_executable() -> str | None:
    configured = os.environ.get("K6_EXE")
    if configured and Path(configured).is_file():
        return str(Path(configured).resolve())
    return shutil.which("k6")


def _node_executable() -> str | None:
    configured = os.environ.get("NODE_EXE")
    if configured and Path(configured).is_file():
        return str(Path(configured).resolve())
    return shutil.which("node")


def test_k6_delivery_freezes_signing_safety_profiles_and_results() -> None:
    script = SCRIPT.read_text(encoding="utf-8")
    signing = SIGNING_MODULE.read_text(encoding="utf-8")
    runner = RUNNER.read_text(encoding="utf-8")
    guide = GUIDE.read_text(encoding="utf-8")
    report = REPORT.read_text(encoding="utf-8")
    combined = script + signing + runner + guide + report

    for value in (
        "ZANGPU-HMAC-SHA256",
        "X-Zangpu-Key",
        "X-Zangpu-Timestamp",
        "X-Zangpu-Nonce",
        "X-Zangpu-Request-Id",
        "X-Zangpu-Signature-Version",
        "X-Zangpu-Signature",
        "http_req_duration",
        "http_req_failed",
        "handleSummary",
        "smoke",
        "steady",
        "burst",
    ):
        assert value in combined

    assert "ZANGPU_API_SECRET" in script
    assert 'import { createSignedHeaders } from "./signing.js"' in script
    assert "ZANGPU_LOAD_CONFIRM_CHAT" in script
    assert "ConfirmChatSpend" in runner
    assert "responseType: \"none\"" in script
    assert "http-debug" not in combined.lower()
    assert "retry" not in script.lower()
    assert "真实" in guide
    assert "待执行" in report
    assert "example-secret" not in combined.lower()


@pytest.mark.skipif(_node_executable() is None, reason="Node.js is required for the executable signer vector")
def test_k6_signer_module_matches_frozen_hmac_vector(tmp_path: Path) -> None:
    node_executable = _node_executable()
    assert node_executable is not None
    module_copy = tmp_path / "signing.mjs"
    shutil.copyfile(SIGNING_MODULE, module_copy)
    harness = tmp_path / "vector.mjs"
    harness.write_text(
        """
import { createHash, createHmac } from "node:crypto";
import { createSignedHeaders } from "./signing.mjs";
const body = '{"model":"zangpu-test","messages":[{"role":"user","content":"hello"}],"stream":false,"max_tokens":64}';
const headers = createSignedHeaders({
  method: "post",
  path: "/api/v1/external/chat/completions",
  body,
  keyId: "zpk_test_0123456789",
  secret: "zps_test_secret_0123456789",
  timestamp: "1785420000",
  nonce: "nonce_0123456789abcdef",
  requestId: "req_0123456789abcdef",
  sha256Hex: (value) => createHash("sha256").update(value, "utf8").digest("hex"),
  hmacSha256Hex: (key, value) => createHmac("sha256", key).update(value, "utf8").digest("hex"),
});
process.stdout.write(headers["X-Zangpu-Signature"]);
""".strip(),
        encoding="utf-8",
    )
    syntax = subprocess.run(  # noqa: S603 - explicit local Node executable parses repository source
        [node_executable, "--input-type=module", "--check"],
        check=False,
        capture_output=True,
        input=SCRIPT.read_text(encoding="utf-8"),
        text=True,
        timeout=10,
    )
    vector = subprocess.run(  # noqa: S603 - explicit local Node executable runs a fixed local vector
        [node_executable, str(harness)],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert syntax.returncode == 0, syntax.stderr
    assert vector.returncode == 0, vector.stderr
    assert vector.stdout == "121118be99e3276c168066f7a10b12cd4d395a13ecbb843dbb545363595decfe"


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="PowerShell 7 is required for the runner safety smoke")
def test_k6_runner_rejects_unconfirmed_chat_without_exposing_secret(tmp_path: Path) -> None:
    pwsh_executable = shutil.which("pwsh")
    assert pwsh_executable is not None
    environment = os.environ.copy()
    environment.update(
        {
            "ZANGPU_API_BASE_URL": "http://127.0.0.1:8080",
            "ZANGPU_API_KEY_ID": "zpk_k6_0123456789",
            "ZANGPU_API_SECRET": SIGNING_VALUE,
            "ZANGPU_LOAD_MODEL": "zangpu-test",
        }
    )
    result = subprocess.run(  # noqa: S603 - explicit PowerShell executable and repository script
        [
            pwsh_executable,
            "-NoProfile",
            "-File",
            str(RUNNER),
            "-Target",
            "chat",
            "-K6Exe",
            str(tmp_path / "missing-k6.exe"),
        ],
        check=False,
        capture_output=True,
        encoding="utf-8",
        env=environment,
        timeout=10,
    )

    rendered = result.stdout + result.stderr
    assert result.returncode != 0
    assert "ConfirmChatSpend" in rendered
    assert "k6 was not found" not in rendered
    assert SIGNING_VALUE not in rendered


@pytest.mark.skipif(_k6_executable() is None, reason="k6 executable is required for the live delivery smoke")
def test_k6_script_sends_unique_valid_signatures_and_sanitized_summary(tmp_path: Path) -> None:
    k6_executable = _k6_executable()
    assert k6_executable is not None
    failures: list[str] = []
    nonces: set[str] = set()
    request_ids: set[str] = set()
    lock = threading.Lock()

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
                with lock:
                    assert self.headers["x-zangpu-nonce"] not in nonces
                    assert self.headers["x-zangpu-request-id"] not in request_ids
                    nonces.add(self.headers["x-zangpu-nonce"])
                    request_ids.add(self.headers["x-zangpu-request-id"])
            except Exception as exc:
                failures.append(type(exc).__name__)
                self.send_response(401)
                self.end_headers()
                return

            payload = json.dumps({"object": "list", "data": []}).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(payload)))
            self.send_header("x-zangpu-request-id", f"srv_{len(request_ids):032d}")
            self.send_header("x-ratelimit-limit", "100")
            self.send_header("x-ratelimit-remaining", "99")
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    summary_json = tmp_path / "summary.json"
    summary_text = tmp_path / "summary.txt"
    environment = os.environ.copy()
    environment.update(
        {
            "ZANGPU_API_BASE_URL": f"http://127.0.0.1:{server.server_port}",
            "ZANGPU_API_KEY_ID": "zpk_k6_0123456789",
            "ZANGPU_API_SECRET": SIGNING_VALUE,
            "ZANGPU_LOAD_TARGET": "models",
            "ZANGPU_LOAD_PROFILE": "smoke",
            "ZANGPU_LOAD_SMOKE_ITERATIONS": "3",
            "ZANGPU_LOAD_P95_MS": "1500",
            "ZANGPU_LOAD_SUMMARY_JSON": str(summary_json),
            "ZANGPU_LOAD_SUMMARY_TEXT": str(summary_text),
        }
    )
    try:
        result = subprocess.run(  # noqa: S603 - explicit local k6 executable and repository script
            [k6_executable, "run", "--quiet", str(SCRIPT)],
            check=False,
            capture_output=True,
            env=environment,
            text=True,
            timeout=30,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert result.returncode == 0, result.stderr
    assert failures == []
    assert len(nonces) == 3
    assert len(request_ids) == 3
    summary = json.loads(summary_json.read_text(encoding="utf-8"))
    assert summary["schema_version"] == 1
    assert summary["target"] == "models"
    assert summary["profile"] == "smoke"
    assert summary["metrics"]["iterations"]["count"] == 3
    assert summary["metrics"]["api_success"]["rate"] == 1
    assert summary["thresholds_passed"] is True
    rendered = result.stdout + result.stderr + summary_json.read_text(encoding="utf-8") + summary_text.read_text(
        encoding="utf-8"
    )
    assert SIGNING_VALUE not in rendered
    assert "x-zangpu-signature" not in rendered.lower()
