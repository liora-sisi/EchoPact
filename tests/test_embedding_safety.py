import pytest

from backend.memory import embeddings


class _FakeEmbeddingResponse:
    def __init__(self, vectors):
        self._vectors = vectors

    def raise_for_status(self):
        return None

    def json(self):
        return {"data": [{"embedding": vector} for vector in self._vectors]}


@pytest.fixture(autouse=True)
def _reset_real_call_count():
    original_count = embeddings._real_call_count
    embeddings._real_call_count = 0
    yield
    embeddings._real_call_count = original_count


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


def _enable_real_api(monkeypatch, *, max_calls="50"):
    _reset_real_api_env(monkeypatch)
    monkeypatch.setenv("USE_REAL_EMBEDDING", "true")
    monkeypatch.setenv("ALLOW_REAL_API_CALLS", "true")
    monkeypatch.setenv("OPENAI_API_KEY", "unit-test-placeholder")
    monkeypatch.setenv("REAL_API_MAX_CALLS", max_calls)


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


def test_both_switches_true_without_key_fails_before_network(monkeypatch):
    _reset_real_api_env(monkeypatch)
    monkeypatch.setenv("USE_REAL_EMBEDDING", "true")
    monkeypatch.setenv("ALLOW_REAL_API_CALLS", "true")
    monkeypatch.setattr(embeddings.requests, "post", _fail_if_requests_post_called)

    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        embeddings.embed(["missing key"])


def test_complete_real_configuration_selects_mocked_request_path(monkeypatch):
    _enable_real_api(monkeypatch)
    calls = []
    expected_vector = [0.25] * embeddings.EMBEDDING_DIM

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return _FakeEmbeddingResponse([expected_vector])

    monkeypatch.setattr(embeddings.requests, "post", fake_post)

    assert embeddings.embed(["configured request"]) == [expected_vector]
    assert len(calls) == 1
    url, kwargs = calls[0]
    assert url == "https://api.openai.com/v1/embeddings"
    assert kwargs["headers"]["Authorization"] == "Bearer unit-test-placeholder"
    assert kwargs["json"] == {
        "model": "text-embedding-3-small",
        "input": ["configured request"],
    }
    assert kwargs["timeout"] == 30


def test_real_api_call_limit_triggers_and_process_reset_recovers(monkeypatch):
    _enable_real_api(monkeypatch, max_calls="1")
    calls = []
    expected_vector = [0.5] * embeddings.EMBEDDING_DIM

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return _FakeEmbeddingResponse([expected_vector])

    monkeypatch.setattr(embeddings.requests, "post", fake_post)

    assert embeddings.embed(["first request"]) == [expected_vector]
    with pytest.raises(RuntimeError, match="Real embedding call limit reached: 1"):
        embeddings.embed(["blocked request"])
    assert len(calls) == 1

    # The existing fuse is process-local; a fresh process starts this counter at zero.
    embeddings._real_call_count = 0
    assert embeddings.embed(["after process reset"]) == [expected_vector]
    assert len(calls) == 2


def test_mock_mode_preserves_legacy_constant_vector_behavior(monkeypatch):
    _reset_real_api_env(monkeypatch)
    expected_vector = [0.1] * embeddings.EMBEDDING_DIM

    vectors = embeddings.embed(["first text", "different text"])

    assert vectors == [expected_vector, expected_vector]
    assert vectors[0] is not vectors[1]
    assert embeddings.embed(["first text"]) == [expected_vector]
    assert embeddings.embed([]) == []


def test_mock_mode_preserves_legacy_non_string_tolerance(monkeypatch):
    _reset_real_api_env(monkeypatch)

    vectors = embeddings.embed([None])  # type: ignore[list-item]

    assert vectors == [[0.1] * embeddings.EMBEDDING_DIM]
