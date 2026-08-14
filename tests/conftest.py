import gzip
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FIXTURES = Path(__file__).resolve().parent / "fixtures"
B9 = FIXTURES / "banner9"


@pytest.fixture(scope="session")
def b9_dir() -> Path:
    return B9


def read_b9(name: str) -> bytes:
    """Fixture bytes. The large JSON pages are stored gzipped to keep clones small."""
    gz = B9 / (name + ".gz")
    if gz.exists():
        return gzip.decompress(gz.read_bytes())
    return (B9 / name).read_bytes()
