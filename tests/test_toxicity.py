import pytest
import backend.utils.db as db_module
from backend.utils.db import init_db
from backend.memory.toxicity import (
    calc_toxicity,
    update_toxicity,
    get_toxicity_factor,
)

@pytest.fixture(autouse=True)
def setup_db(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    db_module.DB_PATH = str(db_file)
    init_db()

def test_toxic_keywords():
    score = calc_toxicity("你嘴臭死了瓜娃子")
    assert score >= 0.4

def test_sweet_keywords():
    score = calc_toxicity("亲爱的克里你好棒")
    assert score == 0.0

def test_mixed():
    score = calc_toxicity("你嘴臭但是乖")
    assert 0.0 <= score <= 1.0

def test_update_and_factor():
    update_toxicity("保长你嘴臭", "那你骂回来啊", agent_id="test")
    update_toxicity("滚啊保长", "好的我滚", agent_id="test")
    factor = get_toxicity_factor(agent_id="test")
    assert factor > 1.0

def test_agent_isolation():
    update_toxicity("嘴臭滚", "...", agent_id="baozhang")
    factor_bao = get_toxicity_factor(agent_id="baozhang")
    factor_keli = get_toxicity_factor(agent_id="keli")
    assert factor_bao > factor_keli
