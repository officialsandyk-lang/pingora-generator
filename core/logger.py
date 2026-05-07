import json
from datetime import datetime, UTC
from pathlib import Path

LOG_FILE = Path("../logs.jsonl")


def log_run(prompt, config=None, success=False, error=None):
    entry = {
        "time": datetime.now(UTC).isoformat(),
        "prompt": prompt,
        "config": config,
        "success": success,
        "error": error,
    }

    with LOG_FILE.open("a") as f:
        f.write(json.dumps(entry) + "\n")