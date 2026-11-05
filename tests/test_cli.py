from __future__ import annotations

import pytest

from hippomem import __version__
from hippomem.cli import main


def test_version_flag_prints_package_version(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_no_command_prints_help(capsys):
    assert main([]) == 0
    assert "usage: hippomem" in capsys.readouterr().out
