import shutil
import subprocess
from pathlib import Path

import pytest

from app import config
from app.logs import distill
from app.policy import Policy, load_policy
from app.sandbox import (DockerSandbox, LocalSandbox, SandboxResult, detect_python_version,
                         failure_reproduced)

DEMO = Path(__file__).parent.parent / "demo" / "tinyshop"


@pytest.fixture
def shop(tmp_path: Path) -> Path:
    dest = tmp_path / "repo"
    shutil.copytree(DEMO, dest)
    return dest


def test_detect_python_version(shop, tmp_path):
    assert detect_python_version(shop) == "3.12"
    wf = shop / ".github" / "workflows" / "ci.yml"
    wf.write_text(wf.read_text().replace('"3.12"', "${{ matrix.py }}").replace(
        "    runs-on: ubuntu-latest", "    runs-on: ubuntu-latest\n    strategy:\n      matrix:\n        py: ['3.10', '3.11']"))
    assert detect_python_version(shop) == "3.11"
    assert detect_python_version(tmp_path / "nothing") == "3.12"


def test_local_sandbox_runs_and_reproduces(shop, tmp_path):
    sb = LocalSandbox(install_deps=False)
    policy = load_policy(shop)
    ok = sb.run_tests(shop, tmp_path / "deps", policy, "3.12")
    assert ok.passed, ok.output

    # Break the code the way a developer might: rename a function but miss a caller.
    pricing = shop / "tinyshop" / "pricing.py"
    pricing.write_text(pricing.read_text().replace("def compute_total", "def calc_total"))
    bad = sb.run_tests(shop, tmp_path / "deps", policy, "3.12", ["tests/test_checkout.py::test_checkout_total"])
    assert not bad.passed
    assert "ImportError" in bad.output or "cannot import name" in bad.output


def test_local_sandbox_hides_this_process_secrets(shop, tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "sk-should-never-leak-1234567890")
    (shop / "tests" / "test_env.py").write_text(
        "import os\n\ndef test_no_secret():\n    assert 'LLM_API_KEY' not in os.environ\n")
    res = LocalSandbox(install_deps=False).run_tests(shop, tmp_path / "deps", Policy(), "3.12", ["tests/test_env.py"])
    assert res.passed, res.output


def test_failure_reproduced_matching():
    ci = distill("FAILED tests/test_checkout.py::test_checkout_total - NameError: name 'calc_total' is not defined")
    assert failure_reproduced(SandboxResult(1, "NameError: name 'calc_total'", 10), ci)
    assert not failure_reproduced(SandboxResult(0, "1 passed", 10), ci)
    assert not failure_reproduced(SandboxResult(None, "", 10, timed_out=True), ci)
    assert not failure_reproduced(SandboxResult(1, "ModuleNotFoundError: No module named 'x'", 10), ci)


def test_docker_commands_are_locked_down(shop, tmp_path, monkeypatch):
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "ok", "")

    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(config.settings, "sandbox_image", "auto")
    sb = DockerSandbox()
    sb.install(shop, tmp_path / "deps", load_policy(shop), "3.12")
    sb.run_tests(shop, tmp_path / "deps", load_policy(shop), "3.12", ["tests/test_cart.py"])

    install, test = calls
    assert install[install.index("--network") + 1] == "bridge"       # network only to install
    assert test[test.index("--network") + 1] == "none"               # no network while tests run
    for args in (install, test):
        assert "--rm" in args and "--cap-drop" in args and "--memory" in args and "--pids-limit" in args
        assert "python:3.12-slim" in args
        assert not any("docker.sock" in a for a in args)
        assert not any(a.startswith("LLM_") or a.startswith("GITHUB_") for a in args)
    assert "--read-only" in test
    assert test[-1] == "tests/test_cart.py"
    assert "--target" in install and "/deps" in install
