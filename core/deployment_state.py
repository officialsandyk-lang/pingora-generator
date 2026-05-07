from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROJECT_NAME = "default-project"
PROJECT_STORE_DIR = PROJECT_ROOT / "generated-projects" / PROJECT_NAME
STATE_FILE = PROJECT_STORE_DIR / "deployment_state.json"

COLORS = ("blue", "green")


def now_utc() -> str:
    return datetime.now(UTC).isoformat()


def default_state() -> dict[str, Any]:
    return {
        "project_name": PROJECT_NAME,
        "active_color": None,
        "previous_color": None,
        "active_version": None,
        "previous_version": None,
        "live_url": None,
        "status": "empty",
        "updated_at": now_utc(),
        "history": [],
    }


def load_deployment_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return default_state()

    try:
        with STATE_FILE.open("r", encoding="utf-8") as f:
            loaded = json.load(f)
    except Exception:
        return default_state()

    state = default_state()
    state.update(loaded)
    return state


def save_deployment_state(state: dict[str, Any]) -> dict[str, Any]:
    PROJECT_STORE_DIR.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = now_utc()

    tmp = STATE_FILE.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)

    tmp.replace(STATE_FILE)
    return state


def get_active_color() -> str | None:
    state = load_deployment_state()
    color = state.get("active_color")
    return color if color in COLORS else None


def get_inactive_color() -> str:
    active = get_active_color()
    if active == "blue":
        return "green"
    if active == "green":
        return "blue"
    return "blue"


def mark_active(
    color: str,
    *,
    live_url: str | None = None,
    version: str | None = None,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if color not in COLORS:
        raise ValueError(f"Invalid deployment color: {color}")

    state = load_deployment_state()

    old_color = state.get("active_color")
    old_version = state.get("active_version")

    if old_color != color:
        state["previous_color"] = old_color
        state["previous_version"] = old_version

    state["active_color"] = color
    state["active_version"] = version or state.get("active_version")
    state["live_url"] = live_url or state.get("live_url")
    state["status"] = "active"

    event = {
        "time": now_utc(),
        "event": "promote",
        "active_color": color,
        "previous_color": state.get("previous_color"),
        "active_version": state.get("active_version"),
        "live_url": state.get("live_url"),
    }

    if config is not None:
        event["config_port"] = config.get("port")

    history = state.setdefault("history", [])
    history.append(event)
    state["history"] = history[-50:]

    return save_deployment_state(state)


def mark_failed(color: str, reason: str) -> dict[str, Any]:
    state = load_deployment_state()
    state["status"] = "failed"

    history = state.setdefault("history", [])
    history.append(
        {
            "time": now_utc(),
            "event": "failed",
            "color": color,
            "reason": reason,
        }
    )
    state["history"] = history[-50:]

    return save_deployment_state(state)