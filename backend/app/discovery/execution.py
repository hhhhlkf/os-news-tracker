"""Discovery recipe execution dispatch.

This module keeps recipe execution outside the HTTP route layer. It only
selects the right interpreter for a stored crawl-method recipe.
"""

from __future__ import annotations

from typing import Any, Callable

from app.discovery.dsl import DslRecipe
from app.discovery.multi_dsl import MultiDslRecipe
from app.discovery.multi_interpreter import MultiDslInterpreter


def run_method(
    recipe: DslRecipe | MultiDslRecipe | dict,
    *,
    progress_callback: Callable[[str, dict[str, Any]], None] | None = None,
) -> dict:
    """Run one deterministic discovery recipe and return candidate items.

    Legacy ``dsl`` recipes are wrapped as ``multi_dsl`` website recipes at
    runtime so execution has one public interpreter entrypoint.
    """
    if isinstance(recipe, dict):
        return MultiDslInterpreter().run(
            _as_multi_dsl_recipe(recipe),
            progress_callback=progress_callback,
        )
    if isinstance(recipe, MultiDslRecipe):
        return MultiDslInterpreter().run(recipe, progress_callback=progress_callback)
    return MultiDslInterpreter().run(
        _as_multi_dsl_recipe(recipe.model_dump(by_alias=True)),
        progress_callback=progress_callback,
    )


def _as_multi_dsl_recipe(recipe: dict[str, Any]) -> MultiDslRecipe:
    if recipe.get("recipe_type") == "multi_dsl":
        return MultiDslRecipe(**recipe)
    dsl = DslRecipe(**recipe)
    return MultiDslRecipe(
        recipe_type="multi_dsl",
        source_kind="website",
        entry=dsl.entry_url,
        auth_ref=None,
        requires_auth=False,
        actions=[action.model_dump(by_alias=True) for action in dsl.actions],
        notes=dsl.notes,
    )
