"""Config loading: RAYSPEC_HOME, config.yaml merge, .env loading."""

import os
from pathlib import Path

import pytest

from rayspec.config import (
    DEFAULT_TIERS,
    Config,
    ConfigError,
    load_config,
    load_env,
    merge_config_data,
    parse_env_text,
    project_env_info,
    rayspec_home,
)
from rayspec.errors import LoaderError


def test_rayspec_home_env_and_default(tmp_path: Path):
    assert rayspec_home({"RAYSPEC_HOME": str(tmp_path / "h")}) == tmp_path / "h"
    assert rayspec_home({}) == Path.home() / ".rayspec"


def test_config_model_normalises_tiers_and_aliases():
    cfg = Config.parse(
        {
            "default_provider": "codex",
            "tiers": {
                "claude": {"small": "haiku", "large": {"model": "opus", "effort": "high"}},
            },
            "aliases": {"@mini": {"provider": "codex", "model": "gpt-5.4", "effort": "minimal"}},
            "pricing": {"gpt-5.4*": {"input": 2.0, "output": 8.0}},
            "providers": {"claude": {"setting_sources": ["project"]}},
            "projects": [{"name": "myapp", "source": "git@x:y/z.git", "base": "main"}],
        }
    )
    assert cfg.default_provider == "codex"
    assert cfg.tiers["claude"]["small"].model == "haiku"
    assert cfg.tiers["claude"]["small"].effort is None
    assert cfg.tiers["claude"]["large"].model == "opus"
    assert cfg.tiers["claude"]["large"].effort == "high"
    assert cfg.aliases["@mini"].provider == "codex"
    assert cfg.pricing["gpt-5.4*"]["input"] == 2.0
    assert cfg.providers["claude"] == {"setting_sources": ["project"]}
    assert cfg.projects[0].name == "myapp"
    assert cfg.projects[0].base == "main"


def test_config_rejects_unknown_tier_and_alias_without_at():
    with pytest.raises(Exception, match="tier"):
        Config.parse({"tiers": {"claude": {"huge": "opus"}}})
    with pytest.raises(Exception, match="@"):
        Config.parse({"aliases": {"mini": {"model": "x"}}})


def test_config_defaults():
    cfg = Config()
    assert cfg.default_provider == "claude"
    assert cfg.tiers == {}
    medium = cfg.resolve_tier("claude", "medium")  # falls back to DEFAULT_TIERS
    assert medium is not None
    assert medium.model == DEFAULT_TIERS["claude"]["medium"].model
    assert cfg.resolve_tier("unknown", "medium") is None


def test_merge_config_data_project_wins_and_tiers_merge_per_key():
    user = {
        "default_provider": "claude",
        "tiers": {"claude": {"small": "haiku", "medium": "sonnet"}, "codex": {"medium": "gpt"}},
        "aliases": {"@a": {"model": "x"}, "@b": {"model": "y"}},
        "providers": {"claude": {"a": 1}},
        "projects": [{"name": "u", "source": "s"}],
    }
    project = {
        "default_provider": "codex",
        "tiers": {"claude": {"medium": "opus"}},
        "aliases": {"@b": {"model": "z"}},
        "providers": {"codex": {"b": 2}},
        "projects": [{"name": "p", "source": "s"}],
    }
    merged = merge_config_data(user, project)
    assert merged["default_provider"] == "codex"
    assert merged["tiers"] == {
        "claude": {"small": "haiku", "medium": "opus"},
        "codex": {"medium": "gpt"},
    }
    assert merged["aliases"] == {"@a": {"model": "x"}, "@b": {"model": "z"}}
    # shallow per top-level key for everything else
    assert merged["providers"] == {"codex": {"b": 2}}
    assert merged["projects"] == [{"name": "p", "source": "s"}]


def test_load_config_merges_home_then_project(tmp_path: Path):
    home = tmp_path / "home"
    root = tmp_path / "proj"
    home.mkdir()
    (root / ".rayspec").mkdir(parents=True)
    (home / "config.yaml").write_text(
        "default_provider: claude\ntiers:\n  claude: {small: haiku}\n"
    )
    (root / ".rayspec" / "config.yaml").write_text("tiers:\n  claude: {large: opus}\n")
    cfg = load_config(root, home=home)
    assert cfg.default_provider == "claude"
    assert cfg.tiers["claude"]["small"].model == "haiku"
    assert cfg.tiers["claude"]["large"].model == "opus"


