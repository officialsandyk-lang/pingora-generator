import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
os.chdir(PROJECT_ROOT)

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.rollback import rollback_to_previous


def main():
    result = rollback_to_previous()

    print("\n✅ Rolled back successfully")
    print(f"Active color: {result['active_color']}")
    print(f"Previous active color: {result['old_active_color']}")
    print(f"Live URL: {result['live_url']}")


if __name__ == "__main__":
    main()