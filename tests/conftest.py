import os

import pytest

os.environ["USE_REAL_EMBEDDING"] = "false"
os.environ["ALLOW_REAL_API_CALLS"] = "false"
os.environ["USE_REAL_LLM"] = "false"
os.environ["USE_REAL_MODEL"] = "false"


@pytest.fixture(autouse=True)
def _isolated_chroma_path(tmp_path, monkeypatch):
    """Give every test a private Chroma path and client lifecycle."""
    monkeypatch.setenv("CHROMA_PATH", str(tmp_path / "chroma_db"))
    from backend.memory import vector_store

    vector_store.reset_client()
    yield
    vector_store.reset_client()
