"""One day's batch of the benchmark, sized to fit a free AI quota.

Order of work (each step resumes where the previous day stopped):
  1. GreenLight on every case
  2. The log-only baseline on every case (1 AI call each)
  3. Ablation "no reproduction step" on the flaky cases and a sample of code bugs
  4. Ablation "raw log" on a sample of code bugs
Then the report is regenerated.

    python -m benchmark.daily --budget 45
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent))

ABLATION_NO_REPRO = ["shop-flaky-price-feed", "toolz-flaky-sampling", "cache-flaky-timing", "slugify-flaky-network",
                     "shop-rename-missed-caller", "toolz-tail-slice", "cache-lru-evicts-newest", "slugify-stopword-case"]
ABLATION_RAW_LOG = ["shop-rename-missed-caller", "shop-missing-import", "toolz-tail-slice", "toolz-private-rename",
                    "cache-lru-evicts-newest", "cache-get-default", "slugify-stopword-case", "tabulate-padleft-direction"]


def done(mode: str) -> set[str]:
    path = ROOT / "results" / f"{mode}.jsonl"
    if not path.exists():
        return set()
    return {json.loads(l)["id"] for l in path.read_text().splitlines() if l.strip()}


def valid_cases() -> set[str]:
    return {json.loads(p.read_text())["id"] for p in (ROOT / ".cache" / "cases").glob("*.json")
            if json.loads(p.read_text()).get("valid")}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--budget", type=int, default=45, help="AI calls to spend today")
    args = parser.parse_args(argv)
    from benchmark import report, run

    cases = valid_cases()
    plan = [("greenlight", sorted(cases)), ("log_only", sorted(cases)),
            ("no_repro", [c for c in ABLATION_NO_REPRO if c in cases]),
            ("raw_log", [c for c in ABLATION_RAW_LOG if c in cases])]
    budget = args.budget
    for mode, wanted in plan:
        todo = [c for c in wanted if c not in done(mode)]
        if not todo:
            continue
        if budget < (2 if mode == "log_only" else 9):
            break
        argv_run = ["--mode", mode, "--skip-done", "--max-calls", str(budget)]
        for c in todo:
            argv_run += ["--only", c]
        print(f"== {mode}: {len(todo)} case(s) left, budget {budget} calls", flush=True)
        used = run_and_count(run.main, argv_run, mode)
        budget -= used
        if used == 0 or budget <= 0:
            break
    report.main()
    return 0


def run_and_count(run_main, argv_run: list[str], mode: str) -> int:
    """Run one mode and return how many AI calls it spent (read back from the results file)."""
    path = ROOT / "results" / f"{mode}.jsonl"
    seen = len(path.read_text().splitlines()) if path.exists() else 0
    run_main(argv_run)
    if not path.exists():
        return 0
    new = [json.loads(l) for l in path.read_text().splitlines()[seen:] if l.strip()]
    return sum(r.get("llm_calls") or 0 for r in new) or (1 if new else 0)


if __name__ == "__main__":
    sys.exit(main())
