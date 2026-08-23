"""Retired website Graph import tombstone.

The old Explorer/Validator/DSL Writer/Auditor implementation was removed in
the connector migration.  These names exist only so opt-in legacy live-test
modules can still be collected and report a clear retirement error if someone
explicitly tries to run them.
"""

from __future__ import annotations

from typing import NoReturn


def _retired(*_args: object, **_kwargs: object) -> NoReturn:
    raise RuntimeError(
        "legacy website Discovery Graph was removed; use the Single Agent Loop"
    )


_make_llm = _retired
auditor = _retired
capture_network = _retired
dsl_writer = _retired
explorer = _retired
fetch_homepage = _retired
validator = _retired
_derive_exploration_candidates_from_state = _retired
_pick_best_deterministic_candidate = _retired
_run_recipe_for_audit = _retired
