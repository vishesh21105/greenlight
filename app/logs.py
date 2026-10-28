"""Log distillation: cut a long CI log down to the few lines that explain the failure.

No AI is involved here. Plain rules find the error messages, the traceback and the
failing test names, keep a little context around them, and drop everything else
(install output, progress bars, passing tests).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# GitHub Actions prefixes every line with a timestamp like 2026-10-28T19:44:20.1234567Z
_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z ?")
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_GH_MARKER = re.compile(r"^##\[(group|endgroup|section|command)\]")

# pytest summary lines, e.g. "FAILED tests/test_cart.py::test_total - NameError: ..."
_PYTEST_FAILED = re.compile(r"^(FAILED|ERROR)\s+([\w./\\-]+\.py(?:::[\w\[\]\-.,=/ ]+?)?)(?:\s+-\s+(.*))?$")
_ERROR_TYPE = re.compile(r"\b([A-Z][A-Za-z]*(?:Error|Exception|Exit|Interrupt))\b")

# Lines worth keeping, strongest signals first.
_IMPORTANT = [
    re.compile(r"Traceback \(most recent call last\)"),
    re.compile(r"^E\s{2,}"),                                  # pytest's explanation lines
    re.compile(r"^(FAILED|ERROR)\s"),
    re.compile(r"=+ (short test summary info|FAILURES|ERRORS) =+"),
    re.compile(r"^[\w./\\-]+\.py:\d+: \w+"),                  # tests/test_x.py:12: AssertionError
    re.compile(r"\b\w*(Error|Exception)\b:"),
    re.compile(r"Process completed with exit code \d+"),
    re.compile(r"No space left on device|Could not resolve host|ETIMEDOUT|Connection reset"),
    re.compile(r"No matching distribution found|ResolutionImpossible|Could not find a version"),
    re.compile(r"^##\[error\]"),
]

_NOISE = [
    re.compile(r"^\s*(Collecting|Downloading|Using cached|Requirement already satisfied|Installing collected|Successfully installed|Building wheel|Created wheel|Stored in directory)\b"),
    re.compile(r"^\s*━|^\s*\|?\s*[█▉▊▋▌▍▎▏ ]+\|"),            # progress bars
    re.compile(r"PASSED|::\w+ PASSED"),
]


@dataclass
class DistilledLog:
    text: str
    raw_lines: int
    kept_lines: int
    failing_tests: list[str] = field(default_factory=list)
    error_types: list[str] = field(default_factory=list)
    key_lines: list[str] = field(default_factory=list)

    @property
    def reduction(self) -> float:
        """Share of the log that was kept (0.02 means 2%)."""
        return self.kept_lines / self.raw_lines if self.raw_lines else 0.0


def clean_line(line: str) -> str:
    line = _TIMESTAMP.sub("", line.rstrip("\r\n"))
    line = _ANSI.sub("", line)
    return line


def _is_noise(line: str) -> bool:
    return any(p.search(line) for p in _NOISE)


def _importance(line: str) -> int:
    for rank, pattern in enumerate(_IMPORTANT):
        if pattern.search(line):
            return len(_IMPORTANT) - rank
    return 0


def distill(raw_log: str, context: int = 20, max_lines: int = 150) -> DistilledLog:
    """Return the useful part of a CI log.

    context:   lines kept before/after each important line
    max_lines: hard cap on the result; the end of the log is preferred,
               because the real error is usually near the end.
    """
    raw = raw_log.splitlines()
    lines = [clean_line(l) for l in raw]
    lines = [l for l in lines if not _GH_MARKER.match(l)]

    failing: list[str] = []
    errors: list[str] = []
    important_idx: list[int] = []
    for i, line in enumerate(lines):
        m = _PYTEST_FAILED.match(line.strip())
        if m and m.group(2) not in failing:
            failing.append(m.group(2).strip())
        if _importance(line):
            important_idx.append(i)
            for err in _ERROR_TYPE.findall(line):
                if err not in errors:
                    errors.append(err)

    # Mark which lines to keep: each important line plus some context around it.
    keep = [False] * len(lines)
    for i in important_idx:
        lo, hi = max(0, i - context), min(len(lines), i + context + 1)
        for j in range(lo, hi):
            keep[j] = True
    # If nothing looked important, keep the last lines of the log.
    if not important_idx:
        for j in range(max(0, len(lines) - 40), len(lines)):
            keep[j] = True

    kept = [(i, lines[i]) for i in range(len(lines)) if keep[i] and lines[i].strip() and not _is_noise(lines[i])]

    if len(kept) > max_lines:
        # Over budget: keep the most important lines first, then fill with the latest context.
        must = {i for i in important_idx}
        chosen = [k for k in kept if k[0] in must][-max_lines:]
        remaining = max_lines - len(chosen)
        if remaining > 0:
            others = [k for k in kept if k[0] not in must][-remaining:]
            chosen = sorted(chosen + others)
        kept = sorted(chosen)

    # Rebuild text, marking gaps so the reader knows lines were skipped.
    out: list[str] = []
    prev = None
    for i, line in kept:
        # Only mark a gap when real (non-blank) lines were dropped.
        if prev is not None and any(lines[j].strip() for j in range(prev + 1, i)):
            out.append("[... skipped ...]")
        out.append(line)
        prev = i

    key_lines = [lines[i] for i in important_idx if lines[i].strip()][-15:]
    return DistilledLog(
        text="\n".join(out),
        raw_lines=len(raw),
        kept_lines=len(kept),
        failing_tests=failing,
        error_types=errors,
        key_lines=key_lines,
    )


def safe_test_targets(failing_tests: list[str]) -> list[str]:
    """Turn pytest ids into arguments we can pass back to pytest.

    Keeps only safe characters, so a log line can never inject extra options.
    """
    safe = re.compile(r"^[\w./\-]+\.py(::[\w\[\]\-.]+)*$")
    targets = []
    for test_id in failing_tests:
        test_id = test_id.strip()
        if safe.match(test_id) and not test_id.startswith("-") and ".." not in test_id:
            targets.append(test_id)
    return targets
