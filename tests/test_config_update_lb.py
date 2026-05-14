from agents.config_update_agent import apply_config_update


def test_update_add_backend_to_route():
    config = {
        "port": 8088,
        "routes": [
            {"path": "/users", "upstream": "127.0.0.1:3001"},
        ],
    }

    result = apply_config_update(config, "add backend 3002 to /users")

    assert result["changed"] is True
    assert result["understood"] is True
    assert result["config"]["routes"][0]["upstreams"] == [
        {"address": "127.0.0.1:3001", "weight": 1},
        {"address": "127.0.0.1:3002", "weight": 1},
    ]


def test_update_duplicate_backend_is_noop_with_summary():
    config = {
        "port": 8088,
        "routes": [
            {
                "path": "/users",
                "upstream": "127.0.0.1:3001",
                "balancing": "round_robin",
                "upstreams": [
                    {"address": "127.0.0.1:3001", "weight": 1},
                    {"address": "127.0.0.1:3002", "weight": 1},
                ],
            }
        ],
    }

    result = apply_config_update(config, "add backend 3002 to /users")

    assert result["changed"] is False
    assert result["understood"] is True
    assert any("Duplicate backend ignored" in item for item in result["change_summary"])


def test_update_remove_backend_from_route():
    config = {
        "port": 8088,
        "routes": [
            {
                "path": "/users",
                "upstream": "127.0.0.1:3001",
                "balancing": "round_robin",
                "upstreams": [
                    {"address": "127.0.0.1:3001", "weight": 1},
                    {"address": "127.0.0.1:3002", "weight": 1},
                ],
            }
        ],
    }

    result = apply_config_update(config, "remove backend 3002 from /users")

    assert result["changed"] is True
    assert result["understood"] is True
    assert result["config"]["routes"][0] == {
        "path": "/users",
        "upstream": "127.0.0.1:3001",
    }


def test_update_set_balanced_across_backends():
    config = {
        "port": 8088,
        "routes": [],
    }

    result = apply_config_update(
        config,
        "set /users backends to 3001, 3002, and 3003",
    )

    route = result["config"]["routes"][0]

    assert result["changed"] is True
    assert result["understood"] is True
    assert route["path"] == "/users"
    assert route["upstream"] == "127.0.0.1:3001"
    assert route["balancing"] == "round_robin"
    assert route["upstreams"] == [
        {"address": "127.0.0.1:3001", "weight": 1},
        {"address": "127.0.0.1:3002", "weight": 1},
        {"address": "127.0.0.1:3003", "weight": 1},
    ]


def test_update_set_algorithm():
    config = {
        "port": 8088,
        "routes": [
            {
                "path": "/users",
                "upstream": "127.0.0.1:3001",
                "upstreams": [
                    {"address": "127.0.0.1:3001", "weight": 1},
                    {"address": "127.0.0.1:3002", "weight": 1},
                ],
            }
        ],
    }

    result = apply_config_update(
        config,
        "set /users algorithm to round robin",
    )

    assert result["changed"] is True
    assert result["config"]["routes"][0]["balancing"] == "round_robin"
