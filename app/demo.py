"""Offline demo: real broken commits, a simulated CI run, and the full GreenLight pipeline.

No GitHub and no AI key needed. If LLM_API_KEY and LLM_MODEL are set, the real model is
used; otherwise a scripted "replay" model stands in, clearly labelled on the timeline.

    python -m app.demo              # create the demo incidents and process them now
    python -m app.demo --enqueue    # only queue them (a running server's worker picks them up)
    python -m app.demo --only rename
"""
from __future__ import annotations

import argparse
import json
import random
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import config
from app.db import add_event, init_db, session_scope
from app.github_client import LocalCodeHost, run_git
from app.jobs import enqueue
from app.llm import LLMResponse, ScriptedProvider, ToolCall, get_llm
from app.models import Incident, Repository
from app.pipeline import Services, process_incident
from app.sandbox import LocalSandbox
from sqlmodel import select

DEMO_SRC = Path(__file__).parent.parent / "demo" / "tinyshop"


# --------------------------------------------------------------------------- repo + CI simulation

def _git(repo: Path, *args: str) -> str:
    return run_git(list(args), repo)


def make_source_repo(dest: Path) -> tuple[Path, str]:
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(DEMO_SRC, dest)
    _git(dest, "init", "-q", "-b", "main")
    _git(dest, "config", "user.email", "dev@example.com")
    _git(dest, "config", "user.name", "Priya Dev")
    _git(dest, "add", "-A")
    _git(dest, "commit", "-q", "-m", "Initial shop")
    return dest, _git(dest, "rev-parse", "HEAD")


def simulate_ci_log(repo: Path, sha: str, work: Path) -> str:
    """Run the tests at `sha` and wrap the output like a GitHub Actions log."""
    checkout = work / f"ci-{sha[:8]}"
    if checkout.exists():
        shutil.rmtree(checkout)
    run_git(["clone", "-q", str(repo), str(checkout)])
    run_git(["checkout", "-q", sha], checkout)
    env = {"PATH": "/usr/bin:/bin", "PYTHONPATH": str(checkout), "PYTHONDONTWRITEBYTECODE": "1", "HOME": str(work)}
    proc = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-rfE"], cwd=checkout,
                          capture_output=True, text=True, env={**env, "PATH": f"{Path(sys.executable).parent}:/usr/bin:/bin"})
    shutil.rmtree(checkout, ignore_errors=True)
    rnd = random.Random(sha)
    t = datetime(2026, 10, 28, 10, 0, tzinfo=timezone.utc)
    lines = ["##[group]Run actions/checkout@v4", f"HEAD is now at {sha[:7]}", "##[endgroup]",
             "##[group]Run actions/setup-python@v5", "Successfully set up CPython (3.12.7)", "##[endgroup]",
             "##[group]Run pip install -r requirements.txt"]
    for k in range(220):
        pkg = rnd.choice(["pytest", "pluggy", "iniconfig", "packaging", "colorama", "tomli"])
        lines.append(rnd.choice([f"Collecting {pkg}>=1.{k % 7}", f"  Downloading {pkg}-1.{k % 7}.0-py3-none-any.whl (32 kB)",
                                 "     ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 32.1/32.1 kB 4.1 MB/s eta 0:00:00"]))
    lines += ["Successfully installed iniconfig packaging pluggy pytest", "##[endgroup]", "##[group]Run python -m pytest -q"]
    lines += (proc.stdout + proc.stderr).splitlines()
    if proc.returncode:
        lines.append(f"##[error]Process completed with exit code {proc.returncode}.")
    out = []
    for i, line in enumerate(lines):
        stamp = (t + timedelta(milliseconds=37 * i)).strftime("%Y-%m-%dT%H:%M:%S.%f") + "0Z"
        out.append(f"{stamp} {line}")
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------- scenarios

def _tool(tool_name: str, **args) -> LLMResponse:
    return LLMResponse(text=None, tool_calls=[ToolCall(f"call_{tool_name}_{random.randint(0, 99999)}", tool_name, args, json.dumps(args))],
                       input_tokens=2500, output_tokens=180)


def scenario_rename(repo: Path) -> dict:
    """A rename that missed a caller: GreenLight should open a verified fix."""
    pricing = repo / "tinyshop" / "pricing.py"
    pricing.write_text(pricing.read_text().replace("def compute_total", "def calc_total"))
    tp = repo / "tests" / "test_pricing.py"
    tp.write_text(tp.read_text().replace("compute_total", "calc_total"))
    _git(repo, "commit", "-qam", "Rename compute_total to calc_total for clarity")
    script = [
        LLMResponse(text="The commit renamed a function. Let me check what changed and who still uses the old name."),
        _tool("get_commit_diff"),
        _tool("find_symbol", name="compute_total"),
        _tool("read_file", path="tinyshop/checkout.py"),
        _tool("propose_patch", edits=[
            {"path": "tinyshop/checkout.py",
             "old_text": "from tinyshop.pricing import add_gst, apply_discount, compute_total",
             "new_text": "from tinyshop.pricing import add_gst, apply_discount, calc_total"},
            {"path": "tinyshop/checkout.py", "old_text": "subtotal = compute_total(cart)",
             "new_text": "subtotal = calc_total(cart)"}],
            rationale="The failing commit renamed `compute_total` to `calc_total` in tinyshop/pricing.py:6 and "
                      "updated the tests, but tinyshop/checkout.py:2 still imports the old name and :7 calls it, "
                      "so importing the checkout module fails. Updating both references completes the rename."),
    ]
    return {"message": "rename", "script": script}


