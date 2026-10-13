"""Run every quality gate: formatting, linting, type checking and tests.

Usage:
    python scripts/check.py          # verify only
    python scripts/check.py --fix    # apply ruff formatting and safe lint fixes first
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _run(label: str, args: list[str]) -> bool:
    print(f"\n==> {label}: {' '.join(args)}", flush=True)
    return subprocess.run([sys.executable, "-m", *args], cwd=ROOT, check=False).returncode == 0


def main(argv: list[str]) -> int:
    fix = "--fix" in argv
    steps: list[tuple[str, list[str]]] = []
    if fix:
        steps += [
            ("format", ["ruff", "format", "."]),
            ("lint-fix", ["ruff", "check", "--fix", "."]),
        ]
    steps += [
        ("format-check", ["ruff", "format", "--check", "."]),
        ("lint", ["ruff", "check", "."]),
        ("types", ["mypy"]),
        ("tests", ["pytest"]),
    ]

    failed = [label for label, args in steps if not _run(label, args)]
    print()
    if failed:
        print(f"FAILED: {', '.join(failed)}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
