"""The messages GreenLight posts on GitHub: short, evidence first."""
from __future__ import annotations

FOOTER = "\n\n---\n*GreenLight never merges anything. A human reviews every change.*"


def _code(text: str, lang: str = "") -> str:
    text = (text or "").replace("```", "'''")
    return f"```{lang}\n{text.strip()}\n```"


def pr_title(incident_short: str, summary: str) -> str:
    summary = (summary or "fix failing build").strip().splitlines()[0]
    return f"GreenLight: {summary[:80]} [{incident_short}]"


def pr_body(category: str, rationale: str, failing_tests: list[str], verify_failing: str,
            verify_full: str, touches_tests: bool, cost_usd: float, dashboard_url: str,
            reverted: list[str] | None = None) -> str:
    warn = ("\n> **Heads-up:** this patch changes a test file. Check that the test change is intended "
            "and does not hide a real bug.\n" if touches_tests else "")
    if reverted:
        sample = "\n".join(f"> `{l[:100]}`" for l in reverted[:3])
        warn += ("\n> **Heads-up:** this patch UNDOES part of the change that broke the build:\n" + sample +
                 "\n> If that change was intended, the test may be what is out of date. Please decide before merging.\n")
    return (
        f"## Why the build failed\n{rationale}\n{warn}\n"
        f"**Category:** `{category}`\n\n"
        f"## Evidence\n"
        f"- Reproduced the failure in an isolated sandbox before changing anything.\n"
        f"- Failing tests: {', '.join(f'`{t}`' for t in failing_tests) or '(whole suite)'}\n\n"
        f"## Verification (run by GreenLight's sandbox, not claimed by the AI)\n"
        f"- Previously failing tests: **{verify_failing}**\n"
        f"- Full test suite: **{verify_full}**\n\n"
        f"AI cost for this incident: ${cost_usd:.3f} · [Full timeline]({dashboard_url})"
        f"{FOOTER}"
    )


def diagnosis_comment(title: str, explanation: str, trimmed_log: str = "", dashboard_url: str = "") -> str:
    log = f"\n\n<details><summary>Trimmed CI log</summary>\n\n{_code(trimmed_log[-3000:])}\n</details>" if trimmed_log else ""
    link = f"\n\n[Full timeline]({dashboard_url})" if dashboard_url else ""
    return f"### GreenLight: {title}\n\n{explanation}{log}{link}{FOOTER}"


def summarize_test_output(output: str) -> str:
    """Pull pytest's last summary line, e.g. '118 passed in 2.31s'."""
    for line in reversed((output or "").splitlines()):
        line = line.strip().strip("=").strip()
        if any(word in line for word in ("passed", "failed", "error", "no tests ran")):
            return line
    return "no summary"
