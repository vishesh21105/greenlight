"""Patches: turning the agent's proposed edits into a checked, applicable diff.

Design choice: the agent does NOT write raw diff text (AI models often get the
line numbers in diff headers wrong). Instead it proposes simple edits:
"in file X, replace this exact text with that text". GreenLight applies them to a
copy, produces the unified diff itself, and then checks that diff against the
safety rules. The same checker also works on any diff from elsewhere.
"""
from __future__ import annotations

import difflib
import hashlib
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from app.policy import Policy, path_matches

_SKIP_MARKERS = re.compile(r"pytest\.mark\.(skip|skipif|xfail)|pytest\.skip\(|unittest\.skip|@skip\b")
_ASSERT = re.compile(r"^\s*(assert\b|self\.assert\w*\(|pytest\.raises\()")
_TEST_DEF = re.compile(r"^\s*(async\s+)?def\s+test_\w*\(")


@dataclass
class FileChange:
    old_path: str | None
    new_path: str | None
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    binary: bool = False
    new_file: bool = False
    deleted_file: bool = False
    mode_change: str | None = None

    @property
    def path(self) -> str:
        return self.new_path or self.old_path or ""


@dataclass
class ValidationResult:
    valid: bool
    errors: list[str]
    files: list[str]
    lines_changed: int
    touches_tests: bool


def patch_hash(diff_text: str) -> str:
    return hashlib.sha256(diff_text.encode()).hexdigest()[:16]


def is_test_path(path: str) -> bool:
    p = PurePosixPath(path)
    return (
        "tests" in p.parts or "test" in p.parts
        or p.name.startswith("test_") or p.name.endswith("_test.py") or p.name == "conftest.py"
    )


def _strip_prefix(path: str) -> str | None:
    path = path.strip().split("\t")[0]
    if path == "/dev/null":
        return None
    if path.startswith(("a/", "b/")):
        return path[2:]
    return path


def parse_diff(diff_text: str) -> list[FileChange]:
    files: list[FileChange] = []
    current: FileChange | None = None
    pending_meta: dict = {}
    lines = diff_text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("diff --git "):
            pending_meta = {}
            current = None
        elif line.startswith("new file mode"):
            pending_meta["new_file"] = True
        elif line.startswith("deleted file mode"):
            pending_meta["deleted_file"] = True
        elif line.startswith(("new mode", "old mode")):
            pending_meta["mode_change"] = line.split()[-1]
        elif line.startswith("Binary files") or line.startswith("GIT binary patch"):
            m = re.match(r"Binary files (\S+) and (\S+) differ", line)
            fc = FileChange(_strip_prefix(m.group(1)) if m else None,
                            _strip_prefix(m.group(2)) if m else None, binary=True)
            files.append(fc)
            current = fc
        elif line.startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
            current = FileChange(_strip_prefix(line[4:]), _strip_prefix(lines[i + 1][4:]), **pending_meta)
            if current.old_path is None:
                current.new_file = True
            if current.new_path is None:
                current.deleted_file = True
            files.append(current)
            pending_meta = {}
            i += 1
        elif current is not None and not current.binary:
            if line.startswith("+"):
                current.added.append(line[1:])
            elif line.startswith("-"):
                current.removed.append(line[1:])
        i += 1
    return files


def _path_problem(path: str, repo_dir: Path | None) -> str | None:
    if not path:
        return "empty file path"
    if path.startswith("/") or re.match(r"^[A-Za-z]:\\", path):
        return f"absolute path not allowed: {path}"
    if ".." in PurePosixPath(path).parts:
        return f"path escapes the repository: {path}"
    if repo_dir is not None:
        root = repo_dir.resolve()
        target = (repo_dir / path)
        # Any symlink on the way could point outside the repository.
        probe = repo_dir
        for part in PurePosixPath(path).parts:
            probe = probe / part
            if probe.is_symlink():
                return f"symlinks are not allowed: {path}"
        try:
            target.resolve().relative_to(root)
        except ValueError:
            return f"path escapes the repository: {path}"
    return None


