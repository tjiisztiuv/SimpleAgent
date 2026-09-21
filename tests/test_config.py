from pathlib import Path

import pytest

from simpleagent.cli import main
from simpleagent.config import (
    Config,
    ConfigError,
    Profile,
    config_path,
    example_config,
    init_config,
    load_config,
    read_env_file,
)


def test_init_writes_example_config_that_loads(sa_home: Path):
    path = init_config()
    assert path == sa_home / "config.toml"

    config = load_config()
    assert config.default_profile == "deepseek"
    assert config.profiles["deepseek"].quirks.reasoning_echo == "current_turn"
    assert config.profiles["glm"].extra_body == {"thinking": {"type": "enabled"}}
    assert config.profiles["local"].api_key_env is None
    assert config.profiles["local"].quirks.reasoning_field == "reasoning"


def test_init_refuses_to_overwrite(sa_home: Path):
    init_config()
    with pytest.raises(ConfigError, match="已存在"):
        init_config()


def test_missing_config_hints_init(sa_home: Path):
    with pytest.raises(ConfigError, match="sa init"):
        load_config()


def _write(content: str) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_default_profile_must_exist(sa_home: Path):
    _write('default_profile = "x"\n[profiles.a]\nbase_url = "http://a"\nmodel = "m"\n')
    with pytest.raises(ConfigError, match="default_profile"):
        load_config()


def test_unknown_keys_are_rejected(sa_home: Path):
    # 拼错的配置项要报错，而不是被悄悄忽略
    _write('default_profile = "a"\n[profiles.a]\nbase_url = "http://a"\nmodel = "m"\nmodle = "x"\n')
    with pytest.raises(ConfigError, match="modle"):
        load_config()


def test_api_key_from_env(sa_home: Path, monkeypatch: pytest.MonkeyPatch):
    profile = Profile(base_url="http://a", model="m", api_key_env="SA_TEST_KEY")
    monkeypatch.delenv("SA_TEST_KEY", raising=False)
    with pytest.raises(ConfigError, match="SA_TEST_KEY"):
        profile.api_key()
    monkeypatch.setenv("SA_TEST_KEY", "sk-test")
    assert profile.api_key() == "sk-test"
    assert Profile(base_url="http://a", model="m").api_key() == "not-needed"


def test_api_key_from_env_file(sa_home: Path, monkeypatch: pytest.MonkeyPatch):
    sa_home.mkdir(parents=True)
    (sa_home / ".env").write_text(
        "# 注释\n\nSA_TEST_KEY=sk-from-file\nexport QUOTED='sk-quoted'\nBAD LINE\n",
        encoding="utf-8",
    )
    assert read_env_file() == {"SA_TEST_KEY": "sk-from-file", "QUOTED": "sk-quoted"}

    monkeypatch.delenv("SA_TEST_KEY", raising=False)
    profile = Profile(base_url="http://a", model="m", api_key_env="SA_TEST_KEY")
    assert profile.api_key() == "sk-from-file"
    monkeypatch.setenv("SA_TEST_KEY", "sk-from-env")
    assert profile.api_key() == "sk-from-env"  # 环境变量优先
    assert "SA_TEST_KEY" not in read_env_file(sa_home / "missing.env")


def test_key_pasted_into_api_key_env_is_not_leaked(sa_home: Path):
    secret = "sk-aaaabbbbccccdddd"
    _write(
        'default_profile = "a"\n[profiles.a]\nbase_url = "http://a"\nmodel = "m"\n'
        f'api_key_env = "{secret}"\n'
    )
    with pytest.raises(ConfigError) as excinfo:
        load_config()
    message = str(excinfo.value)
    assert "profiles.a.api_key_env" in message
    assert ".env" in message
    assert secret not in message


def test_cli_init_and_config_error(sa_home: Path, capsys: pytest.CaptureFixture[str]):
    assert main([]) == 1
    assert "sa init" in capsys.readouterr().err
    assert main(["init"]) == 0
    assert (sa_home / "config.toml").exists()


def test_cli_version(capsys: pytest.CaptureFixture[str]):
    with pytest.raises(SystemExit) as exit_info:
        main(["--version"])
    assert exit_info.value.code == 0
    name, _, number = capsys.readouterr().out.strip().partition(" ")
    assert name == "simpleagent" and number[0].isdigit()


def test_api_key_env_names_collects_all_profiles():
    config = Config.model_validate(
        {
            "default_profile": "a",
            "profiles": {
                "a": {"base_url": "http://a", "model": "m", "api_key_env": "A_KEY"},
                "b": {"base_url": "http://b", "model": "m", "api_key_env": "B_KEY"},
                "local": {"base_url": "http://localhost", "model": "m"},
            },
        }
    )
    assert config.api_key_env_names() == frozenset({"A_KEY", "B_KEY"})


