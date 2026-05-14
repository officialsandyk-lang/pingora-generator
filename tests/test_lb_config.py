from core.lb_config import (
    add_route_or_backend,
    merge_duplicate_routes,
    remove_backend_from_route,
    set_route_algorithm,
)


def test_duplicate_same_path_routes_merge_into_upstreams():
    config = {
        "port": 8088,
        "routes": [
            {"path": "/users", "upstream": "127.0.0.1:3001"},
            {"path": "/users", "upstream": "127.0.0.1:3002"},
            {"path": "/users", "upstream": "127.0.0.1:3003"},
        ],
    }

    repaired = merge_duplicate_routes(config)

    assert repaired["routes"] == [
        {
            "path": "/users",
            "upstream": "127.0.0.1:3001",
            "balancing": "round_robin",
            "upstreams": [
                {"address": "127.0.0.1:3001", "weight": 1},
                {"address": "127.0.0.1:3002", "weight": 1},
                {"address": "127.0.0.1:3003", "weight": 1},
            ],
        }
    ]


def test_duplicate_same_backend_is_ignored():
    config = {
        "port": 8088,
        "routes": [
            {"path": "/users", "upstream": "127.0.0.1:3001"},
            {"path": "/users", "upstream": "127.0.0.1:3001"},
        ],
    }

    repaired = merge_duplicate_routes(config)

    assert repaired["routes"] == [
        {
            "path": "/users",
            "upstream": "127.0.0.1:3001",
        }
    ]


def test_add_backend_to_existing_route_creates_lb_route():
    config = {
        "port": 8088,
        "routes": [
            {"path": "/users", "upstream": "127.0.0.1:3001"},
        ],
    }

    updated, changed, summary = add_route_or_backend(
        config,
        "/users",
        "3002",
        as_backend=True,
    )

    assert changed is True
    assert "Added backend" in summary
    assert updated["routes"][0]["upstream"] == "127.0.0.1:3001"
    assert updated["routes"][0]["balancing"] == "round_robin"
    assert updated["routes"][0]["upstreams"] == [
        {"address": "127.0.0.1:3001", "weight": 1},
        {"address": "127.0.0.1:3002", "weight": 1},
    ]


def test_remove_backend_collapses_to_single_upstream_route():
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

    updated, changed, summary = remove_backend_from_route(config, "/users", "3002")

    assert changed is True
    assert "Removed backend" in summary
    assert updated["routes"][0] == {
        "path": "/users",
        "upstream": "127.0.0.1:3001",
    }


def test_set_route_algorithm():
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

    updated, changed, summary = set_route_algorithm(config, "/users", "round robin")

    assert changed is True
    assert "round_robin" in summary
    assert updated["routes"][0]["balancing"] == "round_robin"
