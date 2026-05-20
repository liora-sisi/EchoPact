import pytest
import backend.utils.db as db_module
from backend.utils.db import init_db
from backend.memory.persona import (
    get_persona, update_persona, 
    adapt_persona_to_context, DEFAULT_PERSONA
)

@pytest.fixture(autouse=True)
def setup_db(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    db_module.DB_PATH = str(db_file)
    init_db()

def test_default_persona():
    p = get_persona(agent_id="new")
    assert p["warmth"] == DEFAULT_PERSONA["warmth"]
    assert p["directness"] == DEFAULT_PERSONA["directness"]
    assert p["initiative"] == DEFAULT_PERSONA["initiative"]

def test_update_persona():
    update_persona(agent_id="test", warmth_delta=0.1)
    p = get_persona(agent_id="test")
    assert p["warmth"] > DEFAULT_PERSONA["warmth"]

def test_persona_cap():
    for _ in range(20):
        update_persona(agent_id="test", warmth_delta=0.1)
    p = get_persona(agent_id="test")
    assert p["warmth"] <= 1.0

def test_high_stress_increases_warmth():
    p = adapt_persona_to_context(stress_level=0.8, agent_id="test")
    assert p["warmth"] > DEFAULT_PERSONA["warmth"]

def test_sprint_increases_directness():
    p = adapt_persona_to_context(is_sprint=True, agent_id="test")
    assert p["directness"] > DEFAULT_PERSONA["directness"]

def test_agent_isolation():
    update_persona(agent_id="a", warmth_delta=0.2)
    update_persona(agent_id="b", warmth_delta=-0.2)
    pa = get_persona(agent_id="a")
    pb = get_persona(agent_id="b")
    assert pa["warmth"] > pb["warmth"]
