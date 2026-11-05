from __future__ import annotations

import pytest

from hippomem import __version__, actors, config, db, systems
from hippomem.cli import main


def test_version_flag_prints_package_version(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_no_command_prints_help(capsys):
    assert main([]) == 0
    assert "usage: hippomem" in capsys.readouterr().out


def test_init_creates_memory_config_and_admin_token(tmp_path, capsys):
    home = tmp_path / "hm"
    assert main(["--home", str(home), "init", "--admin", "@owner"]) == 0
    assert config.config_path(home).exists()
    token = (home / "token").read_text()
    assert oct((home / "token").stat().st_mode)[-3:] == "600"
    conn = db.connect(config.db_path(home))
    assert actors.from_token(conn, token).kind == "admin"


def test_actor_and_map_commands(tmp_path, capsys):
    home = tmp_path / "hm"
    main(["--home", str(home), "init"])
    assert main(["--home", str(home), "actor", "add", "@alex"]) == 0
    token = capsys.readouterr().out.strip().splitlines()[-1]
    assert token.startswith("hm_")
    config.config_path(home).write_text('[[systems]]\nid = "billing"\nowners = ["@alex"]\n')
    assert main(["--home", str(home), "config", "apply"]) == 0
    assert main(["--home", str(home), "map", "add", "api", "src/billing/**", "billing"]) == 0
    with pytest.raises(SystemExit, match="Unknown system"):
        main(["--home", str(home), "map", "add", "api", "**", "nope"])
    capsys.readouterr()
    main(["--home", str(home), "map", "list"])
    assert "api:src/billing/**\tbilling\tconfirmed" in capsys.readouterr().out
    assert systems.systems_for(db.connect(config.db_path(home)), "api", "src/billing/a.py") == ["billing"]


def test_client_command_without_token_explains_itself(tmp_path, monkeypatch):
    monkeypatch.delenv("HIPPOMEM_TOKEN", raising=False)
    with pytest.raises(SystemExit, match="No token"):
        main(["--home", str(tmp_path), "whoami"])


def test_client_command_without_server_explains_itself(tmp_path, monkeypatch):
    monkeypatch.setenv("HIPPOMEM_TOKEN", "hm_x")
    monkeypatch.setenv("HIPPOMEM_URL", "http://127.0.0.1:9")
    with pytest.raises(SystemExit, match="Is `hippomem serve` running"):
        main(["--home", str(tmp_path), "query", "anything"])
