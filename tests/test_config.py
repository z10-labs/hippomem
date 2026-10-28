from __future__ import annotations

import pytest

from hippomem import actors, config, db, systems


def test_missing_file_gives_defaults(tmp_path):
    c = config.load(tmp_path / "nope.toml")
    assert (c.port, c.policy.self_approval, c.max_per_session, c.stale_after_days) == (config.DEFAULT_PORT, "never", 2, 30)


def test_template_parses_to_defaults():
    import tomllib

    assert config.parse(tomllib.loads(config.TEMPLATE)) == config.Config()


def test_invalid_policy_values_are_rejected():
    with pytest.raises(ValueError, match="self_approval"):
        config.parse({"acceptance": {"self_approval": "always"}})
    with pytest.raises(ValueError, match="evidence_only"):
        config.parse({"acceptance": {"evidence_only": "maybe"}})


def test_apply_creates_systems_owners_and_mappings(tmp_path):
    conn = db.connect(":memory:")
    actors.add(conn, "@alex", "engineer")
    raw = {
        "systems": [
            {"id": "ui-kit", "name": "UI kit", "repos": ["ui-kit"]},
            {"id": "billing", "name": "Billing", "owners": ["@alex"], "depends_on": ["ui-kit"], "repos": ["api:src/billing/**", "worker"]},
        ]
    }
    config.apply(conn, config.parse(raw))
    config.apply(conn, config.parse(raw))  # applying twice is harmless
    assert systems.owners_of(conn, "billing") == ["@alex"]
    assert systems.systems_for(conn, "worker") == ["billing", "ui-kit"]
    assert systems.systems_for(conn, "api", "src/billing/x.py") == ["billing", "ui-kit"]


def test_apply_refuses_owner_who_is_not_an_actor():
    conn = db.connect(":memory:")
    with pytest.raises(ValueError, match="hippomem actor add @ghost"):
        config.apply(conn, config.parse({"systems": [{"id": "x", "owners": ["@ghost"]}]}))


def test_tokens_identify_actors_and_rotate():
    conn = db.connect(":memory:")
    token = actors.add(conn, "@alex", "engineer")
    assert actors.from_token(conn, token, "claude-code").agent == "claude-code"
    assert actors.from_token(conn, "hm_wrong") is None
    assert actors.from_token(conn, "not-a-token") is None
    new = actors.rotate(conn, "@alex")
    assert actors.from_token(conn, token) is None and actors.from_token(conn, new).id == "@alex"
    with pytest.raises(ValueError, match="start with '@'"):
        actors.add(conn, "alex", "engineer")
    with pytest.raises(ValueError, match="kind"):
        actors.add(conn, "@x", "robot")
