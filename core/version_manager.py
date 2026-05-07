import json
import shutil
from datetime import datetime, UTC
from pathlib import Path

from core.project_store import (
    GENERATED_PROJECT_DIR,
    ensure_project_store,
    versions_dir,
    save_current_config,
)


def list_versions(project_id: str) -> list[str]:
    base = versions_dir(project_id)

    if not base.exists():
        return []

    return sorted(
        [path.name for path in base.iterdir() if path.is_dir() and path.name.startswith("v")],
        key=lambda value: int(value.replace("v", "")),
    )


def next_version(project_id: str) -> str:
    versions = list_versions(project_id)

    if not versions:
        return "v1"

    latest = max(int(version.replace("v", "")) for version in versions)
    return f"v{latest + 1}"


def version_dir(project_id: str, version: str) -> Path:
    return versions_dir(project_id) / version


def create_version_snapshot(project_id: str, config: dict, status: str = "verified") -> dict:
    ensure_project_store(project_id)

    version = next_version(project_id)
    target_dir = version_dir(project_id, version)
    generated_copy = target_dir / "generated-pingora-proxy"

    target_dir.mkdir(parents=True, exist_ok=True)

    if generated_copy.exists():
        shutil.rmtree(generated_copy)

    if not GENERATED_PROJECT_DIR.exists():
        raise FileNotFoundError("generated-pingora-proxy does not exist.")

    shutil.copytree(GENERATED_PROJECT_DIR, generated_copy)

    metadata = {
        "project_id": project_id,
        "version": version,
        "status": status,
        "created_at": datetime.now(UTC).isoformat(),
        "config": config,
        "generated_project": str(generated_copy),
    }

    (target_dir / "config.json").write_text(json.dumps(config, indent=2))
    (target_dir / "metadata.json").write_text(json.dumps(metadata, indent=2))

    save_current_config(project_id, config)

    print(f"✅ Version snapshot created: {project_id}/{version}")

    return metadata