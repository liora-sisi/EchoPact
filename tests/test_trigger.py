import pytest
import backend.utils.db as db_module
from backend.utils.db import init_db
from backend.judge.trigger_levels import (
    get_trigger_level, TriggerLevel,
    L1_CONFIG, L2_CONFIG, L3_CONFIG
)

@pytest.fixture(autouse=True)
def setup_db(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    db_module.DB_PATH = str(db_file)
    init_db()

def test_default_is_l1():
    config = get_trigger_level()
    assert config.level == TriggerLevel.L1

def test_saga_triggers_l2():
    config = get_trigger_level(has_saga=True)
    assert config.level == TriggerLevel.L2

def test_urgent_triggers_l3():
    config = get_trigger_level(is_urgent=True)
    assert config.level == TriggerLevel.L3

def test_urgent_overrides_saga():
    config = get_trigger_level(has_saga=True, is_urgent=True)
    assert config.level == TriggerLevel.L3

def test_l1_interval():
    assert L1_CONFIG.min_interval_minutes == 60
    assert L1_CONFIG.daily_limit == 5

def test_l2_interval():
    assert L2_CONFIG.min_interval_minutes == 120
    assert L2_CONFIG.daily_limit == 3

def test_l3_interval():
    assert L3_CONFIG.min_interval_minutes == 30
    assert L3_CONFIG.daily_limit == 2
