from __future__ import annotations

import io
import subprocess
import sys

import pytest

from agentbodycam import __version__
from agentbodycam.cli import main


def test_no_args_prints_help_and_succeeds() -> None:
    out = io.StringIO()
    assert main([], out=out) == 0
    text = out.getvalue()
    assert "usage: agentbodycam" in text
    assert "doctor" in text


def test_help_flag_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    assert "Your AI coding agent now has a bodycam." in capsys.readouterr().out


def test_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"agentbodycam {__version__}"


def test_unknown_command_fails(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["record"])
    assert exc.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_doctor_command_reports_environment() -> None:
    out = io.StringIO()
    assert main(["doctor"], out=out) == 0
    text = out.getvalue()
    for label in ("Python", "OS", "Current dir", "Git"):
        assert label in text


def test_python_dash_m_entry_point() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "agentbodycam", "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0
    assert completed.stdout.strip() == f"agentbodycam {__version__}"
