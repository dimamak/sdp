"""Corpus retrieval for the X reply radar — see plan.md §6 step 1.

Pure Python, no new dependency: a keyword-overlap ranking over the same
`items.summary` text the nightly draft pipeline already produces. Runs before
any LLM call, so the drafting call (reply.py) never needs filesystem or
retrieval logic of its own — see reply.py's module docstring for why that
separation is a security boundary, not just a layering preference.
"""
from __future__ import annotations

import re
from collections import Counter

_WORD_RE = re.compile(r"[a-z0-9]+")

# Words too common to mean anything as a match signal.
_STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "of", "to", "in", "on", "for", "is",
    "are", "was", "were", "it", "this", "that", "with", "as", "at", "by",
    "be", "we", "i", "you", "our", "my", "your", "not", "so", "if", "than",
}


def tokenize(text: str) -> list[str]:
    return [w for w in _WORD_RE.findall((text or "").lower()) if w not in _STOPWORDS]


def retrieve(store, query_text: str, limit: int = 8) -> list[dict]:
    """Rank the user's harvested corpus against `query_text` (the candidate post).

    Returns the top `limit` matches, best first, as
    {"item_id", "source", "day", "summary", "score"}. Empty query or empty
    corpus both yield an empty list — the caller (reply.py / cli.py) treats
    that as "nothing found" and routes to the ask-a-question flow rather than
    drafting from nothing.
    """
    query_terms = set(tokenize(query_text))
    if not query_terms:
        return []
    matches = []
    for row in store.radar_corpus():
        terms = tokenize(row["summary"])
        if not terms:
            continue
        counts = Counter(terms)
        overlap = sum(counts[t] for t in query_terms if t in counts)
        if overlap == 0:
            continue
        # Normalize by sqrt(length) so a long item doesn't win purely on
        # volume over a short, tightly-matching one.
        score = overlap / (len(terms) ** 0.5)
        matches.append({
            "item_id": row["id"], "source": row["source"], "day": row["day"],
            "summary": row["summary"], "score": score,
        })
    matches.sort(key=lambda m: m["score"], reverse=True)
    return matches[:limit]


def relevance_score(matches: list[dict]) -> float:
    """0..1 signal for score.py's relevance component, from retrieve()'s top match.

    5.0 is an empirically-reasonable "strong match" ceiling for the
    length-normalized overlap score above; re-tune once real corpus data
    exists (plan.md §5's calibration note applies here too).
    """
    if not matches:
        return 0.0
    return min(1.0, matches[0]["score"] / 5.0)
