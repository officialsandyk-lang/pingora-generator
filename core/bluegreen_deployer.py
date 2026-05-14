from __future__ import annotations

import copy
import json
import subprocess
import traceback
from pathlib import Path
from typing import Any

from core.bluegreen import deploy_inactive_color
from core.deployment_state import PROJECT_ROOT, mark_active
from core.edge_router_writer import EDGE_NETWORK
from core.project_store import save_current_config
from core.traffic_switcher import switch_traffic_to


def create_snapshot_best_effort() -> str | None:
    try:
        from core.version_manager import create_version_snapshot

        result = create_version_snapshot()
        return str(result) if result is not None else None
    except Exception:
        return None


def run_best_effort(cmd: list[str], cwd: Path | None = None) -> str:
    try:
        result = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        return result.stdout or ""
    except Exception as exc:
        return f"Failed to run {' '.join(cmd)}: {exc}"


def _extract_route_upstream_addresses(route: dict[str, Any]) -> list[str]:
    addresses: list[str] = []

    upstreams = route.get("upstreams")

    if isinstance(upstreams, list):
        for item in upstreams:
            if isinstance(item, dict):
                address = (
                    item.get("address")
                    or item.get("upstream")
                    or item.get("backend")
                    or item.get("target")
                    or item.get("url")
                )
            else:
                address = item

            if address:
                text = str(address).strip()

                if text and text not in addresses:
                    addresses.append(text)

    upstream = route.get("upstream")

    if upstream:
        text = str(upstream).strip()

        if text and "," not in text and text not in addresses:
            addresses.insert(0, text)

    return addresses


def _assert_config_has_valid_upstreams(config: dict[str, Any]) -> None:
    """
    Guard against invalid deployment configs.

    This catches:
    - comma-collapsed upstream strings
    - load-balanced routes losing upstreams[]
    - upstream entries without usable addresses
    """

    routes = config.get("routes") or []

    if not isinstance(routes, list):
        raise RuntimeError("Deployment config routes must be a list.")

    for route in routes:
        if not isinstance(route, dict):
            continue

        path = route.get("path") or "/"
        upstream = route.get("upstream")

        if isinstance(upstream, str) and "," in upstream:
            raise RuntimeError(
                f"Route {path} has invalid comma-collapsed upstream string: {upstream}. "
                "Expected upstreams[] for load-balanced routes."
            )

        upstreams = route.get("upstreams")

        if isinstance(upstreams, list) and len(upstreams) > 1:
            addresses = _extract_route_upstream_addresses(route)

            if len(addresses) <= 1:
                raise RuntimeError(
                    f"Load-balanced route {path} lost upstream addresses before deploy."
                )

            for address in addresses:
                if ":" not in address:
                    raise RuntimeError(
                        f"Route {path} has invalid upstream address before deploy: {address}"
                    )


def _compose_path_for_color(color: str) -> Path:
    return (
        PROJECT_ROOT
        / "generated-projects"
        / "default-project"
        / color
        / "generated-pingora-proxy"
        / "docker-compose.bluegreen.yml"
    )


