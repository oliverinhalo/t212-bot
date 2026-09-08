"""Synchronize .env and config.yaml with their .example templates.

Usage:
    python -m scripts.sync_config_env [--check] [--env-file .env] [--config-file config.yaml]

Ensures that any new environment variables or configuration options added to
.env.example or config.yaml.example are added to the live .env and config.yaml
without overwriting existing secrets, custom settings, or user comments.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any, Mapping

import yaml

log = logging.getLogger("sync_config_env")


def extract_env_keys(content: str) -> set[str]:
    """Find all variable names defined in an env file (set or commented out)."""
    keys: set[str] = set()
    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        match = re.match(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        if match:
            keys.add(match.group(1))
    return keys


def sync_env_file(
    env_path: Path,
    example_path: Path,
    *,
    dry_run: bool = False,
) -> tuple[bool, list[str]]:
    """Add missing keys from example_path to env_path.

    Returns (changed, list_of_added_keys).
    """
    if not example_path.exists():
        return False, []

    if not env_path.exists():
        if not dry_run:
            shutil.copy2(example_path, env_path)
        return True, ["(created from example)"]

    env_content = env_path.read_text(encoding="utf-8")
    example_content = example_path.read_text(encoding="utf-8")

    existing_keys = extract_env_keys(env_content)

    # Break example into blocks separated by double newlines or comments
    lines = example_content.splitlines()
    blocks: list[tuple[set[str], list[str]]] = []
    current_block: list[str] = []
    current_keys: set[str] = set()

    for line in lines:
        match = re.match(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=", line.strip())
        if match:
            current_keys.add(match.group(1))
        current_block.append(line)
        if not line.strip() and current_keys:
            blocks.append((current_keys, current_block))
            current_block = []
            current_keys = set()

    if current_block and current_keys:
        blocks.append((current_keys, current_block))

    added_keys: list[str] = []
    to_append: list[str] = []

    for keys, block_lines in blocks:
        missing_in_block = [k for k in keys if k not in existing_keys]
        if missing_in_block:
            added_keys.extend(missing_in_block)
            to_append.extend(block_lines)

    if not added_keys:
        return False, []

    if not dry_run:
        with env_path.open("a", encoding="utf-8") as handle:
            handle.write("\n\n# --- Automatically synchronized from .env.example ---\n")
            handle.write("\n".join(to_append) + "\n")

    return True, added_keys


def find_missing_yaml_keys(
    example: Mapping[str, Any], current: Mapping[str, Any], prefix: str = ""
) -> dict[str, Any]:
    """Recursively find keys present in example but absent in current."""
    missing: dict[str, Any] = {}
    for key, val in example.items():
        if key not in current:
            missing[key] = val
        elif isinstance(val, Mapping) and isinstance(current.get(key), Mapping):
            sub_missing = find_missing_yaml_keys(val, current[key], prefix=f"{prefix}{key}.")
            if sub_missing:
                missing[key] = sub_missing
    return missing


def sync_config_file(
    config_path: Path,
    example_path: Path,
    *,
    dry_run: bool = False,
) -> tuple[bool, list[str]]:
    """Add missing sections and keys from example_path to config_path preserving existing comments."""
    if not example_path.exists():
        return False, []

    if not config_path.exists():
        if not dry_run:
            shutil.copy2(example_path, config_path)
        return True, ["(created from example)"]

    config_text = config_path.read_text(encoding="utf-8")
    example_text = example_path.read_text(encoding="utf-8")

    try:
        current_data = yaml.safe_load(config_text) or {}
        example_data = yaml.safe_load(example_text) or {}
    except Exception as exc:
        log.warning("Could not parse YAML: %s", exc)
        return False, []

    if not isinstance(current_data, Mapping) or not isinstance(example_data, Mapping):
        return False, []

    missing = find_missing_yaml_keys(example_data, current_data)
    if not missing:
        return False, []

    added_paths: list[str] = []

    def record_paths(d: Mapping[str, Any], base: str = ""):
        for k, v in d.items():
            path = f"{base}.{k}" if base else k
            if isinstance(v, Mapping):
                record_paths(v, path)
            else:
                added_paths.append(path)

    record_paths(missing)

    if not dry_run:
        lines = config_text.splitlines()

        # For each top-level missing item or sub-item, append or insert into the text
        for top_key, top_val in missing.items():
            if top_key not in current_data:
                # Top level section missing completely: append with comment
                dumped = yaml.safe_dump({top_key: top_val}, sort_keys=False)
                lines.append(f"\n# --- Added {top_key} from config.yaml.example ---")
                lines.extend(dumped.splitlines())
            elif isinstance(top_val, Mapping):
                # Sub-keys missing: locate section and insert
                section_regex = re.compile(rf"^{re.escape(top_key)}:\s*")
                found_idx = -1
                for idx, line in enumerate(lines):
                    if section_regex.match(line):
                        found_idx = idx
                        break
                if found_idx != -1:
                    # Find end of this section (next unindented non-empty line or EOF)
                    insert_idx = found_idx + 1
                    while insert_idx < len(lines):
                        line = lines[insert_idx]
                        if line.strip() and not line.startswith(" ") and not line.startswith("\t") and not line.startswith("#"):
                            break
                        insert_idx += 1

                    dumped = yaml.safe_dump(top_val, sort_keys=False)
                    indented_dump = ["  " + l if l.strip() else l for l in dumped.splitlines()]
                    lines.insert(insert_idx, f"  # --- Added missing {top_key} keys from example ---")
                    for offset, l in enumerate(indented_dump):
                        lines.insert(insert_idx + 1 + offset, l)
                else:
                    dumped = yaml.safe_dump({top_key: top_val}, sort_keys=False)
                    lines.append(f"\n# --- Added {top_key} keys from config.yaml.example ---")
                    lines.extend(dumped.splitlines())

        config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    return True, added_paths


def sync_all(
    env_path: Path = Path(".env"),
    env_example: Path = Path(".env.example"),
    config_path: Path = Path("config.yaml"),
    config_example: Path = Path("config.yaml.example"),
    *,
    dry_run: bool = False,
) -> int:
    """Synchronize both .env and config.yaml."""
    print("==> Checking environment and configuration sync...")

    env_changed, env_added = sync_env_file(env_path, env_example, dry_run=dry_run)
    if env_changed:
        action = "Would add" if dry_run else "Added"
        print(f"  [+] {env_path}: {action} missing keys: {', '.join(env_added)}")
    else:
        print(f"  [OK] {env_path} is up to date.")

    config_changed, config_added = sync_config_file(config_path, config_example, dry_run=dry_run)
    if config_changed:
        action = "Would add" if dry_run else "Added"
        print(f"  [+] {config_path}: {action} missing settings: {', '.join(config_added)}")
    else:
        print(f"  [OK] {config_path} is up to date.")

    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Synchronize .env and config.yaml with examples")
    parser.add_argument("--check", action="store_true", help="Dry run — do not modify files")
    parser.add_argument("--env-file", type=Path, default=Path(".env"), help="Path to .env")
    parser.add_argument(
        "--env-example", type=Path, default=Path(".env.example"), help="Path to .env.example"
    )
    parser.add_argument(
        "--config-file", type=Path, default=Path("config.yaml"), help="Path to config.yaml"
    )
    parser.add_argument(
        "--config-example",
        type=Path,
        default=Path("config.yaml.example"),
        help="Path to config.yaml.example",
    )
    args = parser.parse_args(argv)

    return sync_all(
        env_path=args.env_file,
        env_example=args.env_example,
        config_path=args.config_file,
        config_example=args.config_example,
        dry_run=args.check,
    )


if __name__ == "__main__":
    sys.exit(main())
