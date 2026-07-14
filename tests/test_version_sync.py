"""__version__ 与 pyproject 的版本必须一致 — 0.11.1 时曾漂移过一次。"""

import tomllib
from pathlib import Path

import autoweaver


def test_module_version_matches_pyproject():
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    with open(pyproject, "rb") as f:
        declared = tomllib.load(f)["project"]["version"]
    assert autoweaver.__version__ == declared