def validate_diff(diff_text: str, policy: Policy, repo_dir: str | Path | None = None) -> ValidationResult:
    errors: list[str] = []
    repo = Path(repo_dir) if repo_dir else None
    if not diff_text.strip():
        return ValidationResult(False, ["patch is empty"], [], 0, False)

    files = parse_diff(diff_text)
    if not files:
        return ValidationResult(False, ["could not read the patch as a unified diff"], [], 0, False)

    touched: list[str] = []
    lines_changed = 0
    touches_tests = False
    for fc in files:
        for path in {p for p in (fc.old_path, fc.new_path) if p}:
            problem = _path_problem(path, repo)
            if problem:
                errors.append(problem)
                continue
            if path_matches(path, policy.all_blocked()):
                errors.append(f"path is blocked by policy: {path}")
            elif not path_matches(path, policy.allowed_paths):
                errors.append(f"path is not in allowed_paths: {path}")
        if fc.binary:
            errors.append(f"binary changes are not allowed: {fc.path}")
        if fc.mode_change:
            errors.append(f"file mode changes are not allowed: {fc.path}")
        if fc.deleted_file:
            errors.append(f"deleting files is not allowed: {fc.path}")
        touched.append(fc.path)
        lines_changed += len(fc.added) + len(fc.removed)

        if is_test_path(fc.path):
            touches_tests = True
            added_text = "\n".join(fc.added)
            if _SKIP_MARKERS.search(added_text):
                errors.append(f"patch skips or xfails tests in {fc.path}")
            removed_asserts = sum(1 for l in fc.removed if _ASSERT.match(l))
            added_asserts = sum(1 for l in fc.added if _ASSERT.match(l))
            if removed_asserts > added_asserts:
                errors.append(f"patch removes assertions from {fc.path}")
            removed_tests = sum(1 for l in fc.removed if _TEST_DEF.match(l))
            added_tests = sum(1 for l in fc.added if _TEST_DEF.match(l))
            if removed_tests > added_tests:
                errors.append(f"patch removes test functions from {fc.path}")
        else:
            if _SKIP_MARKERS.search("\n".join(fc.added)):
                errors.append(f"patch adds skip markers in {fc.path}")

    unique = sorted(set(touched))
    if len(unique) > policy.max_files_changed:
        errors.append(f"patch changes {len(unique)} files; the limit is {policy.max_files_changed}")
    if lines_changed > policy.max_lines_changed:
        errors.append(f"patch changes {lines_changed} lines; the limit is {policy.max_lines_changed}")
    if touches_tests and policy.allow_test_edits == "no":
        errors.append("policy does not allow changes to test files")

    return ValidationResult(not errors, errors, unique, lines_changed, touches_tests)


# --------------------------------------------------------------------------- edits -> diff

@dataclass
class Edit:
    path: str
    old_text: str
    new_text: str


def build_diff_from_edits(repo_dir: str | Path, edits: list[Edit]) -> tuple[str, list[str]]:
    """Apply text replacements in memory and return (unified_diff, errors).

    Each old_text must appear exactly once in its file, so an edit can never land
    in the wrong place. Creating a new file: old_text is empty and the file does not exist.
    """
    repo = Path(repo_dir)
    errors: list[str] = []
    originals: dict[str, str | None] = {}
    current: dict[str, str] = {}

    for e in edits:
        problem = _path_problem(e.path, repo)
        if problem:
            errors.append(problem)
            continue
        target = repo / e.path
        if e.path not in current:
            if target.exists():
                if not target.is_file():
                    errors.append(f"not a regular file: {e.path}")
                    continue
                text = target.read_text(errors="replace")
                originals[e.path] = text
                current[e.path] = text
            else:
                originals[e.path] = None
                current[e.path] = ""
        text = current[e.path]
        if originals[e.path] is None and e.old_text == "" and text == "":
            current[e.path] = e.new_text
            continue
        if e.old_text == "":
            errors.append(f"old_text is empty for existing file {e.path}")
            continue
        count = text.count(e.old_text)
        if count == 0:
            errors.append(f"old_text not found in {e.path}; read the file again and copy the text exactly")
            continue
        if count > 1:
            errors.append(f"old_text appears {count} times in {e.path}; include more surrounding lines")
            continue
        current[e.path] = text.replace(e.old_text, e.new_text, 1)

    if errors:
        return "", errors

    chunks = []
    for path in sorted(current):
        before = originals[path]
        after = current[path]
        if before == after:
            continue
        a = (before or "").splitlines(keepends=True)
        b = after.splitlines(keepends=True)
        if a and not a[-1].endswith("\n"):
            a[-1] += "\n"
        if b and not b[-1].endswith("\n"):
            b[-1] += "\n"
        header = f"diff --git a/{path} b/{path}\n"
        if before is None:
            header += "new file mode 100644\n"
        diff = difflib.unified_diff(a, b,
                                    fromfile="/dev/null" if before is None else f"a/{path}",
                                    tofile=f"b/{path}")
        chunks.append(header + "".join(diff))
    if not chunks:
        return "", ["the edits do not change anything"]
    return "".join(chunks), []


# --------------------------------------------------------------------------- applying

def apply_diff(repo_dir: str | Path, diff_text: str, check_only: bool = False) -> tuple[bool, str]:
    """Apply a diff with `git apply`. Arguments are fixed; nothing goes through a shell."""
    with tempfile.NamedTemporaryFile("w", suffix=".patch", delete=False) as fh:
        fh.write(diff_text)
        patch_file = fh.name
    try:
        args = ["git", "apply", "--recount", "--whitespace=nowarn"]
        if check_only:
            args.append("--check")
        proc = subprocess.run(args + [patch_file], cwd=str(repo_dir), capture_output=True, text=True,
                              timeout=30, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
        return proc.returncode == 0, (proc.stderr or proc.stdout).strip()
    finally:
        os.unlink(patch_file)


def reverted_lines(patch_diff: str, commit_diff: str) -> list[str]:
    """Lines the patch removes that the breaking commit had just added.

    If the fix undoes part of the developer's own change, a reviewer must be told:
    the change may have been intended, and the test may be what is out of date.
    """
    added_by_commit: dict[str, set[str]] = {}
    for fc in parse_diff(commit_diff):
        added_by_commit.setdefault(fc.path, set()).update(l.strip() for l in fc.added if len(l.strip()) > 3)
    hits = []
    for fc in parse_diff(patch_diff):
        recent = added_by_commit.get(fc.path, set())
        hits += [l.strip() for l in fc.removed if l.strip() in recent]
    return hits


# Public name, used by the agent's file tools.
path_problem = _path_problem
