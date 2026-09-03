from server.radar.retrieve import relevance_score, retrieve, tokenize
from server.store import Store


def test_relevant_ranked_above_irrelevant(tmp_path):
    store = Store(tmp_path / "store")
    store.add_item("claude_sessions", "1", "2026-09-01",
                    summary="Debugged a Postgres logical replication slot filling up disk")
    store.add_item("claude_sessions", "2", "2026-09-01",
                    summary="Wrote a blog post about sourdough bread baking times")
    matches = retrieve(store, "logical replication slot disk blowup postgres", limit=5)
    assert matches
    assert "replication" in matches[0]["summary"].lower()
    assert len(matches) == 1  # the bread item shares no terms at all


def test_empty_corpus_returns_empty():
    class _EmptyStore:
        def radar_corpus(self):
            return []

    assert retrieve(_EmptyStore(), "anything here") == []


def test_empty_query_returns_empty_without_dropping_to_the_question_flow_incorrectly(tmp_path):
    store = Store(tmp_path / "store")
    store.add_item("claude_sessions", "1", "2026-09-01", summary="something")
    assert retrieve(store, "") == []


def test_relevance_score_of_no_matches_is_zero():
    assert relevance_score([]) == 0.0


def test_relevance_score_scales_with_match_strength():
    weak = relevance_score([{"score": 0.5}])
    strong = relevance_score([{"score": 10}])
    assert 0 < weak < strong <= 1.0


def test_tokenize_drops_stopwords_and_lowercases():
    assert tokenize("The Quick Brown Fox") == ["quick", "brown", "fox"]
