import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SDK_ROOT = ROOT / "sdk" / "javascript"
PACKAGE = SDK_ROOT / "package.json"
GUIDE = ROOT / "docs" / "api-sdk.md"
DEPLOY_SMOKE = ROOT / "examples" / "javascript" / "deploy-smoke.mjs"


def test_javascript_sdk_package_is_server_only_dependency_free_and_documented() -> None:
    package = json.loads(PACKAGE.read_text(encoding="utf-8"))
    guide = GUIDE.read_text(encoding="utf-8")

    assert package["name"] == "@zangpu/sdk"
    assert package["type"] == "module"
    assert package["engines"]["node"] == ">=20"
    assert package["browser"] is False
    assert package.get("dependencies", {}) == {}
    assert package["types"] == "./src/index.d.ts"
    assert DEPLOY_SMOKE.is_file()
    assert "JavaScript SDK" in guide
    assert "ZANGPU_API_SECRET" in DEPLOY_SMOKE.read_text(encoding="utf-8")


@pytest.mark.skipif(shutil.which("node") is None, reason="Node 20+ is required for JavaScript SDK contracts")
def test_javascript_sdk_node_contracts() -> None:
    node_path = shutil.which("node")
    assert node_path is not None
    result = subprocess.run(  # noqa: S603 - fixed local executable and repository tests
        [node_path, "--test", str(SDK_ROOT / "test")],
        check=False,
        capture_output=True,
        cwd=ROOT,
        encoding="utf-8",
        errors="replace",
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stdout + result.stderr
