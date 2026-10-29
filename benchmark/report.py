"""Turn benchmark/results/*.jsonl into benchmark/results/REPORT.md (numbers + every failure).

    python -m benchmark.report
"""
from __future__ import annotations

import json
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RESULTS = ROOT / "results"
MODES = ["greenlight", "log_only", "no_repro", "raw_log"]
MODE_NAMES = {"greenlight": "GreenLight", "log_only": "Baseline: ask the AI once (no tools, no sandbox)",
              "no_repro": "Ablation: no reproduction step", "raw_log": "Ablation: raw log instead of trimmed log"}


def load(mode: str) -> list[dict]:
    path = RESULTS / f"{mode}.jsonl"
    if not path.exists():
        return []
    latest: dict[str, dict] = {}
    for line in path.read_text().splitlines():
        if line.strip():
            row = json.loads(line)
            latest[row["id"]] = row          # keep the most recent run of each case
    return list(latest.values())


def pct(n: int, d: int) -> str:
    return f"{n}/{d} ({100 * n / d:.0f}%)" if d else "n/a"


def summarize(rows: list[dict]) -> dict:
    fix_cases = [r for r in rows if r["expected"] == "fix"]
    prs = [r for r in rows if r["verdict"]["opened_pr"]]
    wrong = [r for r in rows if r["verdict"]["wrong_fix"]]
    fixed_ok = [r for r in fix_cases if r["verdict"]["success"]]
    seconds = [r["seconds"] for r in fixed_ok if r.get("seconds")]
    own = [max(0.0, r["seconds"] - (r.get("ai_seconds") or 0)) for r in fixed_ok if r.get("seconds") and "ai_seconds" in r]
    calls = [r.get("llm_calls", 0) for r in rows]
    tokens = sum((r.get("input_tokens") or 0) + (r.get("output_tokens") or 0) for r in rows)
    kept = [r["trimmed_log_lines"] / r["raw_log_lines"] for r in rows if r.get("raw_log_lines") and r.get("trimmed_log_lines")]
    classified = [r for r in rows if r["verdict"].get("classified_right") is not None]
    nonfix = [r for r in rows if r["expected"] != "fix"]
    injection = [r for r in rows if "unsafe_patch_attempts" in r["verdict"]]
    return {
        "cases": len(rows),
        "fix_rate": pct(len(fixed_ok), len(fix_cases)),
        "pr_precision": pct(len(prs) - len(wrong), len(prs)),
        "wrong_fix": pct(len(wrong), len(prs)),
        "nonfix_right": pct(sum(r["verdict"]["success"] for r in nonfix), len(nonfix)),
        "classified": pct(sum(r["verdict"]["classified_right"] for r in classified), len(classified)),
        "median_seconds": f"{statistics.median(seconds):.0f}s" if seconds else "n/a",
        "median_own_seconds": f"{statistics.median(own):.0f}s" if own else "n/a",
        "calls_per_case": f"{statistics.mean(calls):.1f}" if calls else "n/a",
        "tokens_per_fix": f"{tokens / len(fixed_ok):,.0f}" if fixed_ok else "n/a",
        "log_kept": f"{100 * statistics.mean(kept):.1f}%" if kept else "n/a",
        "injection_unsafe_merged": sum(1 for r in injection if r["verdict"]["wrong_fix"]),
        "injection_cases": len(injection),
        "secret_leaks": sum(1 for r in rows if r["verdict"].get("secret_leaked")),
    }


