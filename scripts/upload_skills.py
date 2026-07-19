#!/usr/bin/env python3
"""Verify that all local extraction skills are well-formed.

Checks each skill directory under skills/ for:
  - SKILL.md exists and is non-empty
  - schema.json exists and is valid JSON
  - validate.py exists and imports successfully (validate function callable)

Usage:
    python3 scripts/upload_skills.py          # Verify all skills
    python3 scripts/upload_skills.py --list    # List discovered skills
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR = PROJECT_ROOT / "skills"

REQUIRED_FILES = {"SKILL.md", "schema.json", "validate.py"}


def discover_skills() -> list[str]:
    """Find all skill directories under skills/."""
    if not SKILLS_DIR.is_dir():
        return []
    return sorted(
        entry.name
        for entry in SKILLS_DIR.iterdir()
        if entry.is_dir() and (entry / "SKILL.md").exists()
    )


def verify_skill(name: str) -> list[str]:
    """Verify a single skill. Returns a list of error messages (empty = OK)."""
    errors: list[str] = []
    skill_dir = SKILLS_DIR / name

    # Check required files
    for required in REQUIRED_FILES:
        target = skill_dir / required
        if not target.exists():
            errors.append(f"  MISSING: {required}")
        elif target.is_file() and target.stat().st_size == 0:
            errors.append(f"  EMPTY: {required}")

    # Validate schema.json
    schema_path = skill_dir / "schema.json"
    if schema_path.exists() and schema_path.stat().st_size > 0:
        try:
            data = json.loads(schema_path.read_text())
            if not isinstance(data, dict):
                errors.append("  schema.json: root is not a JSON object")
        except json.JSONDecodeError as exc:
            errors.append(f"  schema.json: invalid JSON — {exc}")

    # Validate validate.py imports and exposes validate()
    validate_path = skill_dir / "validate.py"
    if validate_path.exists() and validate_path.stat().st_size > 0:
        try:
            spec = importlib.util.spec_from_file_location(
                f"skill_validate_{name}", validate_path
            )
            if spec and spec.loader:
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                if not hasattr(mod, "validate") or not callable(mod.validate):
                    errors.append("  validate.py: no callable validate() found")
        except Exception as exc:
            errors.append(f"  validate.py: import error — {exc}")

    return errors


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Verify local extraction skills are well-formed"
    )
    parser.add_argument(
        "--list", action="store_true", help="List discovered skills and exit"
    )
    args = parser.parse_args()

    skills = discover_skills()
    if not skills:
        print("No skills found under skills/")
        sys.exit(1)

    if args.list:
        print(f"Discovered {len(skills)} skill(s):")
        for name in skills:
            print(f"  - {name}")
        sys.exit(0)

    # Verify mode
    print(f"Verifying {len(skills)} skill(s)...\n")
    all_ok = True
    for name in skills:
        errors = verify_skill(name)
        if errors:
            print(f"FAIL: {name}")
            for err in errors:
                print(err)
            all_ok = False
        else:
            print(f"OK:   {name}")

    print()
    if all_ok:
        print(f"All {len(skills)} skill(s) verified.")
    else:
        print("Some skills have issues. Fix the above before running.")
        sys.exit(1)


if __name__ == "__main__":
    main()
