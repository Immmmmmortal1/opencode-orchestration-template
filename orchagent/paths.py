from __future__ import annotations

import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_HOME = Path(os.environ.get("ORCHAGENT_HOME", "~/.orchAgent")).expanduser()


def expand_path(value: str | Path) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(str(value))))


def template_path(*parts: str) -> Path:
    return PROJECT_ROOT.joinpath("templates", *parts)


def home_path(*parts: str) -> Path:
    return DEFAULT_HOME.joinpath(*parts)
