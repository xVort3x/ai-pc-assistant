"""Plugin discovery.

Built-ins are modules in ``pcassistant.plugins``.  External plugins are
Python files passed with ``--plugins-dir`` or configured by an embedding
application.  A broken plugin produces a load report entry, never a crash.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import inspect
import logging
import pkgutil
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Iterable

from .config import Config
from .plugin import Plugin


def _classes_in_module(module: ModuleType) -> list[type[Plugin]]:
    found: list[type[Plugin]] = []
    for _, candidate in inspect.getmembers(module, inspect.isclass):
        if candidate is Plugin:
            continue
        if not issubclass(candidate, Plugin):
            continue
        if candidate.__module__ != module.__name__:
            continue  # imported base classes, not definitions
        found.append(candidate)
    return found


def _validate(plugin_class: type[Plugin]) -> str:
    identifier = getattr(plugin_class, "id", "")
    if not identifier:
        return "missing id"
    if not re.fullmatch(r"[a-z][a-z0-9_]{1,40}", identifier):
        return f"invalid id {identifier!r} (use lower_snake_case)"
    if inspect.isabstract(plugin_class):
        return "abstract class"
    return ""


def discover_builtin(
    logger: logging.Logger,
    package_name: str = "pcassistant.plugins",
) -> tuple[list[type[Plugin]], list[dict[str, str]]]:
    classes: list[type[Plugin]] = []
    errors: list[dict[str, str]] = []

    try:
        package = importlib.import_module(package_name)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Could not import plugin package %s", package_name)
        return [], [{"module": package_name, "error": f"{type(exc).__name__}: {exc}"}]

    for module_info in pkgutil.iter_modules(package.__path__, package.__name__ + "."):
        short_name = module_info.name.rsplit(".", 1)[-1]
        if short_name.startswith("_"):
            continue
        try:
            module = importlib.import_module(module_info.name)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Could not import plugin module %s", module_info.name)
            errors.append(
                {"module": module_info.name, "error": f"{type(exc).__name__}: {exc}"}
            )
            continue
        for plugin_class in _classes_in_module(module):
            problem = _validate(plugin_class)
            if problem:
                errors.append({"module": module_info.name, "error": problem})
                continue
            classes.append(plugin_class)

    return classes, errors


def load_plugin_file(
    path: Path, logger: logging.Logger
) -> tuple[list[type[Plugin]], dict[str, str] | None]:
    path = Path(path)
    if not path.is_file() or path.suffix.lower() != ".py":
        return [], {"module": str(path), "error": "not a .py file"}
    digest = hashlib.sha1(str(path.resolve()).encode("utf-8")).hexdigest()[:10]
    module_name = f"pcassistant_external_{path.stem}_{digest}"

    try:
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            return [], {"module": str(path), "error": "could not create import spec"}
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Could not load external plugin %s", path)
        sys.modules.pop(module_name, None)
        return [], {"module": str(path), "error": f"{type(exc).__name__}: {exc}"}

    classes = [cls for cls in _classes_in_module(module) if not _validate(cls)]
    invalid = [cls for cls in _classes_in_module(module) if _validate(cls)]
    if invalid:
        return classes, {
            "module": str(path),
            "error": "; ".join(f"{cls.__name__}: {_validate(cls)}" for cls in invalid),
        }
    return classes, None


def discover_external(
    directories: Iterable[Path], logger: logging.Logger
) -> tuple[list[type[Plugin]], list[dict[str, str]]]:
    classes: list[type[Plugin]] = []
    errors: list[dict[str, str]] = []
    for directory in directories:
        root = Path(directory).expanduser()
        if not root.is_dir():
            errors.append({"module": str(root), "error": "plugin directory not found"})
            continue
        for path in sorted(root.glob("*.py")):
            if path.name.startswith("_"):
                continue
            found, error = load_plugin_file(path, logger)
            classes.extend(found)
            if error:
                errors.append(error)
    return classes, errors


def prepare_config(config: Config) -> Config:
    """Hook for future per-plugin config migration. Kept explicit on purpose."""

    return config
