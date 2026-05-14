from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).parent.absolute()
PROJECT_DIR = PROJECT_ROOT / "generated-pingora-proxy"

os.chdir(PROJECT_ROOT)

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from orchestration.graph import run_graph


SUPPORTED_RUNTIMES = {
    "local",
    "docker",
    "docker_host",
    "kubernetes",
    "k8s",
    "ecs",
    "nomad",
    "vm",
    "bare_metal",
    "baremetal",
}

SUPPORTED_STRATEGIES = {
    "direct",
    "bluegreen",
    "rolling",
    "canary",
}


CURRENTLY_IMPLEMENTED = {
    ("local", "direct"),
    ("local", "bluegreen"),
    ("docker", "bluegreen"),
    ("docker_host", "bluegreen"),
}


def normalize_runtime(value: str | None) -> str:
    text = str(value or "local").strip().lower()
    text = text.replace("-", "_")

    aliases = {
        "host": "local",
        "native": "local",
        "local_host": "local",
        "dockerhost": "docker_host",
        "docker_host": "docker_host",
        "docker": "docker",
        "compose": "docker_host",
        "k8s": "kubernetes",
        "kube": "kubernetes",
        "kubernetes": "kubernetes",
        "ecs": "ecs",
        "nomad": "nomad",
        "vm": "vm",
        "baremetal": "bare_metal",
        "bare_metal": "bare_metal",
    }

    return aliases.get(text, text)


def normalize_strategy(value: str | None, runtime: str) -> str:
    text = str(value or "").strip().lower()
    text = text.replace("-", "").replace("_", "")

    aliases = {
        "direct": "direct",
        "dev": "direct",
        "simple": "direct",
        "bluegreen": "bluegreen",
        "bg": "bluegreen",
        "rolling": "rolling",
        "canary": "canary",
    }

    if text:
        return aliases.get(text, text)

    # Good defaults:
    # local create/test = direct
    # non-local deployment = bluegreen
    if runtime == "local":
        return "direct"

    return "bluegreen"


def effective_runtime_mode(runtime: str, strategy: str) -> str:
    """
    runtime = user's chosen environment
    runtime_mode = addressing mode used inside generated config

    Current blue/green deployer is Docker/Nginx based.
    So local + bluegreen must use docker_host addressing because the gateway
    runs inside a container and must reach host backends through host.docker.internal.
    """

    if runtime == "local" and strategy == "direct":
        return "local"

    if runtime in {"local", "docker", "docker_host"} and strategy == "bluegreen":
        return "docker_host"

    if runtime == "docker":
        return "docker_host"

    return runtime


def validate_runtime_strategy(runtime: str, strategy: str) -> None:
    if runtime not in SUPPORTED_RUNTIMES:
        allowed = ", ".join(sorted(SUPPORTED_RUNTIMES))
        raise SystemExit(f"❌ Unsupported runtime: {runtime}\nAllowed: {allowed}")

    if strategy not in SUPPORTED_STRATEGIES:
        allowed = ", ".join(sorted(SUPPORTED_STRATEGIES))
        raise SystemExit(f"❌ Unsupported strategy: {strategy}\nAllowed: {allowed}")

    if (runtime, strategy) not in CURRENTLY_IMPLEMENTED:
        print("")
        print("⚠️ Runtime/strategy accepted but not fully implemented yet.")
        print(f"Requested: runtime={runtime}, strategy={strategy}")
        print("")
        print("Currently implemented:")
        print("- local + direct")
        print("- local + bluegreen using Docker-based blue/green")
        print("- docker/docker_host + bluegreen")
        print("")
        print("This is reserved for future ADN deployment support.")
        raise SystemExit(2)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="AI-powered Pingora gateway/load balancer generator."
    )

    parser.add_argument(
        "--runtime",
        default="local",
        metavar="RUNTIME",
        help=(
            "Runtime environment. Supported names: local, docker, docker_host, "
            "kubernetes/k8s, ecs, nomad, vm, bare_metal. "
            "Currently implemented: local and docker_host."
        ),
    )

    parser.add_argument(
        "--strategy",
        default=None,
        metavar="STRATEGY",
        help=(
            "Deployment strategy. Supported names: direct, bluegreen, rolling, canary. "
            "Currently implemented: direct and bluegreen."
        ),
    )

    parser.add_argument(
        "prompt",
        nargs="*",
        help="Natural-language gateway instruction.",
    )

    return parser.parse_args()


def main() -> int:
    load_dotenv()

    args = parse_args()

    runtime = normalize_runtime(args.runtime)
    strategy = normalize_strategy(args.strategy, runtime)

    validate_runtime_strategy(runtime, strategy)

    prompt = " ".join(args.prompt).strip()

    if not prompt:
        prompt = input("Describe infrastructure: ").strip()

    if not prompt:
        print("❌ Prompt is empty.")
        return 1

    runtime_mode = effective_runtime_mode(runtime, strategy)

    print("")
    print(f"Requested runtime: {runtime}")
    print(f"Deployment strategy: {strategy}")
    print(f"Effective runtime mode: {runtime_mode}")

    if runtime == "local" and strategy == "bluegreen":
        print(
            "ℹ️ local + bluegreen selected: using docker_host addressing because "
            "the current blue/green deployer runs through Docker/Nginx."
        )

    use_docker = strategy == "bluegreen"
    use_docker_compose = strategy == "bluegreen"
    use_predeploy_sandbox = False

    result = run_graph(
        prompt=prompt,
        project_root=PROJECT_ROOT,
        project_dir=PROJECT_DIR,
        use_docker=use_docker,
        use_docker_compose=use_docker_compose,
        use_predeploy_sandbox=use_predeploy_sandbox,
        runtime=runtime,
        runtime_mode=runtime_mode,
        strategy=strategy,
        deployment_strategy=strategy,
    )

    if result.get("error"):
        return 1

    final_message = result.get("final_message")

    if final_message:
        print("")
        print(final_message)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())