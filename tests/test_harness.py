"""End-to-end tests of the harness, driven by the scripted brain against SimWorld."""
import json
from functools import partial

from worker.agent import TaskWorker
from worker.brain import ScriptedBrain
from worker.human import ScriptedHuman
from worker.scripts import invoice_verifier, invoice_worker

TASK = ("Find the latest invoice from Acme Corp, extract the amount and due date, enter it into our internal "
        "system, and tell me once it is done.")


def make_worker(env, human, **script_kw):
    return TaskWorker(env, ScriptedBrain(partial(invoice_worker, **script_kw)), human,
                      verifier_brain=ScriptedBrain(invoice_verifier))


def events(result, type_):
    lines = (result.run_dir / "trace.jsonl").read_text(encoding="utf-8").splitlines()
    return [e for e in map(json.loads, lines) if e["type"] == type_]


def test_happy_path_with_faults_approval_and_verification(env, world):
    human = ScriptedHuman(approve=True)
    result = make_worker(env, human).run(TASK)

    assert result.status == "completed" and result.verified is True
    assert result.results["invoice_number"] == "INV-2041"          # latest by date, not list order
    assert result.results["due_date"] == "2026-10-31"
    rec = [p for p in world.WORLD.payables if p["invoice_number"] == "INV-2041"]
    assert len(rec) == 1 and rec[0]["amount"] == 12480.0 and rec[0]["status"] == "Pending approval"

    # Transient 503 on the portal was retried automatically.
    assert any(e["tool"] == "browser_navigate" for e in events(result, "retry"))
    # Amount >= 10k required exactly one human approval; the resubmission after the
    # session expiry reused it instead of asking again.
    assert [r.kind for r in human.requests] == ["approval"]
    assert any(e.get("reused") for e in events(result, "approval"))
    # Evidence files exist and secrets never reached the trace or report.
    assert (result.run_dir / "report.html").exists()
    blob = (result.run_dir / "trace.jsonl").read_text(encoding="utf-8") + (result.run_dir / "report.html").read_text(encoding="utf-8")
    assert all(s not in blob for s in env.vault().secret_values())
    assert list((result.run_dir / "evidence").glob("E1-*.html"))
    # Lessons persist for future runs.
    assert (env.workspace / "memory" / "lessons.json").exists()


def test_user_declines_approval_nothing_is_written(env, world):
    result = make_worker(env, ScriptedHuman(approve=False)).run(TASK)
    assert result.status == "blocked"
    assert not [p for p in world.WORLD.payables if p["invoice_number"] == "INV-2041"]


def test_validation_error_is_detected_and_corrected(env, world):
    result = make_worker(env, ScriptedHuman(approve=True), date_mistake=True).run(TASK)
    assert result.status == "completed" and result.verified
    obs = [e for e in events(result, "observation") if "invalid: use the format YYYY-MM-DD" in e["content"]]
    assert obs, "the ERP should have rejected the first date format"


def test_verifier_catches_wrong_value(env, world):
    result = make_worker(env, ScriptedHuman(approve=True), wrong_amount=True).run(TASK)
    assert result.verified is False and result.status == "failed"
    failed = [c for c in result.verification["checks"] if not c["ok"]]
    assert any("amount" in c["claim"].lower() for c in failed)


def test_ambiguous_vendor_triggers_clarification(env, world):
    human = ScriptedHuman(approve=True, answers=["Acme Corp"])
    result = make_worker(env, human).run("Enter the latest Acme invoice into Ledgerly.")
    assert human.requests[0].kind == "clarification"
    assert result.status == "completed"


def test_duplicate_is_not_entered_twice(env, world):
    make_worker(env, ScriptedHuman(approve=True)).run(TASK)
    second = make_worker(env, ScriptedHuman(approve=True)).run(TASK)
    assert second.status == "blocked"
    assert len([p for p in world.WORLD.payables if p["invoice_number"] == "INV-2041"]) == 1
