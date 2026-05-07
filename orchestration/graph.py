from __future__ import annotations

import json
from pathlib import Path

from langgraph.graph import END, START, StateGraph
from langsmith import traceable

from ai.ai_config import prompt_to_config

from agents.config_repair_agent import repair_config
from agents.security_agent import enforce_security

from core.validator import validate_config
from core.project_writer import write_project
from core.runner import cargo_check
from core.logger import log_run
from core.docker_writer import write_docker_files
from core.compose_writer import write_compose_files
from core.preflight import preflight_check, preflight_check_config
from core.bluegreen_deployer import deploy_config_bluegreen

from orchestration.state import GraphState


@traceable(name="main_preflight_node", run_type="tool")
def preflight_node(state: GraphState) -> dict:
    print("")

    preflight_check(
        use_docker=state.get("use_docker", False),
        use_compose=state.get("use_docker_compose", False)
        or state.get("use_predeploy_sandbox", False),
    )

    return {
        "last_node": "preflight",
    }


@traceable(name="main_prompt_to_config_node", run_type="chain")
def prompt_to_config_node(state: GraphState) -> dict:
    prompt = state["prompt"]

    raw_config = prompt_to_config(prompt)

    print("")
    print("🧠 AI understood your request as this config:")
    print(json.dumps(raw_config, indent=2))

    return {
        "raw_config": raw_config,
        "last_node": "prompt_to_config",
    }


@traceable(name="main_config_repair_node", run_type="chain")
def config_repair_node(state: GraphState) -> dict:
    raw_config = state["raw_config"]

    print("")
    print("🛠 Repairing config before validation...")

    repaired_config = repair_config(raw_config)

    print("✅ Repaired config:")
    print(json.dumps(repaired_config, indent=2))

    return {
        "repaired_config": repaired_config,
        "last_node": "config_repair",
    }


@traceable(name="main_validation_node", run_type="tool")
def validation_node(state: GraphState) -> dict:
    repaired_config = state["repaired_config"]

    config = validate_config(repaired_config)

    return {
        "config": config,
        "validation_ok": True,
        "last_node": "validation",
    }


@traceable(name="main_security_node", run_type="chain")
def security_node(state: GraphState) -> dict:
    config = enforce_security(
        state["config"],
        prompt=state.get("prompt"),
    )

    print("")
    print("🔐 Final security config:")
    print(json.dumps(config.get("security", {}), indent=2))

    return {
        "config": config,
        "security_ok": True,
        "last_node": "security",
    }


@traceable(name="main_config_preflight_node", run_type="tool")
def config_preflight_node(state: GraphState) -> dict:
    config = state["config"]

    preflight_check_config(
        config,
        use_docker=state.get("use_docker", False),
        use_compose=state.get("use_docker_compose", False)
        or state.get("use_predeploy_sandbox", False),
    )

    return {
        "config_preflight_ok": True,
        "last_node": "config_preflight",
    }


@traceable(name="main_project_writer_node", run_type="tool")
def project_writer_node(state: GraphState) -> dict:
    config = state["config"]

    write_project(config)

    return {
        "project_written": True,
        "last_node": "project_writer",
    }


@traceable(name="main_container_files_node", run_type="tool")
def container_files_node(state: GraphState) -> dict:
    config = state["config"]
    project_dir = Path(state["project_dir"])

    if state.get("use_docker_compose", False) or state.get("use_predeploy_sandbox", False):
        write_compose_files(config)
    else:
        write_docker_files(config)

    print("")
    print("✅ Project generated successfully")
    print(f"📁 Folder: {project_dir}")

    return {
        "container_files_written": True,
        "last_node": "container_files",
    }


@traceable(name="main_cargo_check_node", run_type="tool")
def cargo_check_node(state: GraphState) -> dict:
    prompt = state["prompt"]
    config = state["config"]

    cargo_ok = cargo_check(prompt, config)

    if not cargo_ok:
        raise RuntimeError("Cargo check failed after debug attempts.")

    return {
        "cargo_ok": True,
        "last_node": "cargo_check",
    }


@traceable(name="main_bluegreen_deploy_node", run_type="chain")
def bluegreen_deploy_node(state: GraphState) -> dict:
    config = state["config"]

    result = deploy_config_bluegreen(config)

    live_url = result.get("live_url")
    active_color = result.get("active_color")

    return {
        "compose_ok": True,
        "predeploy_ok": True,
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


def build_graph():
    graph = StateGraph(GraphState)

    graph.add_node("preflight", preflight_node)
    graph.add_node("prompt_to_config", prompt_to_config_node)
    graph.add_node("config_repair", config_repair_node)
    graph.add_node("validation", validation_node)
    graph.add_node("security", security_node)
    graph.add_node("config_preflight", config_preflight_node)
    graph.add_node("project_writer", project_writer_node)
    graph.add_node("container_files", container_files_node)
    graph.add_node("cargo_check", cargo_check_node)
    graph.add_node("bluegreen_deploy", bluegreen_deploy_node)
    graph.add_node("final_report", final_report_node)

    graph.add_edge(START, "preflight")
    graph.add_edge("preflight", "prompt_to_config")
    graph.add_edge("prompt_to_config", "config_repair")
    graph.add_edge("config_repair", "validation")
    graph.add_edge("validation", "security")
    graph.add_edge("security", "config_preflight")
    graph.add_edge("config_preflight", "project_writer")
    graph.add_edge("project_writer", "container_files")
    graph.add_edge("container_files", "cargo_check")
    graph.add_edge("cargo_check", "bluegreen_deploy")
    graph.add_edge("bluegreen_deploy", "final_report")
    graph.add_edge("final_report", END)

    return graph.compile()


@traceable(name="main_create_gateway_flow", run_type="chain")
def run_graph(
    prompt: str,
    project_root,
    project_dir,
    use_docker: bool = False,
    use_docker_compose: bool = True,
    use_predeploy_sandbox: bool = True,
):
    app = build_graph()

    initial_state: GraphState = {
        "prompt": prompt,
        "project_root": str(project_root),
        "project_dir": str(project_dir),
        "use_docker": use_docker,
        "use_docker_compose": use_docker_compose,
        "use_predeploy_sandbox": use_predeploy_sandbox,
        "cargo_ok": False,
        "docker_ok": False,
        "compose_ok": False,
        "predeploy_ok": False,
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

    except Exception as e:
        print("")
        print("❌ I could not build this yet.")
        print(e)
        print("")
        print("Try something like:")
        print("create server on port 9000 and send traffic to backend 4000")
        print("or")
        print("send /api to backend 3000 and /admin to backend 5000")

        try:
            log_run(
                prompt=prompt,
                config=None,
                success=False,
                error=str(e),
            )
        except Exception:
            pass

        return {
            **initial_state,
            "error": str(e),
            "failed_node": initial_state.get("last_node"),
        }