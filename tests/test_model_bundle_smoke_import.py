from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys


PROJECT_ROOT = Path(__file__).parents[1]


def _write_fake_project(root: Path, identity: str) -> Path:
    backend = root / "app" / "backend"
    backend.mkdir(parents=True)
    (root / "app" / "__init__.py").write_text("", encoding="utf-8")
    (backend / "__init__.py").write_text("", encoding="utf-8")
    (backend / "application.py").write_text(
        "class Application:\n"
        f"    identity = {identity!r}\n",
        encoding="utf-8",
    )
    return root


def _run_isolated(tmp_path: Path, code: str, *arguments: Path) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment.pop("PYTHONHOME", None)
    return subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            "-c",
            code,
            str(PROJECT_ROOT),
            *(str(item) for item in arguments),
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def test_smoke_import_selects_the_exact_requested_project_root(tmp_path: Path) -> None:
    requested = _write_fake_project(tmp_path / "sealed-package", "sealed-package")
    competing = _write_fake_project(tmp_path / "competing-source", "wrong-source")
    result = _run_isolated(
        tmp_path,
        """
import json
import sys
from pathlib import Path

source_root, requested, competing = map(Path, sys.argv[1:4])
sys.path.insert(0, str(source_root))
from tools.smoke_model_bundle_chat import import_application_from_project_root
sys.path.insert(0, str(competing))
application, evidence = import_application_from_project_root(requested)
print(json.dumps({"identity": application.identity, "evidence": evidence}))
""",
        requested,
        competing,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["identity"] == "sealed-package"
    assert payload["evidence"]["isolated_import_verified"] is True
    assert Path(payload["evidence"]["requested_project_root"]) == requested.resolve()
    assert Path(payload["evidence"]["application_module"]) == (
        requested / "app" / "backend" / "application.py"
    ).resolve()
    assert str(competing.resolve()) in payload["evidence"][
        "removed_competing_app_paths"
    ]
    assert str(PROJECT_ROOT.resolve()) in payload["evidence"][
        "removed_competing_app_paths"
    ]


def test_smoke_import_never_writes_bytecode_into_requested_package(
    tmp_path: Path,
) -> None:
    requested = _write_fake_project(tmp_path / "sealed-package", "sealed-package")
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment.pop("PYTHONHOME", None)
    environment.pop("PYTHONDONTWRITEBYTECODE", None)
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            """
import sys
from pathlib import Path

source_root, requested = map(Path, sys.argv[1:3])
sys.path.insert(0, str(source_root))
from tools.smoke_model_bundle_chat import import_application_from_project_root
application, _ = import_application_from_project_root(requested)
assert application.identity == "sealed-package"
""",
            str(PROJECT_ROOT),
            str(requested),
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert not list(requested.rglob("*.pyc"))
    assert not list(requested.rglob("__pycache__"))


def test_smoke_import_rejects_a_wrong_project_root(tmp_path: Path) -> None:
    wrong_root = tmp_path / "not-a-package"
    wrong_root.mkdir()
    workspace = tmp_path / "workspace"
    output = tmp_path / "result.json"
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment.pop("PYTHONHOME", None)
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            str(PROJECT_ROOT / "tools" / "smoke_model_bundle_chat.py"),
            "--project-root",
            str(wrong_root),
            "--workspace",
            str(workspace),
            "--result",
            str(output),
            "--expected-model-id",
            "fixture",
            "--expected-source-sha256",
            "0" * 64,
            "--expected-profile-id",
            "fixture",
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )

    assert result.returncode == 1, result.stderr
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["success"] is False
    assert payload["application_import"]["isolated_import_verified"] is False
    assert payload["project_root"] == str(wrong_root.resolve())
    assert "does not contain app/backend/application.py" in payload["error"]


def test_smoke_import_rejects_preloaded_backend_leakage(tmp_path: Path) -> None:
    requested = _write_fake_project(tmp_path / "sealed-package", "sealed-package")
    leaking = _write_fake_project(tmp_path / "source-checkout", "source-checkout")
    result = _run_isolated(
        tmp_path,
        """
import sys
from pathlib import Path

source_root, requested, leaking = map(Path, sys.argv[1:4])
sys.path.insert(0, str(leaking))
from app.backend.application import Application as LeakedApplication
assert LeakedApplication.identity == "source-checkout"
sys.path.insert(0, str(source_root))
from tools.smoke_model_bundle_chat import import_application_from_project_root
try:
    import_application_from_project_root(requested)
except RuntimeError as error:
    print(str(error))
    raise SystemExit(0)
raise SystemExit(9)
""",
        requested,
        leaking,
    )

    assert result.returncode == 0, result.stderr
    assert "already-imported app module leakage" in result.stdout
    assert "source-checkout" in result.stdout
