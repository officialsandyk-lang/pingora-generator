from __future__ import annotations

import json
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROJECT_NAME = "default-project"

GENERATED_PROJECT_DIR = PROJECT_ROOT / "generated-pingora-proxy"
PROJECT_STORE_DIR = PROJECT_ROOT / "generated-projects" / PROJECT_NAME

WORKSPACE_CONFIG_FILE = GENERATED_PROJECT_DIR / "config.json"
CURRENT_CONFIG_FILE = PROJECT_STORE_DIR / "current_config.json"


class ProjectStoreError(RuntimeError):
    pass


def ensure_project_store() -> None:
    PROJECT_STORE_DIR.mkdir(parents=True, exist_ok=True)
    GENERATED_PROJECT_DIR.mkdir(parents=True, exist_ok=True)


def read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    tmp = path.with_suffix(path.suffix + ".tmp")

    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

    tmp.replace(path)


def load_current_config(project_name: str = PROJECT_NAME) -> dict[str, Any]:
    """
    Load the current active config.

    Priority:
      1. generated-projects/default-project/current_config.json
      2. generated-pingora-proxy/config.json
      3. active blue/green color config, if deployment_state.json exists
    """
    project_store_dir = PROJECT_ROOT / "generated-projects" / project_name
    current_config_file = project_store_dir / "current_config.json"

    if current_config_file.exists():
        return read_json(current_config_file)

    if WORKSPACE_CONFIG_FILE.exists():
        return read_json(WORKSPACE_CONFIG_FILE)

    deployment_state_file = project_store_dir / "deployment_state.json"

    if deployment_state_file.exists():
        try:
            state = read_json(deployment_state_file)
            active_color = state.get("active_color")

            if active_color:
                color_config = (
                    project_store_dir
                    / active_color
                    / "generated-pingora-proxy"
                    / "config.json"
                )

                if color_config.exists():
                    return read_json(color_config)
        except Exception:
            pass

    raise ProjectStoreError(
        "No current config found. Expected one of:\n"
        f"- {current_config_file}\n"
        f"- {WORKSPACE_CONFIG_FILE}"
    )


def save_current_config(*args, **kwargs) -> dict[str, Any]:
    """
    Flexible save function.

    Supports old and new call styles:

      save_current_config(config)
      save_current_config(project_name, config)
      save_current_config(config=config)
      save_current_config(project_name="default-project", config=config)
    """
    project_name = kwargs.get("project_name", PROJECT_NAME)
    config = kwargs.get("config")

    if config is None:
        if len(args) == 1:
            config = args[0]
        elif len(args) == 2:
            project_name = str(args[0])
            config = args[1]
        else:
            raise TypeError(
                "save_current_config expects either "
                "save_current_config(config) or save_current_config(project_name, config)"
            )

    if not isinstance(config, dict):
        raise TypeError(f"config must be dict, got {type(config).__name__}")

    project_store_dir = PROJECT_ROOT / "generated-projects" / project_name
    current_config_file = project_store_dir / "current_config.json"

    write_json(current_config_file, config)
    write_json(WORKSPACE_CONFIG_FILE, config)

    return config


def save_project_config(config: dict[str, Any], project_name: str = PROJECT_NAME) -> dict[str, Any]:
    return save_current_config(project_name, config)


def load_project_config(project_name: str = PROJECT_NAME) -> dict[str, Any]:
    return load_current_config(project_name)