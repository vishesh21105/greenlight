"""Build and validate the benchmark cases.

For every case this creates a small git repository with two commits:
  green  = the project as it was (tests pass), plus a .greenlight.yml policy
  broken = the planted mistake (CI fails)
and checks, inside the Docker sandbox, that:
  - the visible tests pass on green and fail on broken (except infra cases, whose failure is the runner's)
  - the hidden tests pass on green (so they describe correct behaviour)
It also records a realistic GitHub Actions log of the failing run.

    python -m benchmark.build                 # all cases
    python -m benchmark.build --only toolz-tail-slice
Results: benchmark/.cache/cases/<id>/meta.json and benchmark/.cache/build_report.json
"""
from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
PROJECT = ROOT.parent
CACHE = ROOT / ".cache"
sys.path.insert(0, str(PROJECT))

from app.policy import Policy                    # noqa: E402
from app.sandbox import DockerSandbox, SandboxResult  # noqa: E402
from benchmark.cases import CASES, REPOS          # noqa: E402

HIDDEN_NAME = "test_greenlight_hidden.py"
BENCH_DEPS = ["text-unidecode==1.3", "freezegun==1.5.5", "wcwidth==0.2.13"]


def git(repo: Path, *args: str, check: bool = True) -> str:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_AUTHOR_NAME": "Dev", "GIT_AUTHOR_EMAIL": "dev@example.com",
           "GIT_COMMITTER_NAME": "Dev", "GIT_COMMITTER_EMAIL": "dev@example.com"}
    proc = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, env=env)
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def deps_dir() -> Path:
    """Test-only dependencies some projects need, unpacked once (pure-Python wheels)."""
    d = CACHE / "deps"
    marker = d / ".done"
    if not marker.exists():
        shutil.rmtree(d, ignore_errors=True)
        subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "--no-compile", "--only-binary=:all:",
                        "--no-deps", "--target", str(d), *BENCH_DEPS, "python-dateutil==2.9.0.post0", "six==1.17.0"],
                       check=True)
        marker.write_text("ok")
    return d


def source_tree(name: str) -> Path:
    cfg = REPOS[name]
    if cfg["source"].startswith("local:"):
        return PROJECT / cfg["source"].split(":", 1)[1]
    dest = CACHE / "src" / name
    if not (dest / ".git").exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "-q", cfg["source"], str(dest)], check=True)
    git(dest, "checkout", "-q", cfg["sha"])
    return dest


def apply_edit(repo: Path, path: str, old: str | None, new: str) -> None:
    target = repo / path
    if old is None:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(new)
        return
    text = target.read_text()
    count = text.count(old)
    if count != 1:
        raise ValueError(f"{path}: expected the text to appear once, found {count}: {old[:60]!r}")
    target.write_text(text.replace(old, new, 1))


def policy_for(name: str) -> Policy:
    cfg = REPOS[name]
    return Policy(test_command=cfg["test_command"], allowed_paths=cfg["allowed_paths"])


def policy_yaml(name: str) -> str:
    cfg = REPOS[name]
    return yaml.safe_dump({
        "mode": "pull_request", "test_command": cfg["test_command"],
        "install_command": ["pip", "install", "-r", "requirements.txt"],
        "allowed_paths": cfg["allowed_paths"], "max_files_changed": 3, "max_lines_changed": 40,
        "allow_test_edits": "flag",
    }, sort_keys=False)


def run_at(repo: Path, sha: str, name: str, extra_files: dict[str, str] | None = None,
           targets: list[str] | None = None) -> SandboxResult:
    """Run the project's visible test command at a commit, inside the Docker sandbox."""
    with tempfile.TemporaryDirectory(prefix="gl-bench-") as tmp:
        work = Path(tmp) / "repo"
        git(Path(tmp), "clone", "-q", str(repo), str(work))
        git(work, "checkout", "-q", sha)
        for rel, content in (extra_files or {}).items():
            (work / rel).write_text(content)
        os.chmod(tmp, 0o755)
        policy = policy_for(name)
        if targets:      # run only these test files
            policy.test_command = [c for c in policy.test_command if c not in ("tests", "test", "toolz/tests")]
        return DockerSandbox().run_tests(work, deps_dir(), policy, "3.12", targets)


