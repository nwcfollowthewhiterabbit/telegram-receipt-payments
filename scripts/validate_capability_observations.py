#!/usr/bin/env python3
"""Validate the local capability outbox without network access."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

from _capability_contract import capability_repository_errors


ROOT = Path(__file__).resolve().parents[1]


def load_optional_project_config() -> Optional[dict]:
    path = ROOT / "project.toml"
    if not path.is_file():
        return None
    try:
        import tomllib
    except ModuleNotFoundError:
        import _toml_compat as tomllib
    return tomllib.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    try:
        project_config = load_optional_project_config()
    except (OSError, UnicodeError, ValueError) as exc:
        print(f"Capability observations cannot be validated: {exc}")
        return 1

    errors = capability_repository_errors(ROOT, project_config)
    if errors:
        print("Capability observation validation failed:")
        for error in errors:
            print(f"  - {error}")
        return 1
    print("Capability observation validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
