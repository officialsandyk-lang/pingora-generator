from __future__ import annotations

from typing import Any

from core.bluegreen import deploy_inactive_color
from core.deployment_state import mark_active
from core.project_store import save_current_config
from core.traffic_switcher import switch_traffic_to


def create_snapshot_best_effort() -> str | None:
    try:
        from core.version_manager import create_version_snapshot

        result = create_version_snapshot()
        return str(result) if result is not None else None
    except Exception:
        return None


def deploy_config_bluegreen(config: dict[str, Any]) -> dict[str, Any]:
    """
    Shared blue/green deployment path for main.py and update.py.

    Flow:
    - build inactive color
    - start inactive color
    - verify inactive color
    - switch edge router to inactive color
    - mark new color active
    - save current config

    This must never stop the active/live color.
    """
    new_color = deploy_inactive_color(config)

    version = create_snapshot_best_effort()

    live_url = switch_traffic_to(new_color, config)

    mark_active(
        new_color,
        live_url=live_url,
        version=version,
        config=config,
    )

    save_current_config(config)

    return {
        "success": True,
        "active_color": new_color,
        "live_url": live_url,
        "version": version,
        "config": config,
    }