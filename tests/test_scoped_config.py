import pytest
from lightfall_settings import FACILITY, ScopeId

from lightfall_logbook.scoped_config import (
    ScopedConfig,
    UnknownScope,
    load_scoped_config,
    parse_scoped_config,
)

EXAMPLE = """
[facility]
label = "ALS"

[[beamline]]
name = "7.0.1"
branches = ["7.0.1.1", "7.0.1.2"]

[[beamline]]
name = "8.3.2"

[facility_grants]
"/beamlines/_all/admin" = "admin"
"BCG_staff" = "maintainer"

[seed."voice.wake_word"]
type = "str"
scopes = ["branch", "user"]
operator_writable = true
"""

BL = ScopeId("beamline", "7.0.1")
BR = ScopeId("branch", "7.0.1.1")


def test_parse_example():
    cfg = parse_scoped_config(EXAMPLE)
    assert cfg.tree.label == "ALS"
    assert cfg.tree.beamlines == ("7.0.1", "8.3.2")
    assert cfg.tree.branch_parent == {"7.0.1.1": "7.0.1", "7.0.1.2": "7.0.1"}
    assert cfg.facility_grants == {"/beamlines/_all/admin": "admin", "/BCG_staff": "maintainer"}
    (seed,) = cfg.seed
    assert seed.key == "voice.wake_word" and seed.operator_writable
    assert seed.scopes == frozenset({"branch", "user"})


def test_chain_for():
    tree = parse_scoped_config(EXAMPLE).tree
    assert list(tree.chain_for(FACILITY)) == [FACILITY]
    assert list(tree.chain_for(BL)) == [FACILITY, BL]
    assert list(tree.chain_for(BR)) == [FACILITY, BL, BR]
    alice = ScopeId("user", "alice")
    assert list(tree.chain_for(alice)) == [FACILITY, alice]


@pytest.mark.parametrize("scope", ["beamline:9.9.9", "branch:7.0.1.9", "beamline:7.0.10"])
def test_unknown_scopes(scope):
    tree = parse_scoped_config(EXAMPLE).tree
    assert not tree.contains(ScopeId.parse(scope))
    with pytest.raises(UnknownScope):
        tree.chain_for(ScopeId.parse(scope))


def test_scopes_lists_tree():
    tree = parse_scoped_config(EXAMPLE).tree
    assert [str(s) for s in tree.scopes()] == [
        "facility", "beamline:7.0.1", "beamline:8.3.2", "branch:7.0.1.1", "branch:7.0.1.2",
    ]


def test_defaults_when_sections_missing():
    cfg = parse_scoped_config("")
    assert cfg == ScopedConfig()
    assert cfg.facility_grants["/BCG_staff"] == "maintainer"
    assert cfg.tree.scopes() == [FACILITY]


@pytest.mark.parametrize(
    "text",
    [
        '[[beamline]]\nname = "7.0.1"\n[[beamline]]\nname = "7.0.1"\n',
        '[[beamline]]\nname = "a"\nbranches = ["x"]\n[[beamline]]\nname = "b"\nbranches = ["x"]\n',
        '[[beamline]]\nname = "_all"\n',
        '[[beamline]]\nname = ".."\n',
        '[facility_grants]\n"/x" = "superuser"\n',
        '[seed."k"]\ntype = "complex"\n',
        "not = [valid toml",
    ],
)
def test_bad_configs_rejected(text):
    with pytest.raises(ValueError):
        parse_scoped_config(text)


def test_load_from_env(tmp_path, monkeypatch):
    p = tmp_path / "scopes.toml"
    p.write_text(EXAMPLE, encoding="utf-8")
    monkeypatch.setenv("SCOPES_CONFIG", str(p))
    assert load_scoped_config().tree.label == "ALS"
    monkeypatch.delenv("SCOPES_CONFIG")
    assert load_scoped_config() == ScopedConfig()
