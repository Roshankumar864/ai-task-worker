"""Credential vault. The model never sees secret values.

`get_credentials` hands the model placeholders like {{secret:ledgerly_erp.password}}. The browser
and HTTP tools substitute the real value at the last moment, and the trace redacts any secret
that might appear in tool output.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

PLACEHOLDER_RE = re.compile(r"\{\{secret:([\w-]+)\.([\w-]+)\}\}")
SECRET_KEYS = {"password", "api_key", "token"}


class Vault:
    def __init__(self, path: Path):
        self.data: dict[str, dict[str, str]] = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    def systems(self) -> list[str]:
        return sorted(self.data)

    def describe(self, system: str) -> dict[str, str]:
        entry = self.data.get(system)
        if entry is None:
            raise KeyError(f"No credentials stored for {system!r}. Known systems: {self.systems()}")
        return {k: (f"{{{{secret:{system}.{k}}}}}" if k in SECRET_KEYS else v) for k, v in entry.items()}

    def resolve(self, value: str) -> tuple[str, bool]:
        """Replace placeholders in `value`. Returns (resolved, contained_secret)."""
        found = False

        def sub(m: re.Match) -> str:
            nonlocal found
            found = True
            try:
                return self.data[m.group(1)][m.group(2)]
            except KeyError:
                raise KeyError(f"Unknown secret placeholder {m.group(0)}") from None

        return PLACEHOLDER_RE.sub(sub, value), found

    def secret_values(self) -> list[str]:
        return [v for entry in self.data.values() for k, v in entry.items() if k in SECRET_KEYS and v]
