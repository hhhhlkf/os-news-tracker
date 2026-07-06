from app.discovery.dsl import DslRecipe, validate_semantics


def test_validate_semantics_rejects_post_scrapling_fetch() -> None:
    recipe = DslRecipe(
        entry_url="https://example.com",
        actions=[
            {
                "op": "fetch",
                "mode": "json",
                "url": "https://example.com/api",
                "method": "POST",
                "transport": "scrapling",
                "json_body": {"page": 1},
            }
        ],
    )

    errors = validate_semantics(recipe)

    assert "action 0: scrapling transport only supports GET fetch actions" in errors


def test_validate_semantics_rejects_post_scrapling_fetch_inside_loop() -> None:
    recipe = DslRecipe(
        entry_url="https://example.com",
        actions=[
            {"op": "set", "var": "page", "value": 1},
            {
                "op": "loop",
                "until": {"count_of": "items", "op": ">=", "value": 10},
                "max_iters": 2,
                "body": [
                    {
                        "op": "fetch",
                        "mode": "json",
                        "url": "https://example.com/api",
                        "method": "POST",
                        "transport": "scrapling",
                        "json_body": {"page": "{{page}}"},
                    }
                ],
            },
        ],
    )

    errors = validate_semantics(recipe)

    assert "action 1.loop.body.0: scrapling transport only supports GET fetch actions" in errors
