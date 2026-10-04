"""Unit tests: browser snapshot/forms, policy gate, vault, file sandbox, retries."""
import pytest
from starlette.testclient import TestClient

from worker.human import ScriptedHuman
from worker.policy import Policy, PolicyBlocked
from worker.tools.browser import Browser, BrowserError, SubmitRequest
from worker.tools.toolbox import WORKER_TOOLS, ToolBox
from worker.trace import Trace
from worker.vault import Vault


def toolbox(env, human=None, names=WORKER_TOOLS, read_only=False):
    return ToolBox(names=names, browser_client=env.client_factory(), workspace=env.workspace,
                   vault=env.vault(), policy=env.policy(read_only=read_only), human=human or ScriptedHuman(),
                   trace=Trace(env.runs_dir), backoff=0)


def test_snapshot_exposes_refs_and_hides_hidden_fields(env, world):
    b = Browser(TestClient(world.app, base_url="http://sim.test"))
    b.navigate("http://sim.test/erp/")
    b.fill("e1", "ap.clerk")
    b.fill("e2", env.vault().data["ledgerly_erp"]["password"])
    snap = b.click("e3")
    assert "Accounts payable" in snap
    snap = b.navigate("/erp/payables/new")
    assert "csrf_token" not in snap               # hidden inputs are not shown to the model
    assert 'select "Vendor"' in snap and "V-100=Acme Corp" in snap
    with pytest.raises(BrowserError, match="Available options"):
        b.fill("e4", "Initech")                    # unknown select option -> helpful error


def test_stale_ref_gives_actionable_error(env):
    tb = toolbox(env)
    tb.execute("browser_navigate", {"url": "http://sim.test/mail/"})
    res = tb.execute("browser_click", {"ref": "e999"})
    assert res.is_error and "fresh snapshot" in res.content


def test_navigation_outside_allowed_hosts_is_blocked(env):
    res = toolbox(env).execute("browser_navigate", {"url": "https://evil.example/phish"})
    assert res.is_error and "blocked by policy" in res.content


def test_policy_rules():
    p = Policy(allowed_hosts={"x"})
    req = lambda url, fields, label="Save": SubmitRequest("POST", url, fields, fields, label, "")
    assert not p.assess_submit(req("http://x/erp/payables", {"amount": "500"})).needs_approval
    assert p.assess_submit(req("http://x/erp/payables", {"amount": "12480.00"})).needs_approval
    assert p.assess_submit(req("http://x/other/form", {"q": "1"})).needs_approval   # unknown system
    with pytest.raises(PolicyBlocked):
        p.assess_submit(req("http://x/erp/payables/PAY-1/pay", {}, "Release payment now"))
    with pytest.raises(PolicyBlocked):
        Policy(allowed_hosts={"x"}, read_only=True).assess_submit(req("http://x/erp/payables", {"amount": "1"}))


def test_vault_placeholders(env):
    v = Vault(env.workspace / "vault.json")
    shown = v.describe("ledgerly_erp")
    assert shown["password"] == "{{secret:ledgerly_erp.password}}"
    assert v.resolve("{{secret:ledgerly_erp.password}}") == (v.data["ledgerly_erp"]["password"], True)


def test_file_sandbox(env):
    tb = toolbox(env)
    assert tb.execute("read_file", {"path": "vault.json"}).is_error
    assert tb.execute("read_file", {"path": "../../etc/passwd"}).is_error
    assert tb.execute("write_file", {"path": "notes.txt", "content": "x"}).is_error
    assert not tb.execute("write_file", {"path": "output/notes.txt", "content": "x"}).is_error


def test_transient_errors_retry_then_give_up(env, world):
    world.reset_world(portal_503=10)
    tb = toolbox(env)
    tb.execute("browser_navigate", {"url": "http://sim.test/acme/"})
    tb.execute("browser_fill", {"fields": [{"ref": "e1", "value": "ap@ourco.example"},
                                           {"ref": "e2", "value": "{{secret:acme_portal.password}}"}]})
    tb.execute("browser_click", {"ref": "e3"})
    res = tb.execute("browser_navigate", {"url": "/acme/invoices"})
    assert res.is_error and "after 3 retries" in res.content
    assert world.WORLD.faults.portal_503 == 6   # 1 try + 3 retries consumed
