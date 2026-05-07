from typing import Any, Optional, TypedDict


class UpdateGraphState(TypedDict, total=False):
    project_id: str
    update_prompt: str

    project_root: str
    project_dir: str

    use_docker: bool
    use_docker_compose: bool
    use_predeploy_sandbox: bool

    existing_config: dict[str, Any]
    updated_config: dict[str, Any]
    repaired_config: dict[str, Any]
    config: dict[str, Any]

    cargo_ok: bool
    predeploy_ok: bool

    version_metadata: dict[str, Any]
    deployment_state: dict[str, Any]

    error: Optional[str]
    final_message: Optional[str]