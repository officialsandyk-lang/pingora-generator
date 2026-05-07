from __future__ import annotations

import importlib
import re
from typing import Any, Callable

try:
    from langsmith import traceable
except Exception:
    def traceable(*args: Any, **kwargs: Any):
        def decorator(fn):
            return fn
        return decorator


UPDATE_GRAPH_MODULE = "orchestration.update_graph"

UPDATE_FLOW_CANDIDATES = [
    "run_update_graph",
    "run_update_flow",
    "update_gateway_flow",
    "run",
    "main",
]


@traceable(name="control_plane_repair_agent", run_type="chain")
def resolve_update_flow() -> Callable[[str], Any]:
    """
    Repairs update.py control-plane entrypoint issues.

    Example repair:
      update.py expects run_update_flow
      update_graph.py actually has run_update_graph

    Instead of crashing, this agent introspects the module and chooses the valid
    update graph function.
    """

    module = importlib.import_module(UPDATE_GRAPH_MODULE)

    for name in UPDATE_FLOW_CANDIDATES:
        fn = getattr(module, name, None)
        if callable(fn):
            print(f"🛠️ Control-plane repair agent selected update entrypoint: {name}")
            return fn

    callable_names = [
        name
        for name in dir(module)
        if callable(getattr(module, name, None)) and not name.startswith("_")
    ]

    raise RuntimeError(
        "Control-plane repair failed. "
        f"No update graph entrypoint found in {UPDATE_GRAPH_MODULE}. "
        f"Tried: {UPDATE_FLOW_CANDIDATES}. "
        f"Available callables: {callable_names}"
    )


@traceable(name="control_plane_import_error_repair", run_type="chain")
def explain_import_error(error: Exception) -> str:
    message = str(error)

    did_you_mean = re.search(r"Did you mean: '([^']+)'", message)
    if did_you_mean:
        suggested = did_you_mean.group(1)
        return (
            "Python import failed, but the interpreter suggested a valid function: "
            f"{suggested}. The control-plane repair agent will try module introspection."
        )

    return (
        "Python import failed. The control-plane repair agent will inspect "
        "orchestration.update_graph and select a compatible entrypoint."
    )