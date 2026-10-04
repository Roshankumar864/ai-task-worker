"""SimWorld login credentials, generated per machine and never committed.

On first use a random set is written to workspace/vault.json (git-ignored). SimWorld uses it to
create its accounts, and the worker's vault reads the same file, so the two always match.
"""
from __future__ import annotations

import json
import os
import secrets
from pathlib import Path

DEFAULT_PATH = Path(__file__).resolve().parents[1] / "workspace" / "vault.json"


def vault_path() -> Path:
    return Path(os.environ.get("SIMWORLD_VAULT", DEFAULT_PATH))


def load_or_create(path: Path | None = None) -> dict[str, dict[str, str]]:
    path = path or vault_path()
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    data = {
        "acme_portal": {"login_path": "/acme/", "username": "ap@ourco.example",
                        "password": secrets.token_urlsafe(12)},
        "ledgerly_erp": {"login_path": "/erp/", "username": "ap.clerk", "password": secrets.token_urlsafe(12)},
        "ledgerly_api": {"base_path": "/erp/api/", "header": "X-API-Key", "api_key": secrets.token_urlsafe(16)},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return data
