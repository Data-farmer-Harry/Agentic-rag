from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

import yaml

HERMES_HOME = Path(os.environ.get("HERMES_HOME", "/data/hermes"))
DEFAULT_CONFIG = Path("/opt/hermesgraph/config.yaml")
PLUGIN_SOURCE = Path("/opt/hermesgraph/plugin")
PLUGIN_TARGET = HERMES_HOME / "plugins" / "hermesgraph_bridge"


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    return loaded if isinstance(loaded, dict) else {}


def _deep_merge(base: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in current.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def main() -> None:
    HERMES_HOME.mkdir(parents=True, exist_ok=True)
    config_path = HERMES_HOME / "config.yaml"
    config = _deep_merge(_load_yaml(DEFAULT_CONFIG), _load_yaml(config_path))

    model = config.setdefault("model", {})
    if not isinstance(model, dict):
        model = {}
        config["model"] = model
    model_name = os.environ.get("HERMES_MODEL", "gpt-5.6")
    model_base_url = os.environ.get(
        "HERMES_MODEL_BASE_URL",
        "http://host.docker.internal:55523/v1",
    ).rstrip("/")
    api_mode = os.environ.get(
        "HERMES_MODEL_API_MODE",
        "chat_completions",
    )
    model["provider"] = "custom:hermesgraph"
    model["default"] = model_name
    model["base_url"] = model_base_url
    model["api_mode"] = api_mode
    model.pop("api_key", None)

    providers = config.setdefault("providers", {})
    if not isinstance(providers, dict):
        providers = {}
        config["providers"] = providers
    providers["hermesgraph"] = {
        "name": "HermesGraph Model Gateway",
        "base_url": model_base_url,
        "key_env": "OPENAI_API_KEY",
        "default_model": model_name,
        "transport": api_mode,
    }

    terminal = config.setdefault("terminal", {})
    if not isinstance(terminal, dict):
        terminal = {}
        config["terminal"] = terminal
    terminal["backend"] = os.environ.get("HERMES_TERMINAL_BACKEND", "docker")

    config_path.write_text(
        yaml.safe_dump(config, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    config_path.chmod(0o600)

    PLUGIN_TARGET.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(PLUGIN_SOURCE, PLUGIN_TARGET, dirs_exist_ok=True)


if __name__ == "__main__":
    main()
