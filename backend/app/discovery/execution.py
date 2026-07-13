"""Discovery recipe execution dispatch.

This module keeps recipe execution outside the HTTP route layer. It only
selects the right interpreter for a stored crawl-method recipe.
"""

from __future__ import annotations

from typing import Any, Callable

from app.discovery.dsl import DslRecipe
from app.discovery.interpreter import DslInterpreter
from app.discovery.multi_dsl import MultiDslRecipe
from app.discovery.multi_interpreter import MultiDslInterpreter


def run_method(
    recipe: DslRecipe | MultiDslRecipe | dict,
    *,
    progress_callback: Callable[[str, dict[str, Any]], None] | None = None,
) -> dict:
    """Run one deterministic discovery recipe and return candidate items."""
    if isinstance(recipe, dict):
        recipe_type = recipe.get("recipe_type")
        if recipe_type == "multi_dsl":
            return MultiDslInterpreter().run(MultiDslRecipe(**recipe), progress_callback=progress_callback)
        return DslInterpreter().run(DslRecipe(**recipe))
    if isinstance(recipe, MultiDslRecipe):
        return MultiDslInterpreter().run(recipe, progress_callback=progress_callback)
    return DslInterpreter().run(recipe)
