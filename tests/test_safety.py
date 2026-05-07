from __future__ import annotations

from core.safety import (
    detect_destructive_intent,
    normalize_route_path,
    normalize_safe_route_prompt,
    parse_update_cli_args,
    rewrite_confirmed_destructive_prompt,
)


def test_normalize_route_path_adds_slash():
    assert normalize_route_path("analytics") == "/analytics"


def test_normalize_route_path_removes_trailing_slash():
    assert normalize_route_path("/analytics/") == "/analytics"


def test_parse_update_cli_args_joins_unquoted_prompt():
    command = parse_update_cli_args(["add", "/inventory", "to", "backend", "9001"])

    assert command.prompt == "add /inventory to backend 9001"
    assert command.confirm is False


def test_parse_update_cli_args_detects_confirm():
    command = parse_update_cli_args(["delete", "analytics", "--confirm"])

    assert command.prompt == "delete analytics"
    assert command.confirm is True


def test_delete_is_destructive():
    intent = detect_destructive_intent("delete analytics")

    assert intent.detected is True
    assert intent.routes == ["/analytics"]


def test_remove_is_not_destructive():
    intent = detect_destructive_intent("remove analytics")

    assert intent.detected is False


def test_confirmed_delete_rewrites_to_safe_remove():
    intent = detect_destructive_intent("delete analytics")
    prompt = rewrite_confirmed_destructive_prompt("delete analytics", intent)

    assert prompt == "remove /analytics"


def test_safe_remove_prompt_normalizes_bare_route():
    prompt = normalize_safe_route_prompt("remove analytics")

    assert prompt == "remove /analytics"


def test_stop_routing_rewrites_to_remove():
    prompt = normalize_safe_route_prompt("stop routing to analytics")

    assert prompt == "remove /analytics"