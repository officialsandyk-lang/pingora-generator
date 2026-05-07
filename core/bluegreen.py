from __future__ import annotations

import json
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

from core.deployment_state import (
    COLORS,
    PROJECT_ROOT,
    PROJECT_STORE_DIR,
    get_inactive_color,
    mark_failed,
)
from core.edge_router_runner import ensure_edge_network
from core.edge_router_writer import (
    EDGE_NETWORK,
    internal_port_from_config,
    public_port_from_config,
)

BUILD_WORKSPACE = PROJECT_ROOT / "generated-pingora-proxy"


class BlueGreenError(RuntimeError):
    pass


class BlueGreenReadinessError(BlueGreenError):
    """
    Runtime/container readiness failure.

    This should be handled by LangGraph and routed to runtime_agent.py,
    not directly to Debug Agent.
    """
    pass


def color_dir(color: str) -> Path:
    if color not in COLORS:
        raise ValueError(f"Invalid color: {color}")

    return PROJECT_STORE_DIR / color / "generated-pingora-proxy"


def color_compose_path(color: str) -> Path:
    return color_dir(color) / "docker-compose.bluegreen.yml"


def color_project_name(color: str) -> str:
    return f"pingora-{color}"


def color_container_name(color: str) -> str:
    return f"pingora-proxy-{color}"


def color_network_alias(color: str) -> str:
    return f"pingora-{color}"


def color_health_port(color: str, config: dict[str, Any]) -> int:
    """
    Derive a stable local-only health port from the user's selected public port.

    Example:
      public port 9000:
        blue  -> 19001
        green -> 19002

      public port 8088:
        blue  -> 18089
        green -> 18090
    """
    if color not in COLORS:
        raise ValueError(f"Invalid color: {color}")

    public_port = public_port_from_config(config)
    offset = 1 if color == "blue" else 2

    candidate = public_port + 10000 + offset

    if candidate > 65000:
        candidate = 20000 + (public_port % 30000) + offset

    if candidate == public_port:
        candidate += 100

    if candidate < 1024:
        candidate += 20000

    if candidate > 65535:
        raise BlueGreenError(
            f"Could not calculate a valid health port for {color} from public port {public_port}"
        )

    return candidate


def ask_debug_agent(stage: str, output: str, cwd: Path | None = None) -> str | None:
    """
    Best-effort adapter for your existing Debug Agent.

    Debug Agent owns:
      - Dockerfile missing
      - Docker build errors
      - Rust compile/cargo errors
      - generation/build-prep failures

    Runtime readiness failures should go to Runtime Agent through LangGraph.
    """
    try:
        import agents.debug_agent as debug_agent
    except Exception:
        return None

    function_names = [
        "debug_docker_error",
        "debug_rust_error",
        "debug_error",
        "analyze_error",
        "run_debug_agent",
    ]

    for name in function_names:
        fn = getattr(debug_agent, name, None)

        if not callable(fn):
            continue

        call_attempts = [
            (
                (),
                {
                    "stage": stage,
                    "error": output,
                    "project_dir": str(cwd) if cwd else None,
                },
            ),
            (
                (),
                {
                    "stage": stage,
                    "output": output,
                    "project_dir": str(cwd) if cwd else None,
                },
            ),
            (
                (),
                {
                    "error": output,
                    "project_dir": str(cwd) if cwd else None,
                },
            ),
            ((stage, output), {}),
            ((output,), {}),
        ]

        for args, kwargs in call_attempts:
            try:
                response = fn(*args, **kwargs)

                if response:
                    return str(response)
            except TypeError:
                continue
            except Exception as exc:
                return f"Debug Agent failed while handling {stage}: {exc}"

    return None


