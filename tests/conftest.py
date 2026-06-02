import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    return FIXTURES


@pytest.fixture(scope="session")
def manifest() -> dict:
    data = {}
    for line in (FIXTURES / "manifest.txt").read_text().splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            data[k] = v
    return data


def read_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")