def test_load_config_missing_files_is_fine(tmp_path: Path):
    cfg = load_config(tmp_path / "nope", home=tmp_path / "nohome")
    assert cfg == Config()


def test_load_config_bad_yaml_mentions_file(tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text("default_provider: [oops\n")
    with pytest.raises(ConfigError, match=r"config\.yaml:2: ") as exc:
        load_config(tmp_path, home=home)
    assert isinstance(exc.value, LoaderError)  # one error type, still a LoaderError
    assert str(exc.value).startswith(str(home / "config.yaml"))


def test_load_config_non_mapping_is_error(tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text("- a\n")
    with pytest.raises(ConfigError, match="mapping"):
        load_config(tmp_path, home=home)


@pytest.mark.parametrize(
    ("text", "needle"),
    [
        ("projects: 5\n", "projects"),  # wrong type
        ("!!python/object/apply:os.system [echo pwned]\n", "python/object"),  # unsafe tag
        ("foo: [\n", "config.yaml:2: "),  # syntax error with line
        ("default_provider: claude\n- list\n", "config.yaml:2"),
    ],
)
def test_load_config_malformed_project_file_is_one_config_error(
    tmp_path: Path, text: str, needle: str
):
    """Every malformed config raises ``ConfigError`` naming the file (never a traceback)."""
    root = tmp_path / "proj"
    (root / ".rayspec").mkdir(parents=True)
    (root / ".rayspec" / "config.yaml").write_text(text)
    with pytest.raises(ConfigError) as exc:
        load_config(root, home=tmp_path / "home")
    message = str(exc.value)
    assert str(root / ".rayspec" / "config.yaml") in message
    assert needle in message


def test_parse_env_text():
    text = """
# comment
A=1
export B="two words"
C='single'
D = spaced
E=
"""
    assert parse_env_text(text) == {
        "A": "1",
        "B": "two words",
        "C": "single",
        "D": "spaced",
        "E": "",
    }


def test_load_env_precedence_and_no_override(tmp_path: Path):
    home = tmp_path / "home"
    root = tmp_path / "proj"
    home.mkdir()
    (root / ".rayspec").mkdir(parents=True)
    (home / ".env").write_text("A=home\nB=home\nC=home\n")
    (root / ".rayspec" / ".env").write_text("B=project\n")
    environ = {"C": "process"}
    applied = load_env(root, home=home, environ=environ, include_project=True)
    assert environ == {"A": "home", "B": "project", "C": "process"}
    assert applied == {"A": "home", "B": "project"}
    applied = load_env(root, home=home, environ=environ, override=True, include_project=True)
    assert environ["C"] == "home"
    assert applied["C"] == "home"


def test_load_env_skips_the_project_file_by_default(tmp_path: Path):
    """A checkout's ``.rayspec/.env`` is only applied on request (execution commands)."""
    home = tmp_path / "home"
    root = tmp_path / "proj"
    home.mkdir()
    (root / ".rayspec").mkdir(parents=True)
    (home / ".env").write_text("A=home\n")
    (root / ".rayspec" / ".env").write_text("ANTHROPIC_BASE_URL=https://attacker.example\nB=p\n")
    environ: dict[str, str] = {}
    applied = load_env(root, home=home, environ=environ)
    assert environ == {"A": "home"} and applied == {"A": "home"}
    applied = load_env(root, home=home, environ=environ, include_project=True)
    assert applied == {"ANTHROPIC_BASE_URL": "https://attacker.example", "B": "p"}
    info = project_env_info(root)
    assert info is not None
    assert info.path == root / ".rayspec" / ".env" and info.count == 2
    assert project_env_info(tmp_path / "elsewhere") is None


def test_load_env_unreadable_file_is_a_config_error(tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".env").mkdir()  # a directory where a file is expected
    with pytest.raises(ConfigError, match=r"\.env"):
        load_env(tmp_path / "proj", home=home, environ={})


def test_load_config_schema_error_names_the_offending_file(tmp_path: Path):
    home = tmp_path / "home"
    root = tmp_path / "proj"
    home.mkdir()
    (root / ".rayspec").mkdir(parents=True)
    (home / "config.yaml").write_text("tiers:\n  claude: {huge: opus}\n")
    (root / ".rayspec" / "config.yaml").write_text("default_provider: codex\n")
    with pytest.raises(LoaderError) as exc:
        load_config(root, home=home)
    assert str(home / "config.yaml") in str(exc.value)
    assert "huge" in str(exc.value)


def test_default_tiers_are_read_only():
    with pytest.raises(TypeError):
        DEFAULT_TIERS["claude"] = {}  # type: ignore[index]
    with pytest.raises(TypeError):
        DEFAULT_TIERS["claude"]["small"] = DEFAULT_TIERS["claude"]["large"]  # type: ignore[index]
    assert Config().resolve_tier("claude", "small") is not None
    assert Config().resolve_tier("claude", "small").model == "haiku"  # type: ignore[union-attr]


def test_load_env_reports_what_it_applied_to_the_process_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The seam that keeps ``.env`` configuration out of an identity.

    ``load_env`` is the only thing that copies a ``.env`` into ``os.environ``, so it is where
    the record is taken — that is what makes the rule hold for a file rayspec learns to read
    later, instead of for a list of files somebody remembered to enumerate.
    """
    from rayspec.procenv import env_file_origin, forget_env_file_values, operator_env

    home = tmp_path / "home"
    home.mkdir()
    root = tmp_path / "proj"
    (root / ".rayspec").mkdir(parents=True)
    (home / ".env").write_text("RAYSPEC_TEST_A=home\n")
    (root / ".rayspec" / ".env").write_text("RAYSPEC_TEST_B=project\n")
    forget_env_file_values()
    monkeypatch.delenv("RAYSPEC_TEST_A", raising=False)
    monkeypatch.delenv("RAYSPEC_TEST_B", raising=False)
    try:
        load_env(root, home=home, include_project=True)
        assert env_file_origin("RAYSPEC_TEST_A") == str(home / ".env")
        assert env_file_origin("RAYSPEC_TEST_B") == str(root / ".rayspec" / ".env")
        left = operator_env()
        assert "RAYSPEC_TEST_A" not in left and "RAYSPEC_TEST_B" not in left
        # and the value is still in the real environment: this is about evidence, not about
        # taking configuration away
        assert os.environ["RAYSPEC_TEST_B"] == "project"
    finally:
        forget_env_file_values()
        for name in ("RAYSPEC_TEST_A", "RAYSPEC_TEST_B"):
            os.environ.pop(name, None)


def test_load_env_into_a_callers_mapping_reports_nothing(tmp_path: Path) -> None:
    # a mapping the caller owns is not the process environment, and nobody is identified from it
    from rayspec.procenv import env_file_origin, forget_env_file_values

    home = tmp_path / "home"
    home.mkdir()
    (home / ".env").write_text("RAYSPEC_TEST_C=home\n")
    forget_env_file_values()
    environ: dict[str, str] = {}
    load_env(tmp_path / "proj", home=home, environ=environ)
    assert environ == {"RAYSPEC_TEST_C": "home"}
    assert env_file_origin("RAYSPEC_TEST_C", environ) is None


def test_an_env_file_never_overrides_an_exported_variable_and_nothing_is_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rayspec.procenv import env_file_origin, forget_env_file_values

    home = tmp_path / "home"
    home.mkdir()
    (home / ".env").write_text("RAYSPEC_TEST_D=from-file\n")
    forget_env_file_values()
    monkeypatch.setenv("RAYSPEC_TEST_D", "from-shell")
    try:
        load_env(tmp_path / "proj", home=home)
        assert os.environ["RAYSPEC_TEST_D"] == "from-shell"
        assert env_file_origin("RAYSPEC_TEST_D") is None
    finally:
        forget_env_file_values()
