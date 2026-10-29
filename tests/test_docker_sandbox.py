"""Real Docker tests for the sandbox. Skipped when no Docker daemon is running.

Run locally with Docker running:      python -m pytest -q tests/test_docker_sandbox.py
Use a specific image:                 SANDBOX_IMAGE=my-python-image python -m pytest ...
Skip the install test (no internet):  GREENLIGHT_DOCKER_OFFLINE=1 python -m pytest ...
"""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from app import config
from app.policy import Policy, load_policy
from app.sandbox import LABEL, DockerSandbox

DEMO = Path(__file__).parent.parent / "demo" / "tinyshop"


def docker_running() -> bool:
    if shutil.which("docker") is None:
        return False
    return subprocess.run(["docker", "info"], capture_output=True).returncode == 0


pytestmark = pytest.mark.skipif(not docker_running(), reason="Docker daemon not available")


_image_ok: dict = {}


def image_available(image: str) -> bool:
    """True if the image is present or can be pulled (cached per test run)."""
    if image not in _image_ok:
        have = subprocess.run(["docker", "image", "inspect", image], capture_output=True).returncode == 0
        if not have:
            have = subprocess.run(["docker", "pull", "-q", image], capture_output=True, timeout=300).returncode == 0
        _image_ok[image] = have
    return _image_ok[image]


@pytest.fixture
def sandbox(monkeypatch):
    monkeypatch.setattr(config.settings, "sandbox_image", os.environ.get("SANDBOX_IMAGE", "auto"))
    monkeypatch.setattr(config.settings, "sandbox_timeout_seconds", 120)
    sb = DockerSandbox()
    image = sb._image("3.12")
    if not image_available(image):
        pytest.skip(f"Docker image {image} is not available and cannot be pulled")
    return sb


@pytest.fixture
def shop(tmp_path: Path) -> Path:
    dest = tmp_path / "repo"
    shutil.copytree(DEMO, dest)
    return dest


def deps(tmp_path: Path) -> Path:
    d = tmp_path / "deps"
    d.mkdir(exist_ok=True)
    return d


def ready_deps(sandbox, repo: Path, tmp_path: Path) -> Path:
    """Install the project's dependencies (pytest) into the sandbox's deps folder, unless offline."""
    d = deps(tmp_path)
    if not os.environ.get("GREENLIGHT_DOCKER_OFFLINE"):
        res = sandbox.install(repo, d, load_policy(DEMO), "3.12")
        assert res.passed, res.output
    return d


def test_runs_tests_and_sees_a_real_failure(sandbox, shop, tmp_path):
    policy = load_policy(shop)
    if not os.environ.get("GREENLIGHT_DOCKER_OFFLINE"):
        installed = sandbox.install(shop, deps(tmp_path), policy, "3.12")
        assert installed.passed, installed.output
    ok = sandbox.run_tests(shop, deps(tmp_path), policy, "3.12")
    assert ok.passed, ok.output
    assert "10 passed" in ok.output

    pricing = shop / "tinyshop" / "pricing.py"
    pricing.write_text(pricing.read_text().replace("def compute_total", "def calc_total"))
    bad = sandbox.run_tests(shop, deps(tmp_path), policy, "3.12", ["tests/test_checkout.py"])
    assert not bad.passed and "compute_total" in bad.output


def test_untrusted_code_is_locked_in(sandbox, shop, tmp_path, monkeypatch):
    """The project's tests try to misbehave; every attempt must fail."""
    monkeypatch.setenv("LLM_API_KEY", "sk-must-not-leak-0123456789abcdef")
    (shop / "tests" / "test_escape.py").write_text('''
import os, socket

def test_no_secrets_in_environment():
    assert "LLM_API_KEY" not in os.environ
    assert not any("TOKEN" in k or "SECRET" in k for k in os.environ)

def test_no_network():
    try:
        socket.create_connection(("1.1.1.1", 80), timeout=3)
        raise AssertionError("network should be off")
    except OSError:
        pass

def test_cannot_change_the_repository():
    try:
        open("tinyshop/pricing.py", "a").write("# hacked")
        raise AssertionError("repository should be read-only")
    except OSError:
        pass

def test_cannot_write_outside_tmp():
    try:
        open("/evil.txt", "w").write("x")
        raise AssertionError("root filesystem should be read-only")
    except OSError:
        pass

def test_not_root():
    assert os.getuid() != 0

def test_no_docker_socket():
    assert not os.path.exists("/var/run/docker.sock")
''')
    res = sandbox.run_tests(shop, ready_deps(sandbox, shop, tmp_path), Policy(), "3.12", ["tests/test_escape.py"])
    assert res.passed, res.output
    assert "6 passed" in res.output
    assert "hacked" not in (shop / "tinyshop" / "pricing.py").read_text()


def test_runaway_tests_are_stopped(sandbox, shop, tmp_path, monkeypatch):
    (shop / "tests" / "test_hang.py").write_text("import time\n\ndef test_forever():\n    time.sleep(600)\n")
    d = ready_deps(sandbox, shop, tmp_path)
    monkeypatch.setattr(config.settings, "sandbox_timeout_seconds", 8)
    res = sandbox.run_tests(shop, d, Policy(), "3.12", ["tests/test_hang.py"])
    assert res.timed_out and not res.passed
    leftover = subprocess.run(["docker", "ps", "-q", "--filter", f"label={LABEL}"], capture_output=True, text=True)
    assert leftover.stdout.strip() == ""            # the container was removed


def test_memory_limit_kills_greedy_tests(sandbox, shop, tmp_path, monkeypatch):
    monkeypatch.setattr(config.settings, "sandbox_memory", "128m")
    (shop / "tests" / "test_greedy.py").write_text("def test_eat_memory():\n    x = bytearray(600 * 1024 * 1024)\n    assert x\n")
    res = sandbox.run_tests(shop, deps(tmp_path), Policy(), "3.12", ["tests/test_greedy.py"])
    assert not res.passed
