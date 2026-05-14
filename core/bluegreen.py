from __future__ import annotations

import copy
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
    """
    Legacy fixed container name.

    New blue/green Compose files do not use container_name.
    This is kept only to clean up older containers.
    """

    return f"pingora-proxy-{color}"


def color_network_alias(color: str) -> str:
    return f"pingora-{color}"


def color_health_port(color: str, config: dict[str, Any]) -> int:
    """
    Derive a stable local-only health port from the user's selected public port.

    Example:
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


def run_best_effort(
    cmd: list[str],
    *,
    cwd: Path | None = None,
) -> str:
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


def write_color_compose(color: str, config: dict[str, Any]) -> Path:
    """
    Write the blue/green Compose file.

    Important:
    Do not use fixed container_name here.

    Why:
    Blue/green deployments need two independently named Compose stacks.
    Fixed container names can conflict between old blue/green containers
    and new deployments.
    """

    project = color_dir(color)
    project.mkdir(parents=True, exist_ok=True)

    internal_port = internal_port_from_config(config)
    health_port = color_health_port(color, config)

    dockerfile_name = "Dockerfile"

    if (project / "Dockerfile.proxy").exists():
        dockerfile_name = "Dockerfile.proxy"
    elif (project / "Dockerfile").exists():
        dockerfile_name = "Dockerfile"
    else:
        raise BlueGreenError(
            "No Dockerfile found for blue/green build.\n\n"
            f"Expected one of:\n"
            f"- {project / 'Dockerfile'}\n"
            f"- {project / 'Dockerfile.proxy'}"
        )

    compose = f"""services:
  pingora-proxy:
    build:
      context: .
      dockerfile: {dockerfile_name}
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

    This stops/removes the inactive color Compose project.
    It also removes old legacy fixed-name containers from earlier versions.
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

    # Legacy cleanup for older bluegreen.py versions that used fixed names.
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


def _normalize_upstream_value(value: Any) -> str | None:
    if value is None:
        return None

    if isinstance(value, dict):
        value = value.get("address") or value.get("upstream") or value.get("backend")

    if value is None:
        return None

    text = str(value).strip()

    if not text:
        return None

    text = text.replace("http://", "").replace("https://", "").rstrip("/")

    if "/" in text:
        text = text.split("/", 1)[0]

    return text if ":" in text else None


def _collect_expected_upstreams(config: dict[str, Any]) -> list[str]:
    expected: list[str] = []

    for route in config.get("routes", []) or []:
        if not isinstance(route, dict):
            continue

        values: list[Any] = []

        if route.get("upstream"):
            values.append(route.get("upstream"))

        if route.get("backend"):
            values.append(route.get("backend"))

        upstreams = route.get("upstreams")

        if isinstance(upstreams, list):
            values.extend(upstreams)

        backends = route.get("backends")

        if isinstance(backends, list):
            values.extend(backends)

        for value in values:
            normalized = _normalize_upstream_value(value)

            if normalized and normalized not in expected:
                expected.append(normalized)

    return expected


def _read_main_rs(project_dir: Path) -> str:
    path = project_dir / "src" / "main.rs"

    if not path.exists():
        return ""

    return path.read_text(encoding="utf-8")


def _assert_main_rs_preserves_upstreams(
    *,
    config: dict[str, Any],
    project_dir: Path,
    stage: str,
) -> None:
    """
    Prevent silent collapse of load-balanced routes.

    If config contains upstreams 9101/9102/9103, generated src/main.rs must
    contain all three, not only the first one.
    """

    expected = _collect_expected_upstreams(config)

    if len(expected) <= 1:
        return

    main_rs = _read_main_rs(project_dir)

    missing = []

    for upstream in expected:
        if upstream not in main_rs:
            missing.append(upstream)

    if missing:
        raise_with_debug_agent(
            stage=stage,
            message=(
                "Generated Rust dropped one or more load-balancer upstreams.\n\n"
                f"Missing from src/main.rs:\n{json.dumps(missing, indent=2)}\n\n"
                f"Expected upstreams:\n{json.dumps(expected, indent=2)}\n\n"
                f"Project dir:\n{project_dir}\n\n"
                "This usually means a Docker/Compose helper rewrote only "
                "route['upstream'] and ignored route['upstreams']."
            ),
            cwd=project_dir,
        )


def render_project_to_workspace(config: dict[str, Any]) -> Path:
    """
    Generate the build workspace.

    generated-pingora-proxy is only a temporary build workspace.
    The live blue/green stacks are copied into generated-projects/default-project.

    Blue/green Compose builds with:

        build:
          context: .

    So this workspace must contain:
      - Dockerfile or Dockerfile.proxy
      - Cargo.toml
      - src/main.rs
      - config.json

    Production-safety rule:
    Docker/Compose helper writers must not collapse load-balanced upstreams.
    If a helper overwrites src/main.rs, this function restores the canonical
    Rust project afterward and verifies all upstreams survived.
    """

    from core.project_writer import write_project

    canonical_config = copy.deepcopy(config)

    if BUILD_WORKSPACE.exists():
        shutil.rmtree(BUILD_WORKSPACE)

    try:
        call_generator(write_project, copy.deepcopy(canonical_config), BUILD_WORKSPACE)
        _assert_main_rs_preserves_upstreams(
            config=canonical_config,
            project_dir=BUILD_WORKSPACE,
            stage="project generation",
        )
    except Exception as exc:
        raise_with_debug_agent(
            stage="project generation",
            message=(
                "project_writer failed while generating generated-pingora-proxy.\n\n"
                f"Expected workspace:\n{BUILD_WORKSPACE}\n\n"
                f"Error:\n{exc}\n\n"
                f"Config:\n{json.dumps(canonical_config, indent=2)}"
            ),
            cwd=BUILD_WORKSPACE,
        )

    try:
        from core.docker_writer import write_docker_files

        call_generator(write_docker_files, copy.deepcopy(canonical_config), BUILD_WORKSPACE)
        _assert_main_rs_preserves_upstreams(
            config=canonical_config,
            project_dir=BUILD_WORKSPACE,
            stage="docker file generation",
        )
    except ImportError as exc:
        raise_with_debug_agent(
            stage="docker file generation",
            message=(
                "Could not import core.docker_writer.write_docker_files.\n\n"
                "Blue/green deployment requires a Dockerfile in generated-pingora-proxy.\n\n"
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
                f"Config:\n{json.dumps(canonical_config, indent=2)}"
            ),
            cwd=BUILD_WORKSPACE,
        )

    try:
        from core.compose_writer import write_compose_files

        call_generator(write_compose_files, copy.deepcopy(canonical_config), BUILD_WORKSPACE)

        # Some compose writers regenerate project files as a side effect.
        # Restore canonical src/main.rs/config.json before verification.
        call_generator(write_project, copy.deepcopy(canonical_config), BUILD_WORKSPACE)

        _assert_main_rs_preserves_upstreams(
            config=canonical_config,
            project_dir=BUILD_WORKSPACE,
            stage="compose file generation",
        )

    except Exception as exc:
        debug_note = ask_debug_agent(
            stage="compose file generation",
            output=(
                "compose_writer failed while generating Compose files.\n\n"
                f"Error:\n{exc}\n\n"
                f"Expected compose file:\n{BUILD_WORKSPACE / 'docker-compose.yml'}\n\n"
                f"Config:\n{json.dumps(canonical_config, indent=2)}"
            ),
            cwd=BUILD_WORKSPACE,
        )

        message = (
            "compose_writer failed while generating Compose files.\n\n"
            f"Expected compose file:\n{BUILD_WORKSPACE / 'docker-compose.yml'}\n\n"
            f"Error:\n{exc}\n\n"
            f"Debug Agent:\n{debug_note or 'No debug note returned.'}\n\n"
            f"Config:\n{json.dumps(canonical_config, indent=2)}"
        )

        raise_with_debug_agent(
            stage="compose file generation",
            message=message,
            cwd=BUILD_WORKSPACE,
        )

    # Final restoration after all auxiliary Docker/Compose writers.
    try:
        call_generator(write_project, copy.deepcopy(canonical_config), BUILD_WORKSPACE)
        _assert_main_rs_preserves_upstreams(
            config=canonical_config,
            project_dir=BUILD_WORKSPACE,
            stage="post-compose project restoration",
        )
    except Exception as exc:
        raise_with_debug_agent(
            stage="post-compose project restoration",
            message=(
                "Failed to restore canonical generated Rust after Docker/Compose generation.\n\n"
                f"Error:\n{exc}\n\n"
                f"Config:\n{json.dumps(canonical_config, indent=2)}"
            ),
            cwd=BUILD_WORKSPACE,
        )

    if not BUILD_WORKSPACE.exists():
        raise_with_debug_agent(
            stage="project generation",
            message=f"Build workspace was not created: {BUILD_WORKSPACE}",
            cwd=PROJECT_ROOT,
        )

    dockerfile = BUILD_WORKSPACE / "Dockerfile"
    dockerfile_proxy = BUILD_WORKSPACE / "Dockerfile.proxy"

    if not dockerfile.exists() and not dockerfile_proxy.exists():
        raise_with_debug_agent(
            stage="docker file generation",
            message=(
                "No Dockerfile was created in generated-pingora-proxy.\n\n"
                f"Expected one of:\n"
                f"- {dockerfile}\n"
                f"- {dockerfile_proxy}\n\n"
                "This should be fixed by core/docker_writer.py. "
                "write_docker_files(config) must create a Dockerfile or Dockerfile.proxy "
                "in the generated project folder.\n\n"
                f"Config:\n{json.dumps(canonical_config, indent=2)}"
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
                f"Config:\n{json.dumps(canonical_config, indent=2)}"
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
                f"Config:\n{json.dumps(canonical_config, indent=2)}"
            ),
            cwd=BUILD_WORKSPACE,
        )

    _assert_main_rs_preserves_upstreams(
        config=canonical_config,
        project_dir=BUILD_WORKSPACE,
        stage="final bluegreen workspace validation",
    )

    config_path = BUILD_WORKSPACE / "config.json"

    with config_path.open("w", encoding="utf-8") as f:
        json.dump(canonical_config, f, indent=2)

    return BUILD_WORKSPACE


def prepare_color_workspace(color: str, config: dict[str, Any]) -> Path:
    """
    Prepare inactive color workspace.

    This only stops/removes the inactive color selected by deploy_inactive_color().
    """

    canonical_config = copy.deepcopy(config)

    stop_color(color)

    target = color_dir(color)

    if target.exists():
        shutil.rmtree(target)

    workspace = render_project_to_workspace(canonical_config)
    shutil.copytree(workspace, target)

    write_color_compose(color, canonical_config)

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


def color_proxy_container_id(color: str) -> str | None:
    """
    Resolve the current proxy container through Compose.

    This replaces fixed docker inspect by container_name.
    """

    compose_path = color_compose_path(color)

    if not compose_path.exists():
        return None

    result = subprocess.run(
        compose_cmd(color, "ps", "-q", "pingora-proxy"),
        cwd=str(compose_path.parent),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )

    container_id = result.stdout.strip()

    return container_id or None


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
        compose_cmd(color, "up", "-d", "--remove-orphans"),
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
    container_id = color_proxy_container_id(color)

    if not container_id:
        return False

    result = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Running}}", container_id],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )

    return result.returncode == 0 and result.stdout.strip() == "true"


def collect_color_logs(color: str, tail: int = 180) -> str:
    compose_path = color_compose_path(color)

    if compose_path.exists():
        result = subprocess.run(
            compose_cmd(color, "logs", "--tail", str(tail), "pingora-proxy"),
            cwd=str(compose_path.parent),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )

        if result.stdout:
            return result.stdout

    container_id = color_proxy_container_id(color)

    if container_id:
        result = subprocess.run(
            ["docker", "logs", "--tail", str(tail), container_id],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )

        return result.stdout or ""

    return ""


def collect_bluegreen_evidence(
    color: str,
    config: dict[str, Any],
    exc: Exception,
) -> str:
    """
    Collect concrete Docker/Compose evidence so the root-cause agent can classify
    the real failure instead of only bluegreen_or_compose_deployment_failure.
    """

    project = color_dir(color)
    compose_path = color_compose_path(color)

    health_port = None

    try:
        health_port = color_health_port(color, config)
    except Exception:
        pass

    parts: list[str] = []

    parts.append("BLUE/GREEN FAILURE EVIDENCE")
    parts.append("=" * 80)
    parts.append(f"Color: {color}")
    parts.append(f"Project dir: {project}")
    parts.append(f"Compose path: {compose_path}")
    parts.append(f"Exception type: {type(exc).__name__}")
    parts.append(f"Exception: {exc}")

    parts.append("\n--- docker-compose.bluegreen.yml ---")
    if compose_path.exists():
        try:
            parts.append(compose_path.read_text(encoding="utf-8"))
        except Exception as read_exc:
            parts.append(f"Could not read compose file: {read_exc}")
    else:
        parts.append("Compose file does not exist.")

    parts.append("\n--- docker compose config ---")
    if compose_path.exists():
        parts.append(
            run_best_effort(
                compose_cmd(color, "config"),
                cwd=project,
            )
        )
    else:
        parts.append("Skipped because compose file does not exist.")

    parts.append("\n--- docker compose ps ---")
    if compose_path.exists():
        parts.append(
            run_best_effort(
                compose_cmd(color, "ps", "-a"),
                cwd=project,
            )
        )
    else:
        parts.append("Skipped because compose file does not exist.")

    parts.append("\n--- docker compose logs --tail 180 ---")
    if compose_path.exists():
        parts.append(
            run_best_effort(
                compose_cmd(color, "logs", "--tail", "180"),
                cwd=project,
            )
        )
    else:
        parts.append("Skipped because compose file does not exist.")

    parts.append("\n--- docker ps -a ---")
    parts.append(run_best_effort(["docker", "ps", "-a"]))

    parts.append("\n--- docker network ls ---")
    parts.append(run_best_effort(["docker", "network", "ls"]))

    parts.append(f"\n--- docker network inspect {EDGE_NETWORK} ---")
    parts.append(run_best_effort(["docker", "network", "inspect", EDGE_NETWORK]))

    parts.append("\n--- listening ports 8088 / 18089 / 18090 ---")
    parts.append(
        run_best_effort(
            ["bash", "-lc", "ss -ltnp | grep -E '8088|18089|18090' || true"]
        )
    )

    if health_port is not None:
        parts.append(f"\n--- TCP probe 127.0.0.1:{health_port} ---")
        parts.append(str(tcp_port_open("127.0.0.1", health_port)))

        parts.append(f"\n--- HTTP probe http://127.0.0.1:{health_port}/ ---")
        parts.append(str(http_status(f"http://127.0.0.1:{health_port}/")))

        parts.append(
            f"\n--- HTTP probe http://127.0.0.1:{health_port}/__pingora_health ---"
        )
        parts.append(str(http_status(f"http://127.0.0.1:{health_port}/__pingora_health")))

    return "\n".join(parts)


def wait_for_color_ready(
    color: str,
    config: dict[str, Any],
    timeout_seconds: int = 45,
) -> None:
    """
    Verify the inactive color has started before switching traffic.

    Important:
    This readiness check must not require upstream application backends to be alive.

    Why:
    If "/" exists as a catch-all route, health probe URLs like /__pingora_health
    can be forwarded to the upstream backend. If that backend is down, Pingora may
    return 502 even though the gateway container itself is healthy.

    So the production-safe minimum here is:
    - container is running
    - mapped local health TCP port is open

    Full backend health checks should be added later as a separate feature.
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
            # Best-effort HTTP probe only. Do not fail readiness because a route
            # forwards the probe to a missing backend and returns 502.
            last_health = http_status(health_url)
            last_fallback = http_status(fallback_url)

            print(
                f"✅ {color} gateway container is running and TCP-ready "
                f"on 127.0.0.1:{health_port}"
            )

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

    Important:
    On failure, this raises an evidence-rich BlueGreenError so the root-cause
    agent receives the real Docker/Compose evidence instead of only the broad
    bluegreen_deploy stage.
    """

    color = get_inactive_color()
    canonical_config = copy.deepcopy(config)

    try:
        ensure_edge_network()
        prepare_color_workspace(color, canonical_config)
        build_color(color)
        start_color(color)
        wait_for_color_ready(color, canonical_config)
        return color

    except Exception as exc:
        evidence = collect_bluegreen_evidence(color, canonical_config, exc)

        evidence_file = PROJECT_ROOT / "bluegreen_failure_evidence.txt"
        evidence_file.write_text(evidence, encoding="utf-8")

        mark_failed(color, f"{exc}\n\n{evidence}")

        print("\n❌ BLUE/GREEN REAL ERROR")
        print("=" * 80)
        print(str(exc))
        print("=" * 80)
        print(evidence)
        print("=" * 80)
        print(f"\n📄 Evidence written to: {evidence_file}")

        raise BlueGreenError(
            f"Blue/green deployment failed for {color}.\n\n"
            f"Original error:\n{exc}\n\n"
            f"{evidence}\n\n"
            f"Evidence file:\n{evidence_file}"
        ) from exc