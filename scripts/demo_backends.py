from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from core.demo_backend_writer import (
    ensure_host_demo_backend_servers,
    stop_host_demo_backend_servers,
)


def load_config(path: str | Path) -> dict:
    config_path = Path(path)

    if not config_path.is_absolute():
        config_path = PROJECT_ROOT / config_path

    if not config_path.exists():
        raise FileNotFoundError(f"Config not found: {config_path}")

    data = json.loads(config_path.read_text(encoding="utf-8"))

    if not isinstance(data, dict):
        raise ValueError("Config must be a JSON object.")

    return data


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Start/stop host demo backend webservers."
    )
    parser.add_argument("action", choices=["start", "stop"])
    parser.add_argument(
        "config",
        nargs="?",
        default="generated-pingora-proxy/config.json",
    )
    parser.add_argument(
        "--bind",
        default="0.0.0.0",
        help="Bind address. Use 0.0.0.0 for Docker reachability.",
    )

    args = parser.parse_args()
    config = load_config(args.config)

    if args.action == "start":
        result = ensure_host_demo_backend_servers(config, bind_host=args.bind)
    else:
        result = stop_host_demo_backend_servers(config)

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())