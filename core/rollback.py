from __future__ import annotations

from typing import Any

from core.bluegreen import load_color_config
from core.deployment_state import COLORS, load_deployment_state, mark_active
from core.traffic_switcher import switch_traffic_to


class RollbackError(RuntimeError):
    pass


def rollback_to_previous() -> dict[str, Any]:
    state = load_deployment_state()

    previous = state.get("previous_color")
    active = state.get("active_color")

    if previous not in COLORS:
        raise RollbackError("No previous blue/green deployment is available for rollback.")

    if previous == active:
        raise RollbackError("Previous color is already active.")

    previous_config = load_color_config(previous)
    live_url = switch_traffic_to(previous, previous_config)

    mark_active(
        previous,
        live_url=live_url,
        version=state.get("previous_version"),
        config=previous_config,
    )

    try:
        from core.project_store import save_current_config

        save_current_config(previous_config)
    except Exception:
        pass

    return {
        "rolled_back": True,
        "active_color": previous,
        "old_active_color": active,
        "live_url": live_url,
    }