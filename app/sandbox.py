"""The sandbox: where the project's (untrusted) code actually runs.

Two backends:
- DockerSandbox: the real one. A fresh container per run, no secrets, resource
  limits, network ON only while installing dependencies and OFF while tests run.
- LocalSandbox: runs on this machine with no isolation. ONLY for development on your
  own demo repositories, never for code you do not trust.

Every command is a fixed list of arguments; nothing goes through a shell.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

from app import config
from app.logs import DistilledLog
from app.policy import Policy
from app.scrub import scrub_text

LABEL = "greenlight=1"
MAX_OUTPUT_CHARS = 12000


@dataclass
class SandboxResult:
    exit_code: Optional[int]
    output: str
    duration_ms: int
    timed_out: bool = False
    command: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


def _trim_output(text: str) -> str:
    text = scrub_text(text or "")
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    head, tail = text[:2000], text[-(MAX_OUTPUT_CHARS - 2100):]
    return f"{head}\n[... {len(text) - len(head) - len(tail)} characters skipped ...]\n{tail}"


def detect_python_version(repo_dir: str | Path, default: str = "3.12") -> str:
    """Read the Python version the CI workflow uses (actions/setup-python)."""
    wf_dir = Path(repo_dir) / ".github" / "workflows"
    if wf_dir.is_dir():
        for wf in sorted(wf_dir.glob("*.y*ml")):
            try:
                text = wf.read_text()
                data = yaml.safe_load(text) or {}
            except (yaml.YAMLError, OSError):
                continue
            for job in (data.get("jobs") or {}).values():
                if not isinstance(job, dict):
                    continue
                matrix = ((job.get("strategy") or {}).get("matrix") or {})
                for step in job.get("steps") or []:
                    if isinstance(step, dict) and "setup-python" in str(step.get("uses", "")):
                        ver = str((step.get("with") or {}).get("python-version", ""))
                        m = re.search(r"matrix\.([\w-]+)", ver)
                        if m and isinstance(matrix.get(m.group(1)), list) and matrix[m.group(1)]:
                            ver = str(matrix[m.group(1)][-1])   # newest version in the matrix
                        if re.fullmatch(r"3\.\d+(\.\d+)?", ver):
                            return ".".join(ver.split(".")[:2])
    return default


def python_path_entries(repo_root_in_box: str, repo_dir: Path) -> list[str]:
    entries = [repo_root_in_box]
    if (repo_dir / "src").is_dir():
        entries.append(f"{repo_root_in_box}/src")
    return entries


def failure_reproduced(result: SandboxResult, ci_log: DistilledLog) -> bool:
    """Did the sandbox fail the same way CI did?"""
    if result.timed_out or result.exit_code in (0, None, 5):   # 5 = pytest collected no tests
        return False
    out = result.output
    if any(t.split("::")[-1] in out for t in ci_log.failing_tests):
        return True
    if any(err in out for err in ci_log.error_types if err not in ("AssertionError",)):
        return True
    return "AssertionError" in ci_log.error_types and ("assert" in out or "AssertionError" in out)


class Sandbox:
    """Common interface for both backends."""

    def install(self, repo_dir: Path, deps_dir: Path, policy: Policy, py_version: str) -> SandboxResult:
        raise NotImplementedError

    def run_tests(self, repo_dir: Path, deps_dir: Path, policy: Policy, py_version: str,
                  targets: Optional[list[str]] = None) -> SandboxResult:
        raise NotImplementedError

    def cleanup_leftovers(self) -> None:
        pass


def _test_args(policy: Policy, targets: Optional[list[str]]) -> list[str]:
    args = list(policy.test_command)
    if "pytest" in args:
        args += ["-p", "no:cacheprovider"]
    return args + list(targets or [])


def _install_args(policy: Policy, target_dir: str) -> list[str]:
    cmd = list(policy.install_command)
    # Turn "pip install ..." into "python -m pip install --target <dir> ...".
    if cmd[0] in ("pip", "pip3"):
        cmd = ["python", "-m", "pip"] + cmd[1:]
    if "install" in cmd and "--target" not in cmd:
        i = cmd.index("install") + 1
        cmd = cmd[:i] + ["--no-cache-dir", "--disable-pip-version-check", "--target", target_dir] + cmd[i:]
    return cmd


class DockerSandbox(Sandbox):
    def __init__(self) -> None:
        if shutil.which("docker") is None:
            raise RuntimeError("docker is not installed")

    def _image(self, py_version: str) -> str:
        image = config.settings.sandbox_image
        if image in ("", "auto", "greenlight-sandbox:latest"):
            return f"python:{py_version}-slim"
        return image

    def _base(self, name: str, network: str) -> list[str]:
        s = config.settings
        uid = os.getuid() if hasattr(os, "getuid") else 1000
        gid = os.getgid() if hasattr(os, "getgid") else 1000
        return [
            "docker", "run", "--rm", "--name", name, "--label", LABEL,
            "--network", network,
            "--cpus", s.sandbox_cpus, "--memory", s.sandbox_memory, "--pids-limit", "256",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--user", f"{uid}:{gid}", "-e", "HOME=/tmp", "--tmpfs", "/tmp:rw,size=256m",
        ]

    def _run(self, args: list[str], name: str) -> SandboxResult:
        start = time.monotonic()
        try:
            proc = subprocess.run(args, capture_output=True, text=True, timeout=config.settings.sandbox_timeout_seconds)
            out = (proc.stdout or "") + (proc.stderr or "")
            return SandboxResult(proc.returncode, _trim_output(out), int((time.monotonic() - start) * 1000), False, args)
        except subprocess.TimeoutExpired as exc:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True)
            out = (exc.stdout or b"").decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            return SandboxResult(None, _trim_output(out), int((time.monotonic() - start) * 1000), True, args)

    def install(self, repo_dir, deps_dir, policy, py_version):
        deps_dir.mkdir(parents=True, exist_ok=True)
        name = f"greenlight-install-{uuid.uuid4().hex[:10]}"
        args = self._base(name, "bridge") + [
            "-v", f"{repo_dir.resolve()}:/workspace/repo:ro",
            "-v", f"{deps_dir.resolve()}:/deps:rw",
            "-w", "/workspace/repo", self._image(py_version),
        ] + _install_args(policy, "/deps")
        return self._run(args, name)

    def run_tests(self, repo_dir, deps_dir, policy, py_version, targets=None):
        name = f"greenlight-test-{uuid.uuid4().hex[:10]}"
        pypath = ":".join(["/deps"] + python_path_entries("/workspace/repo", repo_dir))
        args = self._base(name, "none") + [
            "--read-only",
            "-v", f"{repo_dir.resolve()}:/workspace/repo:ro",
            "-v", f"{deps_dir.resolve()}:/deps:ro",
            "-e", f"PYTHONPATH={pypath}", "-e", "PYTHONDONTWRITEBYTECODE=1",
            "-w", "/workspace/repo", self._image(py_version),
        ] + _test_args(policy, targets)
        return self._run(args, name)

    def cleanup_leftovers(self) -> None:
        """Remove containers left behind if GreenLight crashed mid-run."""
        proc = subprocess.run(["docker", "ps", "-aq", "--filter", f"label={LABEL}"], capture_output=True, text=True)
        ids = proc.stdout.split()
        if ids:
            subprocess.run(["docker", "rm", "-f", *ids], capture_output=True)


class LocalSandbox(Sandbox):
    """NO ISOLATION. Development only, on repositories you wrote yourself.

    install_deps=False skips installing and uses the current Python (fast tests).
    """

    def __init__(self, install_deps: bool = True) -> None:
        self.install_deps = install_deps

    def _python(self, deps_dir: Path) -> str:
        venv_py = deps_dir / "venv" / ("Scripts" if os.name == "nt" else "bin") / "python"
        return str(venv_py) if venv_py.exists() else sys.executable

    def _run(self, args: list[str], cwd: Path, env: dict) -> SandboxResult:
        start = time.monotonic()
        try:
            proc = subprocess.run(args, cwd=str(cwd), capture_output=True, text=True, env=env,
                                  timeout=config.settings.sandbox_timeout_seconds)
            return SandboxResult(proc.returncode, _trim_output(proc.stdout + proc.stderr),
                                 int((time.monotonic() - start) * 1000), False, args)
        except subprocess.TimeoutExpired as exc:
            out = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            return SandboxResult(None, _trim_output(out), int((time.monotonic() - start) * 1000), True, args)

    def _env(self, repo_dir: Path) -> dict:
        # Start from a minimal environment so no secret from this process leaks in.
        env = {k: v for k, v in os.environ.items() if k in ("PATH", "SYSTEMROOT", "LANG", "LC_ALL", "TMPDIR")}
        env["PYTHONPATH"] = os.pathsep.join(python_path_entries(str(repo_dir.resolve()), repo_dir))
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["HOME"] = str(repo_dir.parent)
        return env

    def install(self, repo_dir, deps_dir, policy, py_version):
        deps_dir.mkdir(parents=True, exist_ok=True)
        if not self.install_deps:
            return SandboxResult(0, "dependency install skipped (local dev mode)", 0)
        env = self._env(repo_dir)
        venv = deps_dir / "venv"
        res = self._run([sys.executable, "-m", "venv", str(venv)], repo_dir, env)
        if not res.passed:
            return res
        cmd = list(policy.install_command)
        if cmd[0] in ("pip", "pip3", "python", "python3"):
            cmd = [self._python(deps_dir), "-m", "pip"] + (cmd[1:] if cmd[0].startswith("pip") else cmd[3:])
        return self._run(cmd, repo_dir, env)

    def run_tests(self, repo_dir, deps_dir, policy, py_version, targets=None):
        args = _test_args(policy, targets)
        if args[0] in ("python", "python3"):
            args = [self._python(deps_dir)] + args[1:]
        return self._run(args, repo_dir, self._env(repo_dir))


def get_sandbox() -> Sandbox:
    backend = config.settings.sandbox_backend
    if backend == "docker":
        return DockerSandbox()
    if backend == "local":
        return LocalSandbox(install_deps=True)
    if backend == "local-noinstall":
        return LocalSandbox(install_deps=False)
    raise ValueError(f"unknown SANDBOX_BACKEND {backend!r}")