def actions_log(test_output: str, command: list[str], seed: str) -> str:
    """Wrap test output the way GitHub Actions prints a job log."""
    rnd = random.Random(seed)
    lines = ["##[group]Run actions/checkout@v5", "Syncing repository", "##[endgroup]",
             "##[group]Run actions/setup-python@v6", "Successfully set up CPython (3.12.7)", "##[endgroup]",
             "##[group]Run pip install -r requirements.txt"]
    for k in range(rnd.randint(80, 160)):
        pkg = rnd.choice(["pytest", "pluggy", "iniconfig", "packaging", "coverage", "setuptools", "wheel"])
        lines.append(rnd.choice([f"Collecting {pkg}>=1.{k % 7}", f"  Downloading {pkg}-1.{k % 7}.0-py3-none-any.whl (41 kB)",
                                 "     ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 41.2/41.2 kB 6.0 MB/s eta 0:00:00"]))
    lines += ["Successfully installed pytest pluggy iniconfig packaging", "##[endgroup]",
              f"##[group]Run {' '.join(command)}", f"{' '.join(command)}", "shell: /usr/bin/bash -e {0}", "env:",
              "  pythonLocation: /opt/hostedtoolcache/Python/3.12.7/x64",
              "  LD_LIBRARY_PATH: /opt/hostedtoolcache/Python/3.12.7/x64/lib", "##[endgroup]"]
    lines += [l for l in test_output.splitlines() if "termcap" not in l and "dumb terminal" not in l]
    lines.append("##[error]Process completed with exit code 1.")
    start = datetime(2026, 10, 20, 9, 0, tzinfo=timezone.utc)
    return "\n".join(f"{(start + timedelta(milliseconds=29 * i)).strftime('%Y-%m-%dT%H:%M:%S.%f')}0Z {l}"
                     for i, l in enumerate(lines)) + "\n"


def build_case(case: dict) -> dict:
    name = case["repo"]
    cfg = REPOS[name]
    src = source_tree(name)
    dest = CACHE / "cases" / case["id"]
    shutil.rmtree(dest, ignore_errors=True)
    shutil.copytree(src, dest, ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache"))
    for path, old, new in case.get("green_edits", []):
        apply_edit(dest, path, old, new)
    (dest / ".greenlight.yml").write_text(policy_yaml(name))
    git(dest, "init", "-q", "-b", "main")
    git(dest, "add", "-A")
    upstream = cfg.get("sha", "demo")[:12]
    git(dest, "commit", "-q", "-m", f"Import {name} ({upstream})")
    green = git(dest, "rev-parse", "HEAD")

    for path, old, new in case.get("edits", []):
        apply_edit(dest, path, old, new)
    for path, content in case.get("new_files", []):
        apply_edit(dest, path, None, content)
    git(dest, "add", "-A")
    git(dest, "commit", "-q", "-m", case["message"])
    broken = git(dest, "rev-parse", "HEAD")

    problems = []
    green_run = run_at(dest, green, name)
    if not green_run.passed:
        problems.append("visible tests fail on green: " + green_run.output[-600:])
    if case.get("hidden"):
        hidden_rel = f"{cfg['hidden_dir']}/{HIDDEN_NAME}"
        if case["category"] == "broken_test":
            # The code change was intended: the hidden tests describe the NEW behaviour,
            # so they must pass on the broken commit's code and fail on the old code.
            on_new = run_at(dest, broken, name, {hidden_rel: case["hidden"]}, targets=[hidden_rel])
            if not on_new.passed:
                problems.append("hidden tests do not pass on the intended (broken) code: " + on_new.output[-900:])
        else:
            hidden_green = run_at(dest, green, name, {hidden_rel: case["hidden"]})
            if not hidden_green.passed:
                problems.append("hidden tests fail on green: " + hidden_green.output[-900:])

    if case["category"] == "infra":
        ci_log = actions_log(case["ci_log"], cfg["test_command"], case["id"])
    else:
        broken_run = None
        for _ in range(12):           # a flaky test may pass by luck; CI only reports failures
            broken_run = run_at(dest, broken, name)
            if not broken_run.passed:
                break
        if broken_run.passed:
            problems.append("visible tests pass on broken")
        ci_log = actions_log(broken_run.output, cfg["test_command"], case["id"])

    (dest.parent / f"{case['id']}.log").write_text(ci_log)
    meta = {"id": case["id"], "repo": name, "category": case["category"], "expected": case["expected"],
            "green": green, "broken": broken, "path": str(dest), "valid": not problems, "problems": problems,
            "log_lines": ci_log.count("\n")}
    (dest.parent / f"{case['id']}.json").write_text(json.dumps(meta, indent=2))
    return meta


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", action="append")
    args = parser.parse_args(argv)
    os.environ.setdefault("SANDBOX_TIMEOUT_SECONDS", "240")
    from app import config
    config.reload_settings()
    cases = [c for c in CASES if not args.only or c["id"] in args.only]
    report = []
    for case in cases:
        try:
            meta = build_case(case)
        except Exception as exc:
            meta = {"id": case["id"], "valid": False, "problems": [f"{type(exc).__name__}: {exc}"]}
        report.append(meta)
        status = "ok " if meta["valid"] else "BAD"
        print(f"{status} {case['id']}", "" if meta["valid"] else "| " + " | ".join(meta["problems"])[:500], flush=True)
    (CACHE / "build_report.json").write_text(json.dumps(report, indent=2))
    bad = [m for m in report if not m["valid"]]
    print(f"\n{len(report) - len(bad)} valid, {len(bad)} invalid")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
