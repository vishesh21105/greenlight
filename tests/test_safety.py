import subprocess
from pathlib import Path

import pytest

from app.patches import Edit, apply_diff, build_diff_from_edits, parse_diff, validate_diff
from app.policy import Policy, PolicyError, parse_policy, path_matches
from app.scrub import scrub


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "tinyshop").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "tinyshop" / "checkout.py").write_text(
        "from tinyshop.pricing import compute_total\n\n\ndef checkout(cart):\n    return calc_total(cart)\n")
    (tmp_path / "tests" / "test_checkout.py").write_text(
        "from tinyshop.checkout import checkout\n\n\ndef test_total():\n    assert checkout([]) == 0\n")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    return tmp_path


# ---------------------------------------------------------------- policy

def test_policy_defaults_and_parse():
    p = parse_policy("max_files_changed: 2\nallowed_paths: ['tinyshop/**']\n")
    assert p.max_files_changed == 2 and p.mode == "pull_request"


@pytest.mark.parametrize("text", [
    "unknown_key: 1",
    "mode: yolo",
    "test_command: ['bash', '-c', 'rm -rf /']",
    "test_command: ['python', '-m', 'pytest', ';', 'curl', 'x']",
])
def test_policy_rejects_dangerous_or_bad_values(text):
    with pytest.raises(PolicyError):
        parse_policy(text)


def test_glob_matching():
    assert path_matches("src/a/b.py", ["src/**"])
    assert path_matches("x/.env", ["**/.env"])
    assert path_matches(".env", ["**/.env"])
    assert not path_matches("srcx/a.py", ["src/**"])
    assert path_matches(".github/workflows/ci.yml", [".github/**"])


# ---------------------------------------------------------------- edits -> diff

def test_edit_produces_valid_diff_that_applies(repo):
    diff, errors = build_diff_from_edits(repo, [Edit("tinyshop/checkout.py", "calc_total(cart)", "compute_total(cart)")])
    assert not errors
    result = validate_diff(diff, Policy(), repo)
    assert result.valid, result.errors
    assert result.files == ["tinyshop/checkout.py"] and result.lines_changed == 2
    ok, msg = apply_diff(repo, diff)
    assert ok, msg
    assert "compute_total(cart)" in (repo / "tinyshop" / "checkout.py").read_text()


def test_edit_old_text_must_match_exactly_once(repo):
    _, errors = build_diff_from_edits(repo, [Edit("tinyshop/checkout.py", "does not exist", "x")])
    assert "not found" in errors[0]
    _, errors = build_diff_from_edits(repo, [Edit("tinyshop/checkout.py", "\n", "x")])
    assert "appears" in errors[0]


def test_edit_rejects_path_escape(repo):
    _, errors = build_diff_from_edits(repo, [Edit("../etc/passwd", "root", "x")])
    assert "escapes" in errors[0]


def test_edit_rejects_symlink(repo):
    (repo / "link.py").symlink_to("/etc/hostname")
    _, errors = build_diff_from_edits(repo, [Edit("link.py", "a", "b")])
    assert "symlink" in errors[0]


# ---------------------------------------------------------------- validator

def _diff(path: str, removed: list[str], added: list[str]) -> str:
    body = "".join(f"-{l}\n" for l in removed) + "".join(f"+{l}\n" for l in added)
    return f"--- a/{path}\n+++ b/{path}\n@@ -1,{len(removed)} +1,{len(added)} @@\n{body}"


def test_blocks_workflow_and_policy_files(repo):
    for path in [".github/workflows/ci.yml", ".greenlight.yml", "config/.env"]:
        r = validate_diff(_diff(path, ["a"], ["b"]), Policy(), repo)
        assert not r.valid and "blocked" in r.errors[0]


def test_blocks_paths_outside_allowed(repo):
    r = validate_diff(_diff("docs/readme.md", ["a"], ["b"]), Policy(allowed_paths=["tinyshop/**"]), repo)
    assert not r.valid and "allowed_paths" in r.errors[0]


def test_blocks_skipping_tests(repo):
    r = validate_diff(_diff("tests/test_checkout.py", ["def test_total():"],
                            ["@pytest.mark.skip", "def test_total():"]), Policy(), repo)
    assert not r.valid and any("skip" in e for e in r.errors)


def test_blocks_removing_assertions(repo):
    r = validate_diff(_diff("tests/test_checkout.py", ["    assert checkout([]) == 0"], ["    pass"]), Policy(), repo)
    assert not r.valid and any("assertions" in e for e in r.errors)


def test_allows_changing_an_assertion_but_flags_it(repo):
    r = validate_diff(_diff("tests/test_checkout.py", ["    assert checkout([]) == 0"],
                            ["    assert checkout([]) == 0.0"]), Policy(), repo)
    assert r.valid and r.touches_tests


def test_test_edits_can_be_forbidden(repo):
    r = validate_diff(_diff("tests/test_checkout.py", ["    assert checkout([]) == 0"],
                            ["    assert checkout([]) == 0.0"]), Policy(allow_test_edits="no"), repo)
    assert not r.valid


def test_size_limits(repo):
    big = _diff("tinyshop/checkout.py", [f"a{i}" for i in range(30)], [f"b{i}" for i in range(30)])
    r = validate_diff(big, Policy(max_lines_changed=40), repo)
    assert not r.valid and "lines" in r.errors[-1]


def test_binary_and_delete_rejected(repo):
    binary = "diff --git a/x.png b/x.png\nBinary files a/x.png and b/x.png differ\n"
    assert not validate_diff(binary, Policy(), repo).valid
    delete = "diff --git a/tinyshop/checkout.py b/tinyshop/checkout.py\ndeleted file mode 100644\n--- a/tinyshop/checkout.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-x\n"
    r = validate_diff(delete, Policy(), repo)
    assert not r.valid and any("deleting" in e for e in r.errors)


def test_parse_diff_counts_lines():
    fc = parse_diff(_diff("a.py", ["x", "y"], ["z"]))[0]
    assert fc.path == "a.py" and len(fc.removed) == 2 and len(fc.added) == 1


# ---------------------------------------------------------------- scrubbing

def test_scrub_hides_secrets():
    text = ("token ghp_" + "a" * 36 + " key AKIA" + "B" * 16 + "\nOPENAI_API_KEY=sk-" + "x" * 30 +
            "\npassword: hunter2hunter2\n-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----")
    clean, count = scrub(text)
    assert "ghp_" not in clean and "AKIA" not in clean and "hunter2" not in clean and "abc" not in clean
    assert "sk-xxx" not in clean
    assert count >= 5


def test_scrub_leaves_normal_text():
    text = "NameError: name 'calc_total' is not defined"
    assert scrub(text) == (text, 0)