def raise_with_debug_agent(
    *,
    stage: str,
    message: str,
    cwd: Path | None = None,
) -> None:
    """
    Route pre-build/generation errors to Debug Agent before failing.
    """
    debug_note = ask_debug_agent(stage=stage, output=message, cwd=cwd)

    final_message = message

    if debug_note:
        final_message += f"\n\nDebug Agent:\n{debug_note}"

    raise BlueGreenError(final_message)


def run_cmd(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
    stage: str = "command",
) -> subprocess.CompletedProcess:
    result = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    if check and result.returncode != 0:
        output = result.stdout or ""
        debug_note = ask_debug_agent(stage=stage, output=output, cwd=cwd)

        message = (
            f"{stage} failed.\n\n"
            f"Command:\n{' '.join(cmd)}\n\n"
            f"Output:\n{output}"
        )

        if debug_note:
            message += f"\n\nDebug Agent:\n{debug_note}"

        raise BlueGreenError(message)

    return result


def write_color_compose(color: str, config: dict[str, Any]) -> Path:
    project = color_dir(color)
    project.mkdir(parents=True, exist_ok=True)

    internal_port = internal_port_from_config(config)
    health_port = color_health_port(color, config)

    compose = f"""services:
  pingora-proxy:
    container_name: {color_container_name(color)}
    build:
      context: .
    restart: unless-stopped
    expose:
      - "{internal_port}"
    ports:
      - "127.0.0.1:{health_port}:{internal_port}"
    extra_hosts:
      - "host.docker.internal:host-gateway"
    networks:
      {EDGE_NETWORK}:
        aliases:
          - {color_network_alias(color)}

networks:
  {EDGE_NETWORK}:
    external: true
"""

    path = color_compose_path(color)
    path.write_text(compose, encoding="utf-8")

    return path


