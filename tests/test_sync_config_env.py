"""Tests for scripts/sync_config_env.py."""

from __future__ import annotations

from pathlib import Path
import yaml

from scripts.sync_config_env import (
    extract_env_keys,
    find_missing_yaml_keys,
    sync_config_file,
    sync_env_file,
    sync_all,
    main,
)


def test_extract_env_keys():
    content = """
    # Comment
    KEY1=value1
    export KEY2=value2
    # KEY3=commented_out

    KEY4="spaced value"
    """
    keys = extract_env_keys(content)
    assert keys == {"KEY1", "KEY2", "KEY4"}


def test_sync_env_file_creates_when_missing(tmp_path: Path):
    env_file = tmp_path / ".env"
    example_file = tmp_path / ".env.example"
    example_file.write_text("OMNIROUTE_API_KEY=xyz\nMODE=paper\n", encoding="utf-8")

    changed, added = sync_env_file(env_file, example_file)
    assert changed is True
    assert env_file.exists()
    assert env_file.read_text(encoding="utf-8") == "OMNIROUTE_API_KEY=xyz\nMODE=paper\n"


def test_sync_env_file_adds_missing_keys_preserving_existing(tmp_path: Path):
    env_file = tmp_path / ".env"
    env_file.write_text("MY_SECRET=12345\nMODE=live\n", encoding="utf-8")

    example_file = tmp_path / ".env.example"
    example_content = """# App Mode
MODE=paper

# OmniRoute AI key
OMNIROUTE_API_KEY=
OMNIROUTE_BASE_URL=https://omni.jacoblevy.co.uk/v1
"""
    example_file.write_text(example_content, encoding="utf-8")

    changed, added = sync_env_file(env_file, example_file)
    assert changed is True
    assert "OMNIROUTE_API_KEY" in added
    assert "OMNIROUTE_BASE_URL" in added

    content = env_file.read_text(encoding="utf-8")
    assert "MY_SECRET=12345" in content
    assert "MODE=live" in content
    assert "OMNIROUTE_API_KEY=" in content
    assert "OMNIROUTE_BASE_URL=" in content


def test_sync_env_file_no_changes_when_up_to_date(tmp_path: Path):
    env_file = tmp_path / ".env"
    env_file.write_text("MODE=demo\nOMNIROUTE_API_KEY=key\n", encoding="utf-8")

    example_file = tmp_path / ".env.example"
    example_file.write_text("MODE=paper\nOMNIROUTE_API_KEY=\n", encoding="utf-8")

    changed, added = sync_env_file(env_file, example_file)
    assert changed is False
    assert added == []


def test_find_missing_yaml_keys():
    example = {
        "capital": {"max_capital_gbp": 400.0, "min_order_gbp": 5.0},
        "ai": {"provider": "omniroute", "timeout_seconds": 60},
        "dashboard": {"port": 8080},
    }
    current = {
        "capital": {"max_capital_gbp": 500.0},
        "ai": {"provider": "openrouter"},
    }
    missing = find_missing_yaml_keys(example, current)
    assert missing == {
        "capital": {"min_order_gbp": 5.0},
        "ai": {"timeout_seconds": 60},
        "dashboard": {"port": 8080},
    }


def test_sync_config_file_creates_when_missing(tmp_path: Path):
    config_file = tmp_path / "config.yaml"
    example_file = tmp_path / "config.yaml.example"
    example_content = """capital:
  max_capital_gbp: 400.0
"""
    example_file.write_text(example_content, encoding="utf-8")

    changed, added = sync_config_file(config_file, example_file)
    assert changed is True
    assert config_file.exists()
    assert "capital" in config_file.read_text(encoding="utf-8")


def test_sync_config_file_inserts_missing_subkeys_and_sections(tmp_path: Path):
    config_file = tmp_path / "config.yaml"
    config_content = """# My custom configuration
capital:
  max_capital_gbp: 500.0 # Customized

ai:
  provider: omniroute
"""
    config_file.write_text(config_content, encoding="utf-8")

    example_file = tmp_path / "config.yaml.example"
    example_content = """capital:
  max_capital_gbp: 400.0
  per_trade_cap_pct: 2.5
  min_order_gbp: 5.0

ai:
  provider: omniroute
  timeout_seconds: 60

logging:
  level: INFO
"""
    example_file.write_text(example_content, encoding="utf-8")

    changed, added = sync_config_file(config_file, example_file)
    assert changed is True
    assert "capital.per_trade_cap_pct" in added
    assert "capital.min_order_gbp" in added
    assert "ai.timeout_seconds" in added
    assert "logging.level" in added

    updated_text = config_file.read_text(encoding="utf-8")
    assert "# My custom configuration" in updated_text
    assert "max_capital_gbp: 500.0 # Customized" in updated_text

    loaded = yaml.safe_load(updated_text)
    assert loaded["capital"]["max_capital_gbp"] == 500.0
    assert loaded["capital"]["per_trade_cap_pct"] == 2.5
    assert loaded["capital"]["min_order_gbp"] == 5.0
    assert loaded["ai"]["provider"] == "omniroute"
    assert loaded["ai"]["timeout_seconds"] == 60
    assert loaded["logging"]["level"] == "INFO"


def test_sync_all_and_cli_main(tmp_path: Path):
    env_file = tmp_path / ".env"
    env_example = tmp_path / ".env.example"
    env_example.write_text("KEY=1\n", encoding="utf-8")

    config_file = tmp_path / "config.yaml"
    config_example = tmp_path / "config.yaml.example"
    config_example.write_text("a: 1\n", encoding="utf-8")

    res = sync_all(
        env_path=env_file,
        env_example=env_example,
        config_path=config_file,
        config_example=config_example,
        dry_run=False,
    )
    assert res == 0
    assert env_file.exists()
    assert config_file.exists()

    # CLI main with --check
    exit_code = main([
        "--check",
        "--env-file", str(env_file),
        "--env-example", str(env_example),
        "--config-file", str(config_file),
        "--config-example", str(config_example),
    ])
    assert exit_code == 0
