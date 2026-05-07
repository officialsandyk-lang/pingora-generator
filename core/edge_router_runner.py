from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from core.edge_router_writer import (
    EDGE_CONTAINER_NAME,
    EDGE_NETWORK,
    write_edge_router_files,
)


class EdgeRouterError(RuntimeError):
    pass


def run_cmd(cmd: list[str], *, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    if check and result.returncode != 0:
        raise EdgeRouterError(
            "Command failed:\n"
            + " ".join(cmd)
            + "\n\nOutput:\n"
            + result.stdout
        )

    return result


def ensure_edge_network() -> None:
    inspect = subprocess.run(
        ["docker", "network", "inspect", EDGE_NETWORK],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    if inspect.returncode == 0:
        return

    run_cmd(["docker", "network", "create", EDGE_NETWORK])


def edge_router_is_running() -> bool:
    result = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Running}}", EDGE_CONTAINER_NAME],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )

    return result.returncode == 0 and result.stdout.strip() == "true"


def up_edge_router(active_color: str, config: dict[str, Any]) -> None:
    ensure_edge_network()
    compose_path = write_edge_router_files(active_color, config)

    run_cmd(
        ["docker", "compose", "-f", str(compose_path), "up", "-d"],
        cwd=compose_path.parent,
    )


def reload_edge_router(active_color: str, config: dict[str, Any]) -> None:
    ensure_edge_network()
    compose_path = write_edge_router_files(active_color, config)

    if not edge_router_is_running():
        up_edge_router(active_color, config)
        return

    # The config file is mounted from the host. Reload Nginx in-place.
    result = subprocess.run(
        ["docker", "exec", EDGE_CONTAINER_NAME, "nginx", "-s", "reload"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    if result.returncode != 0:
        raise EdgeRouterError(
            "Failed to reload edge router:\n"
            + result.stdout
            + "\n\nCompose file:\n"
            + str(compose_path)
        )