def stop_color(color: str) -> None:
    """
    Safe to call only for inactive color.

    This can run docker compose down, but only on the inactive color selected by
    deploy_inactive_color(). It must not be called on the active color.
    """
    compose_path = color_compose_path(color)

    if compose_path.exists():
        subprocess.run(
            [
                "docker",
                "compose",
                "-p",
                color_project_name(color),
                "-f",
                str(compose_path),
                "down",
                "--remove-orphans",
            ],
            cwd=str(compose_path.parent),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )

    subprocess.run(
        ["docker", "rm", "-f", color_container_name(color)],
        text=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def call_generator(
    fn: Callable[..., Any],
    config: dict[str, Any],
    project_dir: Path,
) -> None:
    """
    Supports different writer signatures used across your project:

      fn(config, project_dir=project_dir)
      fn(project_dir, config)
      fn(config)
    """
    try:
        fn(config, project_dir=project_dir)
        return
    except TypeError:
        pass

    try:
        fn(project_dir, config)
        return
    except TypeError:
        pass

    fn(config)


def render_project_to_workspace(config: dict[str, Any]) -> Path:
    """
    Generate the build workspace.

    generated-pingora-proxy is now only a temporary build workspace.
    The live blue/green stacks are copied into generated-projects/default-project.

    Blue/green Compose builds with:

        build:
          context: .

    So this workspace must contain:
      - Dockerfile
      - Cargo.toml
      - src/main.rs
      - config.json

    Missing Docker/build-prep files are sent to Debug Agent.
    """
    from core.project_writer import write_project

    if BUILD_WORKSPACE.exists():
        shutil.rmtree(BUILD_WORKSPACE)

    try:
        call_generator(write_project, config, BUILD_WORKSPACE)
    except Exception as exc:
        raise_with_debug_agent(
            stage="project generation",
            message=(
                "project_writer failed while generating generated-pingora-proxy.\n\n"
                f"Expected workspace:\n{BUILD_WORKSPACE}\n\n"
                f"Error:\n{exc}\n\n"
                f"Config:\n{json.dumps(config, indent=2)}"
            ),
            cwd=BUILD_WORKSPACE,
        )

    try:
        from core.docker_writer import write_docker_files

        call_generator(write_docker_files, config, BUILD_WORKSPACE)
    except ImportError as exc:
        raise_with_debug_agent(
            stage="docker file generation",
            message=(
                "Could not import core.docker_writer.write_docker_files.\n\n"
                "Blue/green deployment requires generated-pingora-proxy/Dockerfile.\n\n"
                f"Error:\n{exc}"
            ),
            cwd=BUILD_WORKSPACE,
        )
    except Exception as exc:
        raise_with_debug_agent(
            stage="docker file generation",
            message=(
                "docker_writer failed while generating Docker files.\n\n"
                f"Expected Dockerfile:\n{BUILD_WORKSPACE / 'Dockerfile'}\n\n"
                f"Error:\n{exc}\n\n"
                f"Config:\n{json.dumps(config, indent=2)}"
            ),
            cwd=BUILD_WORKSPACE,
        )

    try:
        from core.compose_writer import write_compose_files

        call_generator(write_compose_files, config, BUILD_WORKSPACE)
    except Exception as exc:
        debug_note = ask_debug_agent(
            stage="compose file generation",
            output=(
                "compose_writer failed while generating Compose files.\n\n"
                f"Error:\n{exc}\n\n"
                f"Config:\n{json.dumps(config, indent=2)}"
            ),
            cwd=BUILD_WORKSPACE,
        )

        print("")
        print("⚠️ Compose generation failed.")

        if debug_note:
            print("Debug Agent:")
            print(debug_note)

    if not BUILD_WORKSPACE.exists():
        raise_with_debug_agent(
            stage="project generation",
            message=f"Build workspace was not created: {BUILD_WORKSPACE}",
            cwd=PROJECT_ROOT,
        )

    dockerfile = BUILD_WORKSPACE / "Dockerfile"

    if not dockerfile.exists():
        raise_with_debug_agent(
            stage="docker file generation",
            message=(
                "Dockerfile was not created in generated-pingora-proxy.\n\n"
                f"Expected file:\n{dockerfile}\n\n"
                "This should be fixed by core/docker_writer.py. "
                "write_docker_files(config) must create a Dockerfile in the generated project folder.\n\n"
                f"Config:\n{json.dumps(config, indent=2)}"
            ),
            cwd=BUILD_WORKSPACE,
        )

    cargo_toml = BUILD_WORKSPACE / "Cargo.toml"

    if not cargo_toml.exists():
        raise_with_debug_agent(
            stage="rust project generation",
            message=(
                "Cargo.toml was not created in generated-pingora-proxy.\n\n"
                f"Expected file:\n{cargo_toml}\n\n"
                "project_writer.py must generate a valid Rust project before Docker build.\n\n"
                f"Config:\n{json.dumps(config, indent=2)}"
            ),
            cwd=BUILD_WORKSPACE,
        )

    src_main = BUILD_WORKSPACE / "src" / "main.rs"

    if not src_main.exists():
        raise_with_debug_agent(
            stage="rust project generation",
            message=(
                "src/main.rs was not created in generated-pingora-proxy.\n\n"
                f"Expected file:\n{src_main}\n\n"
                "project_writer.py must generate Pingora Rust source before Docker build.\n\n"
                f"Config:\n{json.dumps(config, indent=2)}"
            ),
            cwd=BUILD_WORKSPACE,
        )

    config_path = BUILD_WORKSPACE / "config.json"

    with config_path.open("w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)

    return BUILD_WORKSPACE


def prepare_color_workspace(color: str, config: dict[str, Any]) -> Path:
    """
    Prepare inactive color workspace.

    This only stops/removes the inactive color selected by deploy_inactive_color().
    """
    stop_color(color)

    target = color_dir(color)

    if target.exists():
        shutil.rmtree(target)

    workspace = render_project_to_workspace(config)
    shutil.copytree(workspace, target)

    write_color_compose(color, config)

    return target


def compose_cmd(color: str, *args: str) -> list[str]:
    return [
        "docker",
        "compose",
        "-p",
        color_project_name(color),
        "-f",
        str(color_compose_path(color)),
        *args,
    ]


def build_color(color: str) -> None:
    project = color_dir(color)

    run_cmd(
        compose_cmd(color, "build"),
        cwd=project,
        stage=f"docker build {color}",
    )


def start_color(color: str) -> None:
    project = color_dir(color)

    run_cmd(
        compose_cmd(color, "up", "-d"),
        cwd=project,
        stage=f"docker compose up {color}",
    )


def http_status(url: str, timeout: float = 2.0) -> int | None:
    try:
        req = urllib.request.Request(url, method="GET")

        with urllib.request.urlopen(req, timeout=timeout) as response:
            return int(response.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)
    except Exception:
        return None


def tcp_port_open(host: str, port: int, timeout: float = 1.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def container_is_running(color: str) -> bool:
    result = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Running}}", color_container_name(color)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )

    return result.returncode == 0 and result.stdout.strip() == "true"


def collect_color_logs(color: str, tail: int = 180) -> str:
    result = subprocess.run(
        ["docker", "logs", "--tail", str(tail), color_container_name(color)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    return result.stdout or ""


def wait_for_color_ready(
    color: str,
    config: dict[str, Any],
    timeout_seconds: int = 45,
) -> None:
    """
    Verify the inactive color is accepting traffic before switching edge traffic.

    This intentionally does not require upstream backends to be alive.
    A new route may point to backend 3000/6000 that is not running yet.

    If readiness fails, this raises BlueGreenReadinessError.
    LangGraph should route that error to runtime_agent.py.
    """
    health_port = color_health_port(color, config)
    deadline = time.time() + timeout_seconds

    health_url = f"http://127.0.0.1:{health_port}/__pingora_health"
    fallback_url = f"http://127.0.0.1:{health_port}/__edge_probe"

    last_health = None
    last_fallback = None
    last_tcp = False
    last_running = False

    while time.time() < deadline:
        last_running = container_is_running(color)
        last_tcp = tcp_port_open("127.0.0.1", health_port)

        if last_running and last_tcp:
            last_health = http_status(health_url)

            # Accept any non-5xx health response.
            # 404 is okay when generated Pingora has no explicit health route.
            if last_health is not None and last_health < 500:
                return

            last_fallback = http_status(fallback_url)

            # Accept a 404/403/etc. from an unknown path as proof HTTP is serving.
            if last_fallback is not None and last_fallback < 500:
                return

        time.sleep(1)

    logs = collect_color_logs(color, tail=180)

    message = (
        f"{color} stack did not become ready.\n"
        f"Container running: {last_running}\n"
        f"TCP open on 127.0.0.1:{health_port}: {last_tcp}\n"
        f"Health URL: {health_url}, last status: {last_health}\n"
        f"Fallback URL: {fallback_url}, last status: {last_fallback}\n\n"
        f"Container logs:\n{logs}"
    )

    raise BlueGreenReadinessError(message)


def load_color_config(color: str) -> dict[str, Any]:
    path = color_dir(color) / "config.json"

    if not path.exists():
        raise_with_debug_agent(
            stage="rollback config load",
            message=f"No config found for {color}: {path}",
            cwd=color_dir(color),
        )

    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def deploy_inactive_color(config: dict[str, Any]) -> str:
    """
    Build/start/verify the inactive color only.

    This function never stops the active color.

    Build/generation errors go to Debug Agent.
    Runtime readiness errors are raised as BlueGreenReadinessError so LangGraph
    can route them to Runtime Agent.
    """
    color = get_inactive_color()

    try:
        ensure_edge_network()
        prepare_color_workspace(color, config)
        build_color(color)
        start_color(color)
        wait_for_color_ready(color, config)
        return color
    except Exception as exc:
        mark_failed(color, str(exc))
        raise