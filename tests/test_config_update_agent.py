from __future__ import annotations

import pytest

from agents.config_update_agent import apply_config_update


@pytest.fixture(autouse=True)
def disable_langchain(monkeypatch):
    """
    Keep unit tests deterministic and avoid OpenAI calls.
    """
    monkeypatch.setenv("ENABLE_LANGCHAIN_UPDATE_AGENT", "false")


@pytest.fixture
def sample_config():
    return {
        "routes": [
            {"path": "/", "backend": "127.0.0.1:3000"},
            {"path": "/users", "backend": "127.0.0.1:3000"},
            {"path": "/orders", "backend": "127.0.0.1:5000"},
            {"path": "/admin", "backend": "127.0.0.1:8000"},
        ],
        "security": {
            "blocked_paths": ["/private", "/internal"],
            "allowed_methods": ["GET", "POST"],
            "rate_limit_per_minute": 120,
            "max_request_body_bytes": 1048576,
            "max_connections": 1000,
            "upstream_timeout_seconds": 30,
        },
    }


def route_paths(config):
    return {route["path"] for route in config["routes"]}


def route_backend(config, path):
    for route in config["routes"]:
        if route["path"] == path:
            return route["backend"]
    return None


def test_add_new_route(sample_config):
    result = apply_config_update(
        sample_config,
        "add /inventory to backend 9001",
    )

    assert result["changed"] is True
    assert "/inventory" in route_paths(result["config"])
    assert route_backend(result["config"], "/inventory") == "127.0.0.1:9001"
    assert any("Added route: /inventory" in item for item in result["change_summary"])


def test_duplicate_route_is_reported_without_change(sample_config):
    result = apply_config_update(
        sample_config,
        "add /users to backend 3000",
    )

    assert result["changed"] is False
    assert result["understood"] is True
    assert any(
        "Duplicate route ignored" in item or "already existed" in item
        for item in result["change_summary"]
    )


def test_update_existing_route_backend(sample_config):
    result = apply_config_update(
        sample_config,
        "update /orders to backend 5050",
    )

    assert result["changed"] is True
    assert route_backend(result["config"], "/orders") == "127.0.0.1:5050"
    assert any("Updated route: /orders" in item for item in result["change_summary"])


def test_remove_existing_route(sample_config):
    result = apply_config_update(
        sample_config,
        "remove /admin",
    )

    assert result["changed"] is True
    assert "/admin" not in route_paths(result["config"])
    assert any("Removed route: /admin" in item for item in result["change_summary"])


def test_remove_absent_route_is_reported_without_change(sample_config):
    result = apply_config_update(
        sample_config,
        "remove /analytics",
    )

    assert result["changed"] is False
    assert result["understood"] is True
    assert any("Route already absent: /analytics" in item for item in result["change_summary"])


def test_bare_route_name_is_normalized_for_remove(sample_config):
    result = apply_config_update(
        sample_config,
        "remove admin",
    )

    assert result["changed"] is True
    assert "/admin" not in route_paths(result["config"])
    assert any("Removed route: /admin" in item for item in result["change_summary"])


def test_security_block_paths(sample_config):
    result = apply_config_update(
        sample_config,
        "block /secret and /debug",
    )

    security = result["config"]["security"]

    assert result["changed"] is True
    assert "/secret" in security["blocked_paths"]
    assert "/debug" in security["blocked_paths"]
    assert any("Security changed" in item for item in result["change_summary"])


def test_security_methods_and_limits(sample_config):
    result = apply_config_update(
        sample_config,
        "only allow GET and POST, set rate limit to 90 requests per minute, "
        "max request body to 524288 bytes, max connections to 750, "
        "and upstream timeout to 20 seconds",
    )

    security = result["config"]["security"]

    assert result["changed"] is True
    assert security["allowed_methods"] == ["GET", "POST"]
    assert security["rate_limit_per_minute"] == 90
    assert security["max_request_body_bytes"] == 524288
    assert security["max_connections"] == 750
    assert security["upstream_timeout_seconds"] == 20