def scenario_discount(repo: Path) -> dict:
    """An ambiguous change: the right fix depends on intent, so the agent should give up honestly."""
    pricing = repo / "tinyshop" / "pricing.py"
    pricing.write_text(pricing.read_text().replace("return amount * (100 - percent) // 100",
                                                   "return -(-amount * (100 - percent) // 100)  # round up"))
    _git(repo, "commit", "-qam", "Round discounted prices up instead of down")
    tp = repo / "tests" / "test_pricing.py"
    tp.write_text(tp.read_text() + "\n\ndef test_apply_discount_rounds_down():\n    assert apply_discount(999, 10) == 899\n")
    _git(repo, "commit", "-qam", "Add rounding test")

    def script(messages, tools):
        if tools is None:   # the classification question
            return LLMResponse(text='{"category": "broken_test", "confidence": 0.55, "reason": "A rounding rule changed on purpose and a test expects the old rule"}',
                               input_tokens=1200, output_tokens=40)
        n = sum(1 for m in messages if m["role"] == "assistant")
        steps = [
            _tool("get_commit_diff"),
            _tool("read_file", path="tinyshop/pricing.py", start_line=11, end_line=16),
            _tool("search_code", query="rounds_down"),
            _tool("give_up",
                  diagnosis="tinyshop/pricing.py:15 now rounds discounted prices UP (commit message says this is "
                            "intended), while tests/test_pricing.py:28 expects rounding DOWN (899). The code and the "
                            "newest test disagree about the business rule.",
                  reason="Which rounding rule is correct is a product decision. Changing either the code or the "
                         "test could hide a real bug, so a human should decide."),
        ]
        return steps[min(n, len(steps) - 1)]
    return {"message": "discount", "script": script}


def scenario_flaky(repo: Path) -> dict:
    """A test that fails on alternate runs: GreenLight should call it flaky and not patch."""
    (repo / "tests" / "test_stock.py").write_text(
        "import os\nfrom pathlib import Path\n\n\n"
        "def test_stock_sync():\n"
        "    # Simulates a race with an external stock service: fails on alternate runs.\n"
        "    marker = Path(os.environ.get('HOME', '/tmp')) / '.stock_sync_toggle'\n"
        "    flip = not marker.exists()\n"
        "    marker.touch() if flip else marker.unlink()\n"
        "    assert not flip, 'stock service returned stale data'\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "Add stock sync check")
    return {"message": "flaky", "script": []}


def scenario_infra(repo: Path) -> dict:
    """A runner problem: GreenLight should recognise it from the log alone."""
    (repo / "README.md").write_text((repo / "README.md").read_text() + "\nNow with GST support.\n")
    _git(repo, "commit", "-qam", "Update README")
    log = ("2026-10-28T11:02:01.0000000Z ##[group]Run python -m pytest -q\n"
           "2026-10-28T11:02:03.0000000Z OSError: [Errno 28] No space left on device: '/home/runner/work/_temp'\n"
           "2026-10-28T11:02:03.1000000Z ##[error]Process completed with exit code 1.\n")
    return {"message": "infra", "script": [], "log": log}


SCENARIOS = {"rename": scenario_rename, "discount": scenario_discount, "flaky": scenario_flaky, "infra": scenario_infra}


# --------------------------------------------------------------------------- running

def create_incident(name: str, root: Path) -> tuple[str, list | object]:
    repo, green = make_source_repo(root / f"tinyshop-{name}")
    info = SCENARIOS[name](repo)
    broken = _git(repo, "rev-parse", "HEAD")
    with session_scope() as s:
        full_name = f"demo/tinyshop-{name}"
        r = s.exec(select(Repository).where(Repository.full_name == full_name)).first()
        if r is None:
            r = Repository(full_name=full_name, clone_url=str(repo))
            s.add(r)
            s.flush()
        else:
            r.clone_url = str(repo)
            s.add(r)
        inc = Incident(repository_id=r.id, run_id=None, branch="main", failing_sha=broken, last_green_sha=green)
        s.add(inc)
    ws = Path(config.settings.workspace_dir) / inc.id
    ws.mkdir(parents=True, exist_ok=True)
    log = info.get("log") or simulate_ci_log(repo, broken, root)
    (ws / "ci_log.txt").write_text(log)
    add_event(inc.id, "state", f"Received a failed CI run on main ({broken[:8]}) [offline demo: {name}]",
              {"state": "received"})
    return inc.id, info["script"]


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="GreenLight offline demo")
    parser.add_argument("--only", choices=sorted(SCENARIOS), action="append")
    parser.add_argument("--enqueue", action="store_true", help="queue for a running server instead of processing now")
    args = parser.parse_args(argv)

    config.settings.sandbox_backend = "local-noinstall"
    init_db()
    root = Path(config.settings.workspace_dir).resolve() / "_demo_sources"
    root.mkdir(parents=True, exist_ok=True)
    real_llm = get_llm()

    for name in args.only or ["rename", "discount", "flaky", "infra"]:
        incident_id, script = create_incident(name, root)
        if args.enqueue:
            enqueue(incident_id)
            print(f"queued {name}: {incident_id}")
            continue
        llm = real_llm or ScriptedProvider(script, model="offline-replay")
        if real_llm is None:
            add_event(incident_id, "warning", "No AI model configured: using an offline scripted replay of the agent.")
        services = Services(code_host=LocalCodeHost(), sandbox=LocalSandbox(install_deps=False), llm=llm)
        process_incident(incident_id, services)
        with session_scope() as s:
            inc = s.get(Incident, incident_id)
            print(f"{name:9s} -> {inc.state:16s} {inc.pr_url or ''}  ({incident_id})")
    if not args.enqueue:
        print("\nOpen the dashboard:  uvicorn app.main:app --port 8000   then visit http://localhost:8000")


if __name__ == "__main__":
    main()
