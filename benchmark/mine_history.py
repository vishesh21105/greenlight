"""Set B: real bugs from the projects' own git history.

For an upstream commit C that fixed a bug AND added a test for it:
  green  = the project just before C            (old code, old tests: CI passes)
  broken = old code + C's new tests              (the new test exposes the real bug: CI fails)
  answer = C's change to the code                (what the maintainers actually did)
GreenLight only sees green and broken. A fix counts when the full suite (including the new
test) passes; we also record whether it touched the same files as the real fix.

    python -m benchmark.mine_history --max-per-repo 4
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent))

from benchmark.build import CACHE, actions_log, git, policy_yaml, run_at, source_tree  # noqa: E402
from benchmark.cases import REPOS  # noqa: E402

FIX_WORDS = re.compile(r"\b(fix|fixes|fixed|bug|regression|incorrect|wrong|crash|broken)\b", re.I)
SKIP_WORDS = re.compile(r"\b(typo|docs?|readme|ci|lint|flake8|mypy|typing|type hints?|deprecat|release|version|bump)\b", re.I)


def is_test(path: str) -> bool:
    return "/tests/" in f"/{path}" or "/test/" in f"/{path}" or Path(path).name.startswith("test_")


def candidates(name: str, src: Path, limit_commits: int) -> list[dict]:
    out = []
    shas = git(src, "rev-list", "--no-merges", f"--max-count={limit_commits}", REPOS[name]["sha"]).split()
    for sha in shas:
        msg = git(src, "log", "-1", "--format=%s", sha)
        if not FIX_WORDS.search(msg) or SKIP_WORDS.search(msg):
            continue
        stat = git(src, "show", "--numstat", "--format=", sha).splitlines()
        code, tests, changed = [], [], 0
        for line in stat:
            parts = line.split("\t")
            if len(parts) != 3 or not parts[2].endswith(".py") or "-" in parts[:2]:
                continue
            if is_test(parts[2]):
                tests.append(parts[2])
            else:
                code.append(parts[2])
                changed += int(parts[0]) + int(parts[1])
        if 1 <= len(code) <= 2 and tests and 1 <= changed <= 16:
            out.append({"sha": sha, "message": msg, "code_files": code, "test_files": tests, "changed": changed})
    return out


def build_historical(name: str, cand: dict) -> dict:
    src = source_tree(name)
    case_id = f"hist-{name}-{cand['sha'][:8]}"
    dest = CACHE / "cases" / case_id
    shutil.rmtree(dest, ignore_errors=True)
    parent = cand["sha"] + "^"

    # green = the tree just before the fix
    git(src, "checkout", "-q", parent)
    shutil.copytree(src, dest, ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache"))
    (dest / ".greenlight.yml").write_text(policy_yaml(name))
    git(dest, "init", "-q", "-b", "main")
    git(dest, "add", "-A")
    git(dest, "commit", "-q", "-m", f"Import {name} ({cand['sha'][:8]}^)")
    green = git(dest, "rev-parse", "HEAD")

    # broken = old code + the fix commit's tests
    for tf in cand["test_files"]:
        content = git(src, "show", f"{cand['sha']}:{tf}", check=False)
        if content:
            (dest / tf).parent.mkdir(parents=True, exist_ok=True)
            (dest / tf).write_text(content + "\n")
    git(dest, "add", "-A")
    git(dest, "commit", "-q", "-m", "Add a regression test for a reported bug")
    broken = git(dest, "rev-parse", "HEAD")

    # the maintainers' real fix, kept outside the case repo
    answer = git(src, "show", "--format=", cand["sha"], "--", *cand["code_files"])
    git(src, "checkout", "-q", REPOS[name]["sha"])

    problems = []
    if not run_at(dest, green, name).passed:
        problems.append("old tests fail on the old code")
    broken_run = run_at(dest, broken, name)
    if broken_run.passed:
        problems.append("new tests pass on the old code")
    # The real fix must make everything pass (proves the case is solvable).
    fixed_ok = False
    if not problems:
        import subprocess
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / "r"
            git(Path(tmp), "clone", "-q", str(dest), str(work))
            git(work, "checkout", "-q", broken)
            patch = Path(tmp) / "fix.patch"
            patch.write_text(answer + "\n")
            applied = subprocess.run(["git", "apply", "--recount", str(patch)], cwd=work, capture_output=True)
            if applied.returncode == 0:
                git(work, "add", "-A")
                git(work, "commit", "-q", "-m", "real fix")
                fixed_ok = run_at(work, git(work, "rev-parse", "HEAD"), name).passed
        if not fixed_ok:
            problems.append("the real upstream fix does not pass in this environment")

    ci_log = actions_log(broken_run.output, REPOS[name]["test_command"], case_id)
    (dest.parent / f"{case_id}.log").write_text(ci_log)
    (dest.parent / f"{case_id}.answer.diff").write_text(answer)
    meta = {"id": case_id, "repo": name, "set": "B", "category": "code_bug", "expected": "fix",
            "green": green, "broken": broken, "path": str(dest), "valid": not problems, "problems": problems,
            "upstream_commit": cand["sha"], "upstream_message": cand["message"], "answer_files": cand["code_files"],
            "log_lines": ci_log.count("\n")}
    (dest.parent / f"{case_id}.json").write_text(json.dumps(meta, indent=2))
    return meta


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-per-repo", type=int, default=4)
    parser.add_argument("--history", type=int, default=600, help="how many recent commits to scan per repo")
    args = parser.parse_args(argv)
    from app import config
    config.reload_settings()
    kept = []
    for name in ("toolz", "cachetools", "slugify", "tabulate"):
        src = source_tree(name)
        found = candidates(name, src, args.history)
        print(f"{name}: {len(found)} candidate fix commits", flush=True)
        n = 0
        for cand in found:
            if n >= args.max_per_repo:
                break
            try:
                meta = build_historical(name, cand)
            except Exception as exc:
                print(f"  skip {cand['sha'][:8]}: {type(exc).__name__}: {str(exc)[:120]}")
                git(src, "checkout", "-q", "-f", REPOS[name]["sha"])
                continue
            mark = "ok " if meta["valid"] else "-- "
            print(f"  {mark}{meta['id']}  {cand['message'][:70]}  {'' if meta['valid'] else meta['problems'][0]}", flush=True)
            if meta["valid"]:
                kept.append(meta["id"])
                n += 1
            else:
                shutil.rmtree(meta["path"], ignore_errors=True)
                for ext in (".json", ".log", ".answer.diff"):
                    (CACHE / "cases" / f"{meta['id']}{ext}").unlink(missing_ok=True)
    (CACHE / "historical.json").write_text(json.dumps(kept, indent=2))
    print(f"\n{len(kept)} historical cases kept")
    return 0


if __name__ == "__main__":
    sys.exit(main())
