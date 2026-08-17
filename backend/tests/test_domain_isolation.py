"""Makes the dependency rule executable.

Clean Architecture's one absolute rule — source dependencies point inwards only
— is otherwise a convention, and conventions rot quietly. This walks the AST of
every module under ``app/domain`` and fails the build the moment the core grows
an outward dependency: a stray ``import pydantic`` for convenience, a ``boto3``
type annotation, a ``from fastapi import HTTPException``.

Static analysis rather than import-time inspection, so a violation is caught
even in a branch that never executes.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

DOMAIN_DIR = Path(__file__).resolve().parents[1] / "app" / "domain"
DOMAIN_PACKAGE = "app.domain"
STDLIB_MODULES = frozenset(sys.stdlib_module_names)


def _domain_modules() -> list[Path]:
    return sorted(DOMAIN_DIR.glob("*.py"))


def _imported_modules(source: str) -> set[str]:
    """Absolute module names imported by ``source``.

    Relative imports are skipped: inside ``domain`` they can only reach
    ``domain``, which is the core depending on itself.
    """
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported.add(node.module)
    return imported


def _is_permitted(module: str) -> bool:
    if module.split(".")[0] in STDLIB_MODULES:
        return True
    return module == DOMAIN_PACKAGE or module.startswith(f"{DOMAIN_PACKAGE}.")


def test_domain_directory_is_not_empty() -> None:
    """Guard against the isolation test silently passing on nothing at all."""
    assert _domain_modules(), f"no modules found under {DOMAIN_DIR}"


def test_domain_imports_nothing_outside_the_standard_library() -> None:
    violations: dict[str, set[str]] = {}

    for path in _domain_modules():
        offending = {
            module
            for module in _imported_modules(path.read_text(encoding="utf-8"))
            if not _is_permitted(module)
        }
        if offending:
            violations[path.name] = offending

    assert not violations, (
        "domain/ must depend on nothing but the standard library and itself; "
        f"found: {violations}"
    )
