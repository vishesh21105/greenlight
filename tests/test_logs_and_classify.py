from pathlib import Path

from app.classify import classify, classify_by_rules
from app.logs import distill, safe_test_targets

FIX = Path(__file__).parent / "fixtures"


def load(name: str) -> str:
    return (FIX / name).read_text()


def test_distill_keeps_error_and_drops_noise():
    log = distill(load("log_code_bug.txt"))
    assert "NameError: name 'calc_total' is not defined" in log.text
    assert "tinyshop/checkout.py:7" in log.text
    assert "Collecting" not in log.text           # pip noise removed
    assert "PASSED" not in log.text               # passing tests removed
    assert "\x1b[" not in log.text                # colour codes removed
    assert "2026-10-28T" not in log.text          # timestamps removed
    assert log.failing_tests == ["tests/test_checkout.py::test_checkout_total"]
    assert "NameError" in log.error_types
    assert log.kept_lines <= 150
    assert log.reduction < 0.10                    # keeps under 10% of this log


def test_distill_caps_output():
    noisy = "\n".join(f"ValueError: problem {i}" for i in range(1000))
    log = distill(noisy, max_lines=50)
    assert log.kept_lines <= 50
    assert "problem 999" in log.text               # the end of the log is preferred


def test_distill_without_known_errors_keeps_tail():
    log = distill("\n".join(f"line {i}" for i in range(500)))
    assert "line 499" in log.text and "line 10\n" not in log.text


def test_test_ids_are_sanitised():
    ids = ["tests/test_a.py::test_x", "tests/test_b.py", "--rootdir=/etc", "tests/../../x.py", "tests/t.py::t; rm -rf /"]
    assert safe_test_targets(ids) == ["tests/test_a.py::test_x", "tests/test_b.py"]


def test_classify_code_bug():
    c = classify_by_rules(distill(load("log_code_bug.txt")))
    assert c.category == "code_bug" and c.confidence >= 0.7


def test_classify_dependency():
    c = classify_by_rules(distill(load("log_dependency.txt")))
    assert c.category == "dependency"


def test_classify_infra():
    c = classify_by_rules(distill(load("log_infra.txt")))
    assert c.category == "infra"


def test_classify_import_inside_own_package_is_code_bug():
    log = distill(load("log_import.txt"))
    assert classify_by_rules(log, repo_modules={"tinyshop", "tests"}).category == "code_bug"
    assert classify_by_rules(log, repo_modules={"other"}).category == "dependency"


def test_assertion_is_ambiguous_and_asks_the_model():
    log = distill(load("log_assertion.txt"))
    assert classify_by_rules(log).confidence < 0.7

    class FakeLLM:
        def chat(self, messages, **kwargs):
            class R:
                text = '{"category": "broken_test", "confidence": 0.8, "reason": "discount rule changed on purpose"}'
            return R()

    c = classify(log, llm=FakeLLM())
    assert c.category == "broken_test" and c.source == "llm"


def test_bad_model_answer_falls_back_to_rules():
    log = distill(load("log_assertion.txt"))

    class BadLLM:
        def chat(self, messages, **kwargs):
            class R:
                text = "I think it's probably fine!"
            return R()

    assert classify(log, llm=BadLLM()).source == "rules"
