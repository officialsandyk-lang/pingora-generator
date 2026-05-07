from __future__ import annotations

from typing import Any, Optional, TypedDict


class GraphState(TypedDict, total=False):
    """
    Central LangGraph shared state for the AI Pingora Infrastructure Generator.

    This is the shared memory contract between:

    Agent 0   Preflight Doctor
    Agent 1   Prompt → Config
    Agent 1.5 Config Repair
    Agent 2A  Cargo/Rust Debug Agent
    Agent 2B  Runtime Self-Healing Agent
    Agent 2C  Docker Build Debug Agent
    Agent 3   Security Agent
    Agent 4   Docker/Compose/Predeploy
    Agent 5   Reliability Agent
    Agent 6   Config Update + Blue/Green Deployment

    Important:
    - state.py stores shared information.
    - graph.py decides routing.
    - agents/*.py repair/analyze.
    - core/*.py performs deterministic mechanics.
    """

    # ------------------------------------------------------------------
    # User input / request context
    # ------------------------------------------------------------------
    prompt: str
    update_prompt: str

    # ------------------------------------------------------------------
    # Project paths
    # ------------------------------------------------------------------
    project_root: str
    project_dir: str
    generated_project_dir: str
    active_project_dir: str
    inactive_project_dir: str

    # ------------------------------------------------------------------
    # Runtime mode flags
    # ------------------------------------------------------------------
    use_docker: bool
    use_docker_compose: bool
    use_predeploy_sandbox: bool
    use_bluegreen: bool

    # ------------------------------------------------------------------
    # Config lifecycle
    # ------------------------------------------------------------------
    raw_config: dict[str, Any]
    repaired_config: dict[str, Any]
    config: dict[str, Any]
    previous_config: dict[str, Any]
    updated_config: dict[str, Any]
    secured_config: dict[str, Any]

    # ------------------------------------------------------------------
    # Agent 0 — Preflight Doctor
    # ------------------------------------------------------------------
    preflight_ok: bool
    preflight_error: Optional[str]
    preflight_report: Optional[str]

    config_preflight_ok: bool
    config_preflight_error: Optional[str]
    config_preflight_report: Optional[str]

    # ------------------------------------------------------------------
    # Agent 1 / 1.5 — Config generation and repair
    # ------------------------------------------------------------------
    config_generated: bool
    config_repaired: bool
    config_validated: bool
    config_error: Optional[str]
    config_repair_report: Optional[str]

    # ------------------------------------------------------------------
    # Agent 3 — Security Agent
    # ------------------------------------------------------------------
    security_ok: bool
    security_error: Optional[str]
    security_report: Optional[str]

    # ------------------------------------------------------------------
    # Project generation status
    # ------------------------------------------------------------------
    project_generated: bool
    container_files_generated: bool
    dockerfile_generated: bool
    compose_files_generated: bool

    project_generation_error: Optional[str]
    container_generation_error: Optional[str]

    # ------------------------------------------------------------------
    # Cargo / Rust build status
    # Agent 2A owns cargo/rust compile failures.
    # ------------------------------------------------------------------
    cargo_ok: bool
    cargo_attempts: int
    cargo_error: Optional[str]
    rust_error: Optional[str]
    rust_logs: Optional[str]
    rust_project_dir: Optional[str]
    rust_repaired: bool
    rust_debug_report: Optional[str]

    # ------------------------------------------------------------------
    # Docker / Compose build status
    # Agent 2C owns Dockerfile/build/image failures.
    # ------------------------------------------------------------------
    docker_ok: bool
    compose_ok: bool
    docker_build_ok: bool
    compose_build_ok: bool

    docker_error: Optional[str]
    docker_logs: Optional[str]
    docker_project_dir: Optional[str]
    docker_repaired: bool
    docker_debug_report: Optional[str]

    # ------------------------------------------------------------------
    # Runtime status
    # Agent 2B owns runtime health failures, Pingora panics,
    # route failures, upstream resolution/connectivity issues.
    # ------------------------------------------------------------------
    runtime_ok: bool
    runtime_error: Optional[str]
    runtime_logs: Optional[str]
    runtime_project_dir: Optional[str]
    runtime_repaired: bool
    runtime_repair_attempts: int
    runtime_report: Optional[str]

    health_ok: bool
    health_error: Optional[str]
    health_url: Optional[str]
    route_test_url: Optional[str]
    route_test_status: Optional[int]

    # ------------------------------------------------------------------
    # Predeploy / sandbox status
    # Agent 4 owns sandbox verification, but this should not stop live
    # blue/green active stack.
    # ------------------------------------------------------------------
    predeploy_ok: bool
    predeploy_error: Optional[str]
    predeploy_report: Optional[str]

    # ------------------------------------------------------------------
    # Agent 5 — Reliability / readiness / protection checks
    # ------------------------------------------------------------------
    reliability_ok: bool
    reliability_error: Optional[str]
    reliability_report: Optional[str]

    protection_ok: bool
    protection_error: Optional[str]
    protection_report: Optional[str]

    performance_ok: bool
    performance_error: Optional[str]
    performance_report: Optional[str]

    readiness_ok: bool
    readiness_error: Optional[str]
    readiness_report: Optional[str]

    # ------------------------------------------------------------------
    # Agent 6 — Blue/green deployment state
    # ------------------------------------------------------------------
    deploy_ok: bool
    deploy_error: Optional[str]
    deploy_report: Optional[str]

    active_color: Optional[str]
    inactive_color: Optional[str]
    previous_color: Optional[str]

    active_version: Optional[str]
    previous_version: Optional[str]
    version: Optional[str]

    live_url: Optional[str]
    public_port: Optional[int]
    internal_port: Optional[int]
    health_port: Optional[int]

    traffic_switched: bool
    traffic_switch_error: Optional[str]

    rollback_available: bool
    rollback_ok: bool
    rollback_error: Optional[str]
    rollback_report: Optional[str]

    # ------------------------------------------------------------------
    # Failure routing
    # graph.py should inspect these fields to decide the next node.
    # ------------------------------------------------------------------
    failed_node: Optional[str]
    error: Optional[str]

    failure_type: Optional[str]
    """
    Expected values:
      - preflight
      - config
      - security
      - rust_compile
      - docker_build
      - runtime
      - readiness
      - traffic_switch
      - rollback
      - unknown
    """

    should_retry: bool
    retry_target_node: Optional[str]

    repair_attempts: int
    max_repair_attempts: int

    debug_repaired: bool
    runtime_should_repair: bool
    debug_should_repair: bool

    # ------------------------------------------------------------------
    # Cross-agent messages / reports
    # ------------------------------------------------------------------
    agent_reports: list[str]

    last_agent: Optional[str]
    next_agent: Optional[str]

    debug_report: Optional[str]
    final_message: Optional[str]