def test_panel_config(sa_home: Path):
    base = 'default_profile = "a"\n[profiles.a]\nbase_url = "http://a"\nmodel = "m"\n'
    _write(base)
    assert load_config().panel.archive_after_minutes == 30  # 不写就是 30 分钟
    _write(base + "[panel]\narchive_after_minutes = 5\n")
    assert load_config().panel.archive_after_minutes == 5
    _write(base + "[panel]\narchive_after_minutes = 0\n")
    with pytest.raises(ConfigError, match="archive_after_minutes"):
        load_config()


# ------------------------------------------------------------------ MCP server（M5）

BASE = 'default_profile = "a"\n[profiles.a]\nbase_url = "http://a"\nmodel = "m"\n'


def test_mcp_server_defaults(sa_home: Path):
    _write(BASE + '[mcp_servers.fs]\ncommand = "npx"\n')
    server = load_config().mcp_servers["fs"]
    assert server.args == [] and server.env == {} and server.env_vars == []
    assert server.enabled and server.trust_annotations
    assert server.protocol == "auto"
    assert (server.startup_timeout, server.tool_timeout) == (60, 60)
    assert server.enabled_tools is None and server.disabled_tools == []


def test_no_mcp_servers_by_default(config: Config):
    assert config.mcp_servers == {}


@pytest.mark.parametrize("name", ["a__b", "a.b", "_a", "a_", "x" * 25, "中文"])
def test_mcp_server_name_rules(sa_home: Path, name: str):
    _write(BASE + f'[mcp_servers."{name}"]\ncommand = "npx"\n')
    with pytest.raises(ConfigError, match="MCP server 名"):
        load_config()


@pytest.mark.parametrize("name", ["fs", "my_server", "gh-2"])
def test_mcp_server_name_ok(sa_home: Path, name: str):
    _write(BASE + f'[mcp_servers."{name}"]\ncommand = "npx"\n')
    assert name in load_config().mcp_servers


def test_mcp_env_vars_must_be_names(sa_home: Path):
    _write(BASE + '[mcp_servers.gh]\ncommand = "npx"\nenv_vars = ["ghp_abc123-secret"]\n')
    with pytest.raises(ConfigError, match="要填环境变量名") as info:
        load_config()
    assert "ghp_abc123-secret" not in str(info.value)


@pytest.mark.parametrize("key", ["GITHUB_TOKEN", "OPENAI_API_KEY", "DB_PASSWORD", "client_secret"])
def test_mcp_secret_in_env_is_rejected_without_leaking(sa_home: Path, key: str):
    _write(BASE + f'[mcp_servers.gh]\ncommand = "npx"\nenv = {{ {key} = "sk-very-secret" }}\n')
    with pytest.raises(ConfigError, match=f'env_vars = \\["{key}"\\]') as info:
        load_config()
    assert "sk-very-secret" not in str(info.value)


def test_mcp_plain_env_is_allowed(sa_home: Path):
    env = 'env = { KEYBOARD_LAYOUT = "us", LOG_LEVEL = "info" }'
    _write(BASE + f'[mcp_servers.fs]\ncommand = "npx"\n{env}\n')
    assert load_config().mcp_servers["fs"].env == {"KEYBOARD_LAYOUT": "us", "LOG_LEVEL": "info"}


def test_mcp_unknown_field_and_bad_values(sa_home: Path):
    _write(BASE + '[mcp_servers.fs]\ncommand = "npx"\narg = ["x"]\n')
    with pytest.raises(ConfigError, match="arg"):
        load_config()
    _write(BASE + '[mcp_servers.fs]\ncommand = "npx"\nprotocol = "2026"\n')
    with pytest.raises(ConfigError, match="protocol"):
        load_config()
    _write(BASE + '[mcp_servers.fs]\ncommand = "npx"\npermissions = { write_file = "deny" }\n')
    with pytest.raises(ConfigError, match="permissions"):
        load_config()


def test_example_mcp_block_loads_when_uncommented(sa_home: Path):
    text = example_config()
    start = text.index("# [mcp_servers.filesystem]")
    block = "\n".join(line.removeprefix("# ") for line in text[start:].splitlines())
    _write(text[:start] + block)
    server = load_config().mcp_servers["filesystem"]
    assert server.command == "npx"
    assert server.args[-1] == "~/notes"
    assert server.env_vars == ["GITHUB_TOKEN"]
    assert server.permissions == {"write_file": "allow"}
