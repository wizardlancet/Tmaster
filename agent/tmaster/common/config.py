"""Shared config helpers.

Each component has its own Settings subclass (agent, sidecar, server) that
pulls from env vars and an optional TOML file. This module provides the common
loader used by all three.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


def load_toml(path: str | os.PathLike[str] | None) -> dict[str, Any]:
    if not path:
        return {}
    p = Path(path)
    if not p.exists():
        return {}
    with p.open("rb") as fh:
        return tomllib.load(fh)


def load_settings(cls: type[T], *, file_env: str, default_paths: list[str]) -> T:
    """Load a pydantic model from: TOML file > env vars > model defaults.

    `file_env` is the env var holding the config file path; if unset we try
    `default_paths` in order.
    """
    path = os.environ.get(file_env)
    if path is None:
        for candidate in default_paths:
            if Path(candidate).expanduser().exists():
                path = candidate
                break
    data = load_toml(Path(path).expanduser()) if path else {}
    # pydantic-settings would also read env vars automatically, but keeping
    # this layer simple and explicit makes it easier to unit-test.
    return cls.model_validate(data)
