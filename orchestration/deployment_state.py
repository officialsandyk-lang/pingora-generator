import json
from datetime import datetime, UTC

from core.project_store import project_dir


def deployment_state_path(project_id: str):
    return project_dir(project_id) / "deployment_state.json"


def default_state(project_id: str) -> dict:
    return {
        "project_id": project_id,
        "active_version": None,
        "previous_version": None,
        "active_color": "blue",
        "status": "new",
        "live_url": None,
        "updated_at": datetime.now(UTC).isoformat(),
    }


def load_deployment_state(project_id: str) -> dict:
    path = deployment_state_path(project_id)

    if not path.exists():
        return default_state(project_id)

    return json.loads(path.read_text())


def save_deployment_state(project_id: str, state: dict):
    path = deployment_state_path(project_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = datetime.now(UTC).isoformat()
    path.write_text(json.dumps(state, indent=2))


def mark_active_version(project_id: str, version: str, live_url: str):
    state = load_deployment_state(project_id)

    state["previous_version"] = state.get("active_version")
    state["active_version"] = version
    state["status"] = "healthy"
    state["live_url"] = live_url

    current_color = state.get("active_color", "blue")
    state["active_color"] = "green" if current_color == "blue" else "blue"

    save_deployment_state(project_id, state)

    print(f"✅ Active version updated: {version}")
    return state