def main() -> int:
    data = {m: load(m) for m in MODES}
    data = {m: rows for m, rows in data.items() if rows}
    if not data:
        print("No results yet. Run: python -m benchmark.run --mode greenlight")
        return 1
    out = ["# GreenLight benchmark results", ""]
    gl = data.get("greenlight", [])
    if gl:
        models = Counter()
        for r in gl:
            if r.get("calls_by_model"):
                models.update(r["calls_by_model"])
            else:
                models.update({m: 1 for m in r.get("models", [])})
        if all(r.get("calls_by_model") for r in gl):
            model_line = "Model(s): " + ", ".join(f"{m} ({n} calls)" for m, n in models.most_common())
        else:
            model_line = "Model(s) used: " + ", ".join(m for m, _ in models.most_common())
        out += [f"Cases: {len(gl)} ({sum(r['set'] == 'A' for r in gl)} seeded, {sum(r['set'] == 'B' for r in gl)} "
                f"real historical bugs) across {len({r['repo'] for r in gl})} projects. {model_line}.", ""]

    labels = [("Cases run", "cases"), ("Fix rate (verified fix that also passes hidden tests)", "fix_rate"),
              ("PR precision (opened fixes that are correct)", "pr_precision"),
              ("Wrong-fix rate (opened fixes that are wrong)", "wrong_fix"),
              ("Flaky / infra handled without a patch", "nonfix_right"),
              ("Failure kind classified correctly", "classified"),
              ("Median time to a correct fix (including waits for the free AI tier)", "median_seconds"),
              ("Median GreenLight time to a correct fix (excluding AI wait)", "median_own_seconds"),
              ("AI calls per case", "calls_per_case"),
              ("Tokens per correct fix", "tokens_per_fix"), ("Share of log sent to the AI", "log_kept"),
              ("Secret leaks", "secret_leaks")]
    sums = {m: summarize(rows) for m, rows in data.items()}
    out += ["## Headline numbers", "", "| Metric | " + " | ".join(MODE_NAMES[m] for m in data) + " |",
            "| --- | " + " | ".join("---" for _ in data) + " |"]
    for label, key in labels:
        out.append(f"| {label} | " + " | ".join(str(sums[m][key]) for m in data) + " |")
    out.append("")

    if gl:
        by = defaultdict(list)
        for r in gl:
            by[(r["set"], r["category"])].append(r)
        out += ["## GreenLight by kind of case", "", "| Set | Kind | Cases | Success | Wrong fixes |", "| --- | --- | --- | --- | --- |"]
        for (st, cat), rows in sorted(by.items()):
            out.append(f"| {'seeded' if st == 'A' else 'historical'} | {cat.replace('_', ' ')} | {len(rows)} | "
                       f"{sum(r['verdict']['success'] for r in rows)} | {sum(r['verdict']['wrong_fix'] for r in rows)} |")
        out.append("")
        inj = [r for r in gl if "unsafe_patch_attempts" in r["verdict"]]
        if inj:
            out += ["## Prompt-injection cases", ""]
            for r in inj:
                out.append(f"- **{r['id']}**: outcome `{r['state']}`; unsafe patches the AI tried (all blocked by the "
                           f"checker): {r['verdict']['unsafe_patch_attempts']}; secret leaked: "
                           f"{'YES' if r['verdict'].get('secret_leaked') else 'no'}.")
            out.append("")
        out += ["## Every case", "", "| Case | Expected | Outcome | Result | Time | AI calls | Notes |",
                "| --- | --- | --- | --- | --- | --- | --- |"]
        for r in sorted(gl, key=lambda r: (r["set"], r["repo"], r["id"])):
            v = r["verdict"]
            notes = []
            if v["wrong_fix"]:
                notes.append("WRONG FIX")
            if r.get("reverts_recent_change"):
                notes.append("reverts the broken line (reviewer warned)")
            if r.get("touches_tests"):
                notes.append("edits a test (flagged)")
            sc = r.get("score") or {}
            if sc and sc.get("hidden_passes") is False:
                notes.append("hidden tests failed")
            out.append(f"| {r['id']} | {r['expected']} | {r['state']} | {'pass' if v['success'] else 'FAIL'} | "
                       f"{r.get('seconds', '')}s | {r.get('llm_calls', '')} | {'; '.join(notes)} |")
        out.append("")
        failures = [r for r in gl if not r["verdict"]["success"]]
        if failures:
            out += ["## Why the failures happened", ""]
            for r in failures:
                out.append(f"- **{r['id']}** ({r['state']}): {(r.get('rationale') or '').strip()[:400] or 'no explanation recorded'}")
            out.append("")
    out += ["## Method", "",
            "- Seeded cases: a realistic mistake is committed on top of a green commit of a real project. Hidden tests "
            "(never shown to GreenLight) decide whether a fix is really correct.",
            "- Historical cases: the maintainers' own bug-fix commits, replayed: the new regression test is added to the "
            "old code, and GreenLight must make it pass without breaking anything else.",
            "- All project code runs inside the Docker sandbox. Each case is run once; AI models are not deterministic, "
            "so small differences between runs are expected.",
            "- Runs use the free Gemini tier, which queues requests and limits calls per day, so total times include "
            "waiting for the AI. The row excluding AI wait shows GreenLight's own work (sandbox runs, tools, checks).",
            "- A case where the AI provider refused or timed out because of quota is not recorded; it runs again later.",
            "- Baseline: the same model is asked once with the end of the CI log, the commit diff and the changed files, "
            "with no tools, no reproduction and no verification. Its patch counts as a pull request if it applies.", ""]
    (RESULTS / "REPORT.md").write_text("\n".join(out))
    print("\n".join(out[:40]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
