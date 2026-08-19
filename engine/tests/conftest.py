import pytest

from engine.analysis_core.io.statestore import StateStore


@pytest.fixture
def store(tmp_path):
    s = StateStore(driver="sqlite", dsn=str(tmp_path / "kubehuddle.db"))
    s.apply_schema()
    try:
        yield s
    finally:
        s.close()
