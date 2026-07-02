import pytest

from backend.memory import embeddings


def _fail_if_requests_post_called(*args, **kwargs):
    pytest.fail("requests.post should not be called in safe embedding mode")


def _reset_real_api_env(monkeypatch):
    for name in (
        "USE_REAL_EMBEDDING",
        "ALLOW_REAL_API_CALLS",
        "OPENAI_API_KEY",
        "REAL_API_MAX_CALLS",
    ):
        monkeypatch.delenv(name, raising=False)


def _assert_embed_does_not_call_requests(monkeypatch):
    monkeypatch.setattr(
        embeddings.requests,
        "post",
        _fail_if_requests_post_called,
    )

    vectors = embeddings.embed(["safety test"])
    assert isinstance(vectors, list)
    assert len(vectors) == 1
    assert isinstance(vectors[0], list)
    assert len(vectors[0]) > 0


def test_default_state_does_not_call_requests_post(monkeypatch):
    _reset_real_api_env(monkeypatch)

    _assert_embed_does_not_call_requests(monkeypatch)


def test_explicit_test_state_does_not_call_requests_post(monkeypatch):
    _reset_real_api_env(monkeypatch)
    monkeypatch.setenv("USE_REAL_EMBEDDING", "false")
    monkeypatch.setenv("ALLOW_REAL_API_CALLS", "false")

    _assert_embed_does_not_call_requests(monkeypatch)


def test_only_use_real_embedding_true_does_not_call_requests_post(monkeypatch):
    _reset_real_api_env(monkeypatch)
    monkeypatch.setenv("USE_REAL_EMBEDDING", "true")

    _assert_embed_does_not_call_requests(monkeypatch)


def test_only_allow_real_api_calls_true_does_not_call_requests_post(monkeypatch):
    _reset_real_api_env(monkeypatch)
    monkeypatch.setenv("ALLOW_REAL_API_CALLS", "true")

    _assert_embed_does_not_call_requests(monkeypatch)
