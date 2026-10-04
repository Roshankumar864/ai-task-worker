import shutil
import sys
from pathlib import Path

import pytest
from starlette.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from simworld import app as sim  # noqa: E402
from worker.agent import Environment  # noqa: E402

SIM_URL = "http://sim.test"


@pytest.fixture
def env(tmp_path):
    sim.reset_world()
    ws = tmp_path / "workspace"
    shutil.copytree(ROOT / "workspace", ws, ignore=shutil.ignore_patterns("output", "memory"))
    return Environment(sim_url=SIM_URL, workspace=ws, runs_dir=tmp_path / "runs",
                       client_factory=lambda: TestClient(sim.app, base_url=SIM_URL), retry_backoff=0)


@pytest.fixture
def world():
    return sim
