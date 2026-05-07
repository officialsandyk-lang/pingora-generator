import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.absolute()
PROJECT_DIR = PROJECT_ROOT / "generated-pingora-proxy"

os.chdir(PROJECT_ROOT)

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from orchestration.graph import run_graph


USE_DOCKER = False
USE_DOCKER_COMPOSE = True
USE_PREDEPLOY_SANDBOX = False


def main():
    prompt = input("Describe infrastructure: ").strip()

    if not prompt:
        print("❌ Prompt is empty.")
        return

    result = run_graph(
        prompt=prompt,
        project_root=PROJECT_ROOT,
        project_dir=PROJECT_DIR,
        use_docker=USE_DOCKER,
        use_docker_compose=USE_DOCKER_COMPOSE,
        use_predeploy_sandbox=USE_PREDEPLOY_SANDBOX,
    )

    if result.get("error"):
        return

    final_message = result.get("final_message")
    if final_message:
        print("")
        print(final_message)


if __name__ == "__main__":
    main()