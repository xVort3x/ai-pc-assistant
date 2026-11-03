"""Small filesystem helpers shared by plugins.

All writes are atomic so a crash mid-write cannot leave a corrupt cache that
breaks the next launch.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def read_json(path: Path, default: Any = None) -> Any:
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return default


def write_json_atomic(path: Path, data: Any, *, indent: int = 2) -> bool:
    """Write JSON beside the target, then replace it in one operation."""

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temp_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=indent)
            os.replace(temp_name, path)
            return True
        except BaseException:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise
    except OSError:
        return False


def file_signature(path: Path) -> str:
    """Cheap change detector; cheaper and more reliable than hashing content."""

    try:
        stat = path.stat()
    except OSError:
        return ""
    return f"{stat.st_size}:{int(stat.st_mtime)}"


def safe_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0
