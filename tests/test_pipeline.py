from pathlib import Path

from sqlmodel import select

from app import config
from app.db import session_scope
from app.demo import simulate_ci_log
from app.github_client import LocalCodeHost
from app.llm import ScriptedProvider
from app.models import Incident, IncidentEvent, LLMCall, Patch, Repository, SandboxRun
from app.pipeline import Services, load_trusted_policy, process_incident
from app.sandbox import LocalSandbox
from tests.helpers import fix_script, git, make_repo, tool


def setup_incident(tmp_path: Path, repo: Path, green: str, broken: str, log: str | None = None) -> str:
    with session_scope() as s:
        r = Repository(full_name="demo/shop", clone_url=str(repo))
        s.add(r)
        s.flush()
        inc = Incident(repository_id=r.id, branch="main", failing_sha=broken, last_green_sha=green)
        s.add(inc)
    ws = Path(config.settings.workspace_dir) / inc.id
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "ci_log.txt").write_text(log or simulate_ci_log(repo, broken, tmp_path))
    return inc.id


def services(llm) -> Services:
    return Services(code_host=LocalCodeHost(), sandbox=LocalSandbox(install_deps=False), llm=llm)


def state_of(incident_id: str) -> Incident:
    with session_scope() as s:
        return s.get(Incident, incident_id)


def test_full_loop_opens_verified_fix(tmp_path):
    repo, green, broken = make_repo(tmp_path / "src")
    iid = setup_incident(tmp_path, repo, green, broken)
    process_incident(iid, services(ScriptedProvider(fix_script())))

    inc = state_of(iid)
    assert inc.state == "fixed", inc.summary
    assert inc.category == "code_bug"
    assert inc.pr_url and "greenlight/fix-" in inc.pr_url
    assert inc.trimmed_log_lines < inc.raw_log_lines / 5

    # The fix branch exists in the source repo and its tests pass.
    branch = inc.pr_url.split("/tree/")[1]
    git(repo, "checkout", "-q", branch)
    assert "calc_total(cart)" in (repo / "tinyshop" / "checkout.py").read_text()

    with session_scope() as s:
        patch = s.exec(select(Patch).where(Patch.incident_id == iid)).one()
        purposes = [r.purpose for r in s.exec(select(SandboxRun).where(SandboxRun.incident_id == iid))]
        calls = s.exec(select(LLMCall).where(LLMCall.incident_id == iid)).all()
    assert patch.verified and patch.files_changed == 1
    assert purposes.count("reproduce") == 1 and "verify_failing" in purposes and "verify_full" in purposes
    assert len(calls) == 4
    pr = (Path(config.settings.workspace_dir) / iid / "pull_request.md").read_text()
    assert "Verification (run by GreenLight's sandbox" in pr and "passed" in pr


def test_without_a_model_posts_rules_diagnosis(tmp_path):
    repo, green, broken = make_repo(tmp_path / "src")
    iid = setup_incident(tmp_path, repo, green, broken)
    process_incident(iid, services(None))
    assert state_of(iid).state == "diagnosed_no_fix"
    comment = (Path(config.settings.workspace_dir) / iid / "comment.md").read_text()
    assert "reproduced in a sandbox" in comment


def test_wrong_fixes_never_become_a_pr(tmp_path):
    repo, green, broken = make_repo(tmp_path / "src")
    iid = setup_incident(tmp_path, repo, green, broken)

    def wrong(messages, tools):
        return tool("propose_patch", edits=[{"path": "tinyshop/pricing.py", "old_text": "GST_RATE = 0.18",
                                             "new_text": "GST_RATE = 0.12"}],
                    rationale="Maybe the GST rate is wrong (it is not, this is a deliberately bad fix).")

    process_incident(iid, services(ScriptedProvider(wrong)))
    inc = state_of(iid)
    assert inc.state == "gave_up" and inc.pr_url is None
    with session_scope() as s:
        patches = s.exec(select(Patch).where(Patch.incident_id == iid)).all()
    assert len(patches) == 3 and not any(p.verified for p in patches)     # first try + 2 repairs


def test_failure_that_does_not_reproduce(tmp_path):
    repo, green, _ = make_repo(tmp_path / "src")
    # CI says this commit failed, but the code at green is fine: not reproducible.
    log = "FAILED tests/test_cart.py::test_remove_item - AssertionError: assert 1 == 0\n"
    iid = setup_incident(tmp_path, repo, green, green, log)
    process_incident(iid, services(ScriptedProvider(fix_script())))
    assert state_of(iid).state == "not_reproduced"


def test_infra_failure_stops_early(tmp_path):
    repo, green, broken = make_repo(tmp_path / "src")
    iid = setup_incident(tmp_path, repo, green, broken, "OSError: [Errno 28] No space left on device\n")
    process_incident(iid, services(ScriptedProvider(fix_script())))
    assert state_of(iid).state == "infra"


def test_internal_errors_still_end_in_a_final_state(tmp_path):
    repo, green, broken = make_repo(tmp_path / "src")
    iid = setup_incident(tmp_path, repo, green, broken)

    class ExplodingHost(LocalCodeHost):
        def failing_log(self, repo, incident):
            raise RuntimeError("GitHub is down")

    process_incident(iid, Services(ExplodingHost(), LocalSandbox(install_deps=False), None))
    assert state_of(iid).state == "error"
    with session_scope() as s:
        msgs = [e.message for e in s.exec(select(IncidentEvent).where(IncidentEvent.incident_id == iid))]
    assert any("GitHub is down" in m for m in msgs)


def test_policy_comes_from_the_green_commit_not_the_failing_one(tmp_path):
    repo, green, _ = make_repo(tmp_path / "src")
    (repo / ".greenlight.yml").write_text("max_files_changed: 99\nallowed_paths: ['**']\n")
    git(repo, "commit", "-qam", "Loosen the rules (sneaky)")
    iid = setup_incident(tmp_path, repo, green, git(repo, "rev-parse", "HEAD"), "log")
    policy = load_trusted_policy(repo, green, iid)
    assert policy.max_files_changed == 3