def collect_bluegreen_deployer_evidence(
    *,
    stage: str,
    config: dict[str, Any],
    new_color: str | None,
    traffic_switched: bool,
    live_url: str | None,
    exc: Exception,
) -> str:
    """
    Collect concrete evidence around the full blue/green wrapper.

    This catches failures after deploy_inactive_color(), especially:
    - traffic switch failures
    - edge router failures
    - public gateway health-check failures
    - active state/save failures
    """

    public_port = config.get("port", 8088)

    parts: list[str] = []

    parts.append("BLUE/GREEN DEPLOYER FAILURE EVIDENCE")
    parts.append("=" * 80)
    parts.append(f"Stage: {stage}")
    parts.append(f"New color: {new_color}")
    parts.append(f"Traffic switched: {traffic_switched}")
    parts.append(f"Live URL: {live_url}")
    parts.append(f"Exception type: {type(exc).__name__}")
    parts.append(f"Exception: {exc}")

    parts.append("\n--- traceback ---")
    parts.append(traceback.format_exc())

    parts.append("\n--- config ---")
    try:
        parts.append(json.dumps(config, indent=2))
    except Exception:
        parts.append(str(config))

    parts.append("\n--- docker ps -a ---")
    parts.append(run_best_effort(["docker", "ps", "-a"]))

    parts.append("\n--- docker network ls ---")
    parts.append(run_best_effort(["docker", "network", "ls"]))

    parts.append(f"\n--- docker network inspect {EDGE_NETWORK} ---")
    parts.append(run_best_effort(["docker", "network", "inspect", EDGE_NETWORK]))

    parts.append("\n--- listening ports ---")
    parts.append(
        run_best_effort(
            [
                "bash",
                "-lc",
                f"ss -ltnp | grep -E '{public_port}|18089|18090' || true",
            ]
        )
    )

    for color in ["blue", "green"]:
        compose_path = _compose_path_for_color(color)
        project_dir = compose_path.parent

        parts.append(f"\n--- {color} compose path ---")
        parts.append(str(compose_path))

        parts.append(f"\n--- {color} docker-compose.bluegreen.yml ---")
        if compose_path.exists():
            try:
                parts.append(compose_path.read_text(encoding="utf-8"))
            except Exception as read_exc:
                parts.append(f"Could not read compose file: {read_exc}")
        else:
            parts.append("Compose file does not exist.")

        if compose_path.exists():
            parts.append(f"\n--- {color} docker compose config ---")
            parts.append(
                run_best_effort(
                    [
                        "docker",
                        "compose",
                        "-p",
                        f"pingora-{color}",
                        "-f",
                        str(compose_path),
                        "config",
                    ],
                    cwd=project_dir,
                )
            )

            parts.append(f"\n--- {color} docker compose ps -a ---")
            parts.append(
                run_best_effort(
                    [
                        "docker",
                        "compose",
                        "-p",
                        f"pingora-{color}",
                        "-f",
                        str(compose_path),
                        "ps",
                        "-a",
                    ],
                    cwd=project_dir,
                )
            )

            parts.append(f"\n--- {color} docker compose logs --tail 180 ---")
            parts.append(
                run_best_effort(
                    [
                        "docker",
                        "compose",
                        "-p",
                        f"pingora-{color}",
                        "-f",
                        str(compose_path),
                        "logs",
                        "--tail",
                        "180",
                    ],
                    cwd=project_dir,
                )
            )

    parts.append(f"\n--- public gateway probe http://127.0.0.1:{public_port}/ ---")
    parts.append(
        run_best_effort(
            [
                "bash",
                "-lc",
                f"curl -i --max-time 3 http://127.0.0.1:{public_port}/ || true",
            ]
        )
    )

    parts.append("\n--- blue local health-port probe http://127.0.0.1:18089/ ---")
    parts.append(
        run_best_effort(
            ["bash", "-lc", "curl -i --max-time 3 http://127.0.0.1:18089/ || true"]
        )
    )

    parts.append("\n--- green local health-port probe http://127.0.0.1:18090/ ---")
    parts.append(
        run_best_effort(
            ["bash", "-lc", "curl -i --max-time 3 http://127.0.0.1:18090/ || true"]
        )
    )

    return "\n".join(parts)


def deploy_config_bluegreen(config: dict[str, Any]) -> dict[str, Any]:
    """
    Shared blue/green deployment path for main.py and update.py.

    Flow:
    - validate canonical config
    - build inactive color
    - start inactive color
    - verify inactive color
    - switch edge router to inactive color
    - mark new color active
    - save current config

    This must never stop the active/live color before the new color is healthy.

    Production-safety rule:
    This function must not allow Docker/deploy helpers to mutate the canonical
    gateway config saved as current_config.json.
    """

    # Marker to prove whether main.py actually enters this deployer path.
    (PROJECT_ROOT / "bluegreen_deployer_entered.txt").write_text(
        "deploy_config_bluegreen was entered\n",
        encoding="utf-8",
    )

    canonical_config = copy.deepcopy(config or {})

    deploy_config = copy.deepcopy(canonical_config)
    switch_config = copy.deepcopy(canonical_config)
    state_config = copy.deepcopy(canonical_config)
    saved_config = copy.deepcopy(canonical_config)

    stage = "validate_deploy_config"
    new_color: str | None = None
    live_url: str | None = None
    version: str | None = None
    traffic_switched = False

    try:
        _assert_config_has_valid_upstreams(canonical_config)

        stage = "deploy_inactive_color"
        new_color = deploy_inactive_color(deploy_config)

        stage = "create_version_snapshot"
        version = create_snapshot_best_effort()

        stage = "traffic_switch"
        live_url = switch_traffic_to(new_color, switch_config)
        traffic_switched = True

        stage = "mark_active"
        mark_active(
            new_color,
            live_url=live_url,
            version=version,
            config=state_config,
        )

        stage = "save_current_config"
        save_current_config(saved_config)

        return {
            "success": True,
            "deployed": True,
            "traffic_switched": True,
            "active_color": new_color,
            "live_url": live_url,
            "version": version,
            "config": canonical_config,
        }

    except Exception as exc:
        evidence = collect_bluegreen_deployer_evidence(
            stage=stage,
            config=canonical_config,
            new_color=new_color,
            traffic_switched=traffic_switched,
            live_url=live_url,
            exc=exc,
        )

        evidence_file = PROJECT_ROOT / "bluegreen_deployer_failure_evidence.txt"
        evidence_file.write_text(evidence, encoding="utf-8")

        print("\n❌ BLUE/GREEN DEPLOYER REAL ERROR")
        print("=" * 80)
        print(evidence)
        print("=" * 80)
        print(f"\n📄 Evidence written to: {evidence_file}")

        raise RuntimeError(
            f"Blue/green deployer failed at stage: {stage}\n\n"
            f"Traffic switched: {traffic_switched}\n"
            f"New color: {new_color}\n"
            f"Live URL: {live_url}\n"
            f"Version: {version}\n\n"
            f"Original error:\n{exc}\n\n"
            f"{evidence}\n\n"
            f"Evidence file:\n{evidence_file}"
        ) from exc