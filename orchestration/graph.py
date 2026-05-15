from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

from langgraph.graph import END, START, StateGraph
from langsmith import traceable

from ai.ai_config import prompt_to_config
from agents.config_repair_agent import repair_config
from agents.security_agent import enforce_security
from core.bluegreen_deployer import deploy_config_bluegreen
from core.compose_writer import write_compose_files
from core.docker_writer import write_docker_files
from core.logger import log_run
from core.preflight import preflight_check, preflight_check_config
from core.project_writer import write_project
from core.runner import cargo_check
from core.validator import validate_config
from orchestration.state import GraphState


LOCAL_RUNTIME_ALIASES = {
    "local",
    "host",
    "host_local",
    "local_host",
    "native",
}

DOCKER_RUNTIME_ALIASES = {
    "docker",
    "docker_host",
    "dockerhost",
    "compose",
    "bluegreen",
    "blue_green",
}

STRATEGY_ALIASES = {
    "direct": "direct",
    "simple": "direct",
    "dev": "direct",
    "local": "direct",
    "bluegreen": "bluegreen",
    "blue_green": "bluegreen",
    "blue-green": "bluegreen",
    "bg": "bluegreen",
    "rolling": "rolling",
    "canary": "canary",
}


def normalize_runtime(value: Any = None) -> str:
    text = str(value or "local").strip().lower()
    text = text.replace("-", "_").replace(" ", "_")

    if text in LOCAL_RUNTIME_ALIASES:
        return "local"

    if text in DOCKER_RUNTIME_ALIASES:
        return "docker_host"

    if text in {"k8s", "kubernetes"}:
        return "kubernetes"

    if text == "ecs":
        return "ecs"

    if text == "nomad":
        return "nomad"

    if text == "vm":
        return "vm"

    if text in {"baremetal", "bare_metal"}:
        return "bare_metal"

    return text


def normalize_strategy(value: Any = None) -> str:
    text = str(value or "direct").strip().lower()
    text = text.replace(" ", "_")

    return STRATEGY_ALIASES.get(text, "direct")


def infer_balancing_from_prompt(prompt: str | None) -> str | None:
    text = str(prompt or "").lower()
    text = text.replace("_", " ").replace("-", " ")

    if "weighted round robin" in text:
        return "weighted_round_robin"

    if "least connections" in text or "least connection" in text:
        return "least_connections"

    if "random" in text:
        return "random"

    if "round robin" in text:
        return "round_robin"

    if "ip hash" in text or "sticky" in text:
        return "ip_hash"

    return None


def extract_upstream_addresses_from_text(text: str) -> list[str]:
    addresses: list[str] = []

    pattern = re.compile(
        r"(?:(?:https?://)?(?:localhost|127\.0\.0\.1|0\.0\.0\.0|host\.docker\.internal|[a-zA-Z0-9_.-]+):\d{2,5})"
    )

    for match in pattern.findall(text or ""):
        address = match.strip()
        address = address.replace("http://", "").replace("https://", "").rstrip("/")

        if address.startswith("localhost:"):
            address = "127.0.0.1:" + address.split(":", 1)[1]

        if address not in addresses:
            addresses.append(address)

    return addresses


def extract_weighted_upstreams_from_text(text: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []

    pattern = re.compile(
        r"(?P<address>(?:https?://)?(?:localhost|127\.0\.0\.1|0\.0\.0\.0|host\.docker\.internal|[a-zA-Z0-9_.-]+):\d{2,5})"
        r"(?:\s+(?:weight|weighted|w)\s+(?P<weight>\d+))?",
        flags=re.IGNORECASE,
    )

    seen: set[str] = set()

    for match in pattern.finditer(text or ""):
        address = match.group("address").strip()
        address = address.replace("http://", "").replace("https://", "").rstrip("/")

        if address.startswith("localhost:"):
            address = "127.0.0.1:" + address.split(":", 1)[1]

        if address in seen:
            continue

        seen.add(address)

        try:
            weight = int(match.group("weight") or 1)
        except Exception:
            weight = 1

        if weight < 1:
            weight = 1

        items.append(
            {
                "address": address,
                "weight": weight,
            }
        )

    return items


def infer_balanced_path_from_text(text: str) -> str | None:
    segment = str(text or "").strip()

    patterns = [
        r"(?:load\s+balance|load[- ]?balance|balance)\s+(/[a-zA-Z0-9/_\-.]*)\s+(?:across|between|over)",
        r"(?:with|route|path|for)\s+(/[a-zA-Z0-9/_\-.]*)\s+(?:balanced|load\s+balanced|load\s+balance|using|across)",
        r"(^|\s)(/[a-zA-Z0-9/_\-.]*)\s+(?:balanced|load\s+balanced|load\s+balance|using)\b",
    ]

    for pattern in patterns:
        match = re.search(pattern, segment, flags=re.IGNORECASE)

        if not match:
            continue

        path = match.group(match.lastindex or 1)

        if not path.startswith("/"):
            continue

        if len(path) > 1:
            path = path.rstrip("/")

        return path

    return None


def split_prompt_into_route_segments(prompt: str | None) -> list[str]:
    text = str(prompt or "")
    parts = re.split(r"[;\n]+", text)

    clean_parts = [
        part.strip()
        for part in parts
        if part.strip()
    ]

    return clean_parts or [text]


def _segment_has_load_balancer_intent(segment: str) -> bool:
    lowered = segment.lower()
    normalized = lowered.replace("-", " ").replace("_", " ")

    if "load balance" in normalized:
        return True

    if "load balanced" in normalized:
        return True

    if "balanced" in normalized:
        return True

    if "using weighted round robin" in normalized:
        return True

    if "using round robin" in normalized:
        return True

    if "using random" in normalized:
        return True

    if "using least connection" in normalized or "using least connections" in normalized:
        return True

    if "using ip hash" in normalized or "using sticky" in normalized:
        return True

    return False


def apply_prompt_route_hints(
    config: dict[str, Any],
    prompt: str | None,
) -> dict[str, Any]:
    """
    Deterministic safety net when the LLM or agents drop multi-upstream routes
    or strip weighted upstream objects.

    Supports:
      /api balanced across backends 9101, 9102, 9103 using random
      load balance /api across 9101, 9102, 9103 using random
      / using weighted round robin across 9101 weight 5, 9102 weight 2
    """

    fixed = copy.deepcopy(config)

    routes = fixed.get("routes")

    if not isinstance(routes, list):
        fixed["routes"] = []
        routes = fixed["routes"]

    segments = split_prompt_into_route_segments(prompt)

    for segment in segments:
        if not _segment_has_load_balancer_intent(segment):
            continue

        addresses = extract_upstream_addresses_from_text(segment)

        if len(addresses) <= 1:
            continue

        path = infer_balanced_path_from_text(segment)

        if not path:
            continue

        balancing = (
            infer_balancing_from_prompt(segment)
            or infer_balancing_from_prompt(prompt)
            or "round_robin"
        )

        if balancing == "weighted_round_robin":
            weighted_upstreams = extract_weighted_upstreams_from_text(segment)

            if len(weighted_upstreams) <= 1:
                weighted_upstreams = [
                    {
                        "address": address,
                        "weight": 1,
                    }
                    for address in addresses
                ]

            upstreams_value: list[Any] = weighted_upstreams
            first_address = weighted_upstreams[0]["address"]
        else:
            upstreams_value = addresses
            first_address = addresses[0]

        existing_route = None

        for route in routes:
            if not isinstance(route, dict):
                continue

            route_path = str(route.get("path") or route.get("prefix") or "/").strip()

            if len(route_path) > 1:
                route_path = route_path.rstrip("/")

            if route_path == path:
                existing_route = route
                break

        if existing_route is None:
            existing_route = {
                "path": path,
            }
            routes.append(existing_route)

        existing_route["path"] = path
        existing_route["upstream"] = first_address
        existing_route["backend"] = first_address
        existing_route["upstreams"] = upstreams_value
        existing_route["balancing"] = balancing

        existing_route.pop("type", None)
        existing_route.pop("root", None)
        existing_route.pop("index", None)
        existing_route.pop("algorithm", None)
        existing_route.pop("lb_algorithm", None)
        existing_route.pop("load_balancing", None)
        existing_route.pop("backends", None)
        existing_route.pop("backend_upstreams", None)
        existing_route.pop("target", None)
        existing_route.pop("url", None)

    return fixed


def apply_prompt_balancing_hint(
    config: dict[str, Any],
    prompt: str | None,
) -> dict[str, Any]:
    prompt_balancing = infer_balancing_from_prompt(prompt)

    if not prompt_balancing:
        return config

    fixed = copy.deepcopy(config)

    for route in fixed.get("routes", []) or []:
        if not isinstance(route, dict):
            continue

        if route.get("type") == "static":
            continue

        upstreams = route.get("upstreams")
        backends = route.get("backends")

        upstream_count = 1

        if isinstance(upstreams, list):
            upstream_count = max(upstream_count, len(upstreams))

        if isinstance(backends, list):
            upstream_count = max(upstream_count, len(backends))

        if upstream_count > 1:
            route["balancing"] = prompt_balancing

    return fixed


def lock_prompt_route_intent(
    config: dict[str, Any],
    prompt: str | None,
) -> dict[str, Any]:
    fixed = apply_prompt_route_hints(config, prompt)
    fixed = apply_prompt_balancing_hint(fixed, prompt)
    return fixed


def _runtime_mode_from_state(state: GraphState) -> str:
    return normalize_runtime(
        state.get("runtime_mode")
        or state.get("runtime")
        or "local"
    )


def _strategy_from_state(state: GraphState) -> str:
    return normalize_strategy(
        state.get("strategy")
        or state.get("deployment_strategy")
        or "direct"
    )


def _call_writer(fn, config: dict[str, Any], project_dir: Path) -> None:
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


def _call_preflight_check(*, use_docker: bool, use_compose: bool) -> None:
    try:
        preflight_check(
            use_docker=use_docker,
            use_compose=use_compose,
        )
        return
    except TypeError:
        pass

    preflight_check()


def _call_config_preflight(
    config: dict[str, Any],
    *,
    use_docker: bool,
    use_compose: bool,
) -> None:
    try:
        preflight_check_config(
            config,
            use_docker=use_docker,
            use_compose=use_compose,
        )
        return
    except TypeError:
        pass

    preflight_check_config(config)


def _route_items(config: dict[str, Any]) -> list[dict[str, Any]]:
    routes = config.get("routes")
    return routes if isinstance(routes, list) else []


def _rewrite_address_for_runtime(
    address: Any,
    runtime_mode: str,
) -> tuple[str, str | None]:
    original = str(address or "").strip()

    if not original:
        return original, None

    if runtime_mode == "local":
        return original, None

    rewritten = original

    for prefix in ("http://", "https://"):
        if rewritten.startswith(prefix):
            rewritten = rewritten[len(prefix):]

    rewritten = rewritten.rstrip("/")

    if rewritten.startswith("localhost:"):
        rewritten = "host.docker.internal:" + rewritten.split(":", 1)[1]

    if rewritten.startswith("127.0.0.1:"):
        rewritten = "host.docker.internal:" + rewritten.split(":", 1)[1]

    if rewritten.startswith("0.0.0.0:"):
        rewritten = "host.docker.internal:" + rewritten.split(":", 1)[1]

    if rewritten != original:
        return rewritten, f"{original} -> {rewritten}"

    return rewritten, None


def _rewrite_upstream_value_for_runtime(
    value: Any,
    runtime_mode: str,
) -> tuple[Any, str | None]:
    if isinstance(value, dict):
        fixed = copy.deepcopy(value)

        address_key = None

        for key in ("address", "upstream", "backend", "target", "url"):
            if fixed.get(key):
                address_key = key
                break

        if address_key is None:
            return fixed, None

        rewritten, note = _rewrite_address_for_runtime(
            fixed[address_key],
            runtime_mode,
        )
        fixed[address_key] = rewritten

        return fixed, note

    return _rewrite_address_for_runtime(value, runtime_mode)


def apply_runtime_addressing(
    config: dict[str, Any],
    runtime_mode: str,
) -> tuple[dict[str, Any], list[str]]:
    runtime_mode = normalize_runtime(runtime_mode)
    fixed = copy.deepcopy(config)
    fixed["runtime_mode"] = runtime_mode

    rewrites: list[str] = []

    for route in _route_items(fixed):
        if not isinstance(route, dict):
            continue

        if route.get("type") == "static":
            continue

        path = str(
            route.get("path")
            or route.get("prefix")
            or route.get("route")
            or "/"
        ).strip()

        if not path.startswith("/"):
            path = "/" + path

        for key in ("upstream", "backend"):
            if key in route:
                new_value, note = _rewrite_upstream_value_for_runtime(
                    route.get(key),
                    runtime_mode,
                )
                route[key] = new_value

                if note:
                    rewrites.append(f"{path}: {note}")

        for key in ("upstreams", "backends"):
            if isinstance(route.get(key), list):
                new_items = []

                for item in route[key]:
                    new_item, note = _rewrite_upstream_value_for_runtime(
                        item,
                        runtime_mode,
                    )
                    new_items.append(new_item)

                    if note:
                        rewrites.append(f"{path}: {note}")

                route[key] = new_items

    return fixed, rewrites


def write_locked_config(project_dir: Path, config: dict[str, Any]) -> None:
    config_path = project_dir / "config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)

    with config_path.open("w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)


@traceable(name="main_preflight_node", run_type="tool")
def preflight_node(state: GraphState) -> dict:
    runtime = normalize_runtime(state.get("runtime") or "local")
    runtime_mode = _runtime_mode_from_state(state)
    strategy = _strategy_from_state(state)

    use_docker = strategy == "bluegreen" or runtime_mode == "docker_host"
    use_compose = strategy == "bluegreen"

    if strategy == "direct" and runtime_mode == "local":
        use_docker = False
        use_compose = False

    print("")
    print(f"Runtime selected: {runtime}")
    print(f"Runtime mode: {runtime_mode}")
    print(f"Deployment strategy: {strategy}")

    _call_preflight_check(
        use_docker=use_docker,
        use_compose=use_compose,
    )

    return {
        "runtime": runtime,
        "runtime_mode": runtime_mode,
        "strategy": strategy,
        "deployment_strategy": strategy,
        "use_docker": use_docker,
        "use_docker_compose": use_compose,
        "use_predeploy_sandbox": False,
        "last_node": "preflight",
    }


@traceable(name="main_prompt_to_config_node", run_type="chain")
def prompt_to_config_node(state: GraphState) -> dict:
    prompt = state["prompt"]
    raw_config = prompt_to_config(prompt)

    print("")
    print("AI understood your request as this config:")
    print(json.dumps(raw_config, indent=2))

    return {
        "raw_config": raw_config,
        "last_node": "prompt_to_config",
    }


@traceable(name="main_config_repair_node", run_type="chain")
def config_repair_node(state: GraphState) -> dict:
    raw_config = state["raw_config"]
    prompt = state.get("prompt")

    print("")
    print("Repairing config before validation...")

    repaired_config = repair_config(
        raw_config,
        prompt=prompt,
    )

    repaired_config = lock_prompt_route_intent(
        repaired_config,
        prompt,
    )

    print("✅ Repaired config:")
    print(json.dumps(repaired_config, indent=2))

    return {
        "repaired_config": repaired_config,
        "last_node": "config_repair",
    }


@traceable(name="main_validation_node", run_type="tool")
def validation_node(state: GraphState) -> dict:
    prompt = state.get("prompt")

    config = lock_prompt_route_intent(
        state["repaired_config"],
        prompt,
    )

    config = validate_config(config)

    config = lock_prompt_route_intent(
        config,
        prompt,
    )

    return {
        "config": config,
        "validation_ok": True,
        "last_node": "validation",
    }


@traceable(name="main_security_node", run_type="chain")
def security_node(state: GraphState) -> dict:
    prompt = state.get("prompt")

    config = enforce_security(
        state["config"],
        prompt=prompt,
    )

    config = lock_prompt_route_intent(
        config,
        prompt,
    )

    print("")
    print("Final security config:")
    print(json.dumps(config.get("security", {}), indent=2))

    return {
        "config": config,
        "security_ok": True,
        "last_node": "security",
    }


@traceable(name="main_runtime_addressing_node", run_type="tool")
def runtime_addressing_node(state: GraphState) -> dict:
    runtime = normalize_runtime(state.get("runtime") or "local")
    runtime_mode = _runtime_mode_from_state(state)
    strategy = _strategy_from_state(state)
    prompt = state.get("prompt")

    source_config = lock_prompt_route_intent(
        state["config"],
        prompt,
    )

    config, rewrites = apply_runtime_addressing(
        source_config,
        runtime_mode,
    )

    config = lock_prompt_route_intent(
        config,
        prompt,
    )

    config["runtime"] = runtime
    config["runtime_mode"] = runtime_mode
    config["strategy"] = strategy
    config["deployment_strategy"] = strategy

    print("")
    print("🌐 Runtime addressing resolved")

    if runtime_mode == "local":
        print("Runtime addressing: mode=local, no upstream rewrites.")
    else:
        print("Runtime addressing: mode=docker_host")

        if rewrites:
            for item in rewrites:
                print(f"- {item}")
        else:
            print("- no upstream rewrites needed")

    return {
        "config": config,
        "runtime": runtime,
        "runtime_mode": runtime_mode,
        "strategy": strategy,
        "deployment_strategy": strategy,
        "runtime_addressing_ok": True,
        "runtime_addressing_rewrites": rewrites,
        "last_node": "runtime_addressing",
    }


@traceable(name="main_config_preflight_node", run_type="tool")
def config_preflight_node(state: GraphState) -> dict:
    runtime_mode = _runtime_mode_from_state(state)
    strategy = _strategy_from_state(state)

    if strategy == "direct" and runtime_mode == "local":
        print("")
        print("✅ Config-level Docker/Compose preflight skipped for local direct runtime.")
        print("Local runtime agent will handle existing local gateway processes and port conflicts.")

        return {
            "config_preflight_ok": True,
            "last_node": "config_preflight",
        }

    _call_config_preflight(
        state["config"],
        use_docker=True,
        use_compose=strategy == "bluegreen",
    )

    return {
        "config_preflight_ok": True,
        "last_node": "config_preflight",
    }


@traceable(name="main_project_writer_node", run_type="tool")
def project_writer_node(state: GraphState) -> dict:
    project_dir = Path(state["project_dir"])
    prompt = state.get("prompt")

    config = lock_prompt_route_intent(
        state["config"],
        prompt,
    )

    _call_writer(write_project, config, project_dir)

    write_locked_config(project_dir, config)

    print("")
    print("✅ Project generated successfully")
    print(f"Folder: {project_dir}")

    return {
        "config": config,
        "project_written": True,
        "last_node": "project_writer",
    }


@traceable(name="main_container_files_node", run_type="tool")
def container_files_node(state: GraphState) -> dict:
    runtime_mode = _runtime_mode_from_state(state)
    strategy = _strategy_from_state(state)
    project_dir = Path(state["project_dir"])
    prompt = state.get("prompt")

    config = lock_prompt_route_intent(
        state["config"],
        prompt,
    )

    if strategy == "direct" and runtime_mode == "local":
        print("")
        print("✅ Docker/Compose file generation skipped for local direct runtime.")

        return {
            "config": config,
            "container_files_written": False,
            "dockerfile_generated": False,
            "compose_files_generated": False,
            "last_node": "container_files",
        }

    _call_writer(write_docker_files, config, project_dir)
    _call_writer(write_compose_files, config, project_dir)

    write_locked_config(project_dir, config)

    print("")
    print("✅ Docker/Compose files generated")

    return {
        "config": config,
        "container_files_written": True,
        "dockerfile_generated": True,
        "compose_files_generated": True,
        "last_node": "container_files",
    }


@traceable(name="main_cargo_check_node", run_type="tool")
def cargo_check_node(state: GraphState) -> dict:
    prompt = state["prompt"]
    project_dir = Path(state["project_dir"])

    config = lock_prompt_route_intent(
        state["config"],
        prompt,
    )

    cargo_ok = cargo_check(prompt, config)

    if not cargo_ok:
        raise RuntimeError("cargo_check_failure: Cargo check failed after debug attempts.")

    write_locked_config(project_dir, config)

    return {
        "config": config,
        "cargo_ok": True,
        "last_node": "cargo_check",
    }


@traceable(name="main_local_runtime_node", run_type="tool")
def local_runtime_node(state: GraphState) -> dict:
    config = state["config"]
    project_dir = Path(state["project_dir"])
    port = int(
        config.get("port")
        or config.get("listen_port")
        or config.get("proxy_port")
        or 8088
    )

    try:
        from agents.runtime_agent import run_local_gateway
    except ImportError as exc:
        raise RuntimeError(
            "local_runtime_startup_failure: agents.runtime_agent.run_local_gateway "
            "is missing."
        ) from exc

    result = run_local_gateway(
        project_dir=project_dir,
        port=port,
        startup_timeout_seconds=90,
        stop_existing=True,
    )

    if isinstance(result, dict):
        success = bool(
            result.get("success")
            or result.get("ok")
            or result.get("runtime_ok")
        )
        live_url = result.get("live_url") or f"http://127.0.0.1:{port}"
        error = result.get("error") or result.get("message") or result.get("reason")
        warning = result.get("warning")
        classification = result.get("classification")
        health_ok = bool(result.get("health_ok"))
        readiness_ok = bool(result.get("readiness_ok", True))
    else:
        success = bool(result)
        live_url = f"http://127.0.0.1:{port}"
        error = None
        warning = None
        classification = None
        health_ok = success
        readiness_ok = success

    if not success:
        raise RuntimeError(
            f"{classification or 'local_runtime_startup_failure'}: "
            + str(error or "Local gateway did not become ready.")
        )

    final_message = (
        "✅ Local gateway is running\n"
        f"Live URL: {live_url}"
    )

    if warning:
        final_message += f"\n⚠️ {warning}"

    return {
        "runtime_ok": True,
        "health_ok": health_ok,
        "readiness_ok": readiness_ok,
        "runtime_classification": classification,
        "live_url": live_url,
        "final_message": final_message,
        "last_node": "local_runtime",
    }


@traceable(name="main_bluegreen_deploy_node", run_type="chain")
def bluegreen_deploy_node(state: GraphState) -> dict:
    strategy = _strategy_from_state(state)

    if strategy != "bluegreen":
        raise RuntimeError(
            "BUG: bluegreen_deploy_node reached while deployment_strategy is not bluegreen."
        )

    config = state["config"]
    result = deploy_config_bluegreen(config)

    live_url = result.get("live_url")
    active_color = result.get("active_color")

    return {
        "compose_ok": True,
        "predeploy_ok": True,
        "deploy_ok": True,
        "traffic_switched": bool(result.get("traffic_switched", True)),
        "active_color": active_color,
        "live_url": live_url,
        "deployment_result": result,
        "final_message": (
            "✅ Blue/green deployment completed.\n"
            f"Active color: {active_color}\n"
            f"Live URL: {live_url}"
        ),
        "last_node": "bluegreen_deploy",
    }


@traceable(name="main_final_report_node", run_type="tool")
def final_report_node(state: GraphState) -> dict:
    final_message = state.get("final_message")

    if final_message:
        print("")
        print(final_message)

    return {
        "last_node": "final_report",
    }


def route_after_project_writer(state: GraphState) -> str:
    strategy = _strategy_from_state(state)
    runtime_mode = _runtime_mode_from_state(state)

    if strategy == "bluegreen":
        return "container_files"

    if runtime_mode == "local":
        return "cargo_check"

    return "container_files"


def route_after_cargo_check(state: GraphState) -> str:
    strategy = _strategy_from_state(state)
    runtime_mode = _runtime_mode_from_state(state)

    if strategy == "bluegreen":
        return "bluegreen_deploy"

    if runtime_mode == "local":
        return "local_runtime"

    return "bluegreen_deploy"


def build_graph():
    graph = StateGraph(GraphState)

    graph.add_node("preflight", preflight_node)
    graph.add_node("prompt_to_config", prompt_to_config_node)
    graph.add_node("config_repair", config_repair_node)
    graph.add_node("validation", validation_node)
    graph.add_node("security", security_node)
    graph.add_node("runtime_addressing", runtime_addressing_node)
    graph.add_node("config_preflight", config_preflight_node)
    graph.add_node("project_writer", project_writer_node)
    graph.add_node("container_files", container_files_node)
    graph.add_node("cargo_check", cargo_check_node)
    graph.add_node("local_runtime", local_runtime_node)
    graph.add_node("bluegreen_deploy", bluegreen_deploy_node)
    graph.add_node("final_report", final_report_node)

    graph.add_edge(START, "preflight")
    graph.add_edge("preflight", "prompt_to_config")
    graph.add_edge("prompt_to_config", "config_repair")
    graph.add_edge("config_repair", "validation")
    graph.add_edge("validation", "security")
    graph.add_edge("security", "runtime_addressing")
    graph.add_edge("runtime_addressing", "config_preflight")
    graph.add_edge("config_preflight", "project_writer")

    graph.add_conditional_edges(
        "project_writer",
        route_after_project_writer,
        {
            "cargo_check": "cargo_check",
            "container_files": "container_files",
        },
    )

    graph.add_edge("container_files", "cargo_check")

    graph.add_conditional_edges(
        "cargo_check",
        route_after_cargo_check,
        {
            "local_runtime": "local_runtime",
            "bluegreen_deploy": "bluegreen_deploy",
        },
    )

    graph.add_edge("local_runtime", "final_report")
    graph.add_edge("bluegreen_deploy", "final_report")
    graph.add_edge("final_report", END)

    return graph.compile()


@traceable(name="main_create_gateway_flow", run_type="chain")
def run_graph(
    prompt: str,
    project_root,
    project_dir,
    use_docker: bool = False,
    use_docker_compose: bool = False,
    use_predeploy_sandbox: bool = False,
    runtime: str | None = "local",
    runtime_mode: str | None = None,
    strategy: str | None = "direct",
    deployment_strategy: str | None = None,
):
    requested_runtime = normalize_runtime(runtime or "local")
    selected_runtime_mode = normalize_runtime(runtime_mode or requested_runtime)
    selected_strategy = normalize_strategy(deployment_strategy or strategy or "direct")

    if selected_strategy == "bluegreen":
        use_docker = True
        use_docker_compose = True
        use_predeploy_sandbox = False
    elif selected_runtime_mode == "local":
        use_docker = False
        use_docker_compose = False
        use_predeploy_sandbox = False

    app = build_graph()

    initial_state: GraphState = {
        "prompt": prompt,
        "project_root": str(project_root),
        "project_dir": str(project_dir),

        "runtime": requested_runtime,
        "runtime_mode": selected_runtime_mode,

        "strategy": selected_strategy,
        "deployment_strategy": selected_strategy,

        "use_docker": use_docker,
        "use_docker_compose": use_docker_compose,
        "use_predeploy_sandbox": use_predeploy_sandbox,

        "cargo_ok": False,
        "docker_ok": False,
        "compose_ok": False,
        "predeploy_ok": False,
        "runtime_ok": False,
        "health_ok": False,
        "deploy_ok": False,
        "traffic_switched": False,
        "error": None,
        "failed_node": None,
        "final_message": None,
    }

    try:
        result = app.invoke(initial_state)

        try:
            log_run(
                prompt=prompt,
                config=result.get("config"),
                success=True,
                error=None,
            )
        except Exception:
            pass

        return result

    except Exception as exc:
        print("")
        print("❌ I could not build this yet.")
        print(exc)

        try:
            log_run(
                prompt=prompt,
                config=None,
                success=False,
                error=str(exc),
            )
        except Exception:
            pass

        return {
            **initial_state,
            "error": str(exc),
            "failed_node": initial_state.get("last_node"),
        }