from __future__ import annotations

from mlops_agents.knowledge.limits import MAX_QUERY_CHARS, MAX_QUERY_TOKENS
from mlops_agents.knowledge.query import build_fts_query


def test_empty_and_whitespace_input() -> None:
    assert build_fts_query("") is None
    assert build_fts_query("   ") is None
    assert build_fts_query("\t\n\r") is None


def test_punctuation_and_symbols_only() -> None:
    assert build_fts_query("***") is None
    assert build_fts_query("--- ::: ((( )))") is None
    assert build_fts_query('" " "') is None
    assert build_fts_query("!@#$%^&*()+-=[]{}|;':,./<>?") is None


def test_simple_words() -> None:
    assert build_fts_query("hello world") == '"hello" OR "world"'


def test_lowercasing_and_deduplication() -> None:
    # Preserves first appearance order, case-insensitive dedup
    assert build_fts_query("Apple banana APPLE Orange BANANA") == '"apple" OR "banana" OR "orange"'


def test_fts_keywords_treated_as_plain_tokens() -> None:
    # FTS keywords like AND, NOT, OR, NEAR should become quoted literals
    res = build_fts_query("AND NOT OR NEAR")
    assert res == '"and" OR "not" OR "or" OR "near"'


def test_special_characters_stripped() -> None:
    # Quotes, parens, colons, asterisks, hyphens
    res = build_fts_query('"quoted" and (parentheses) or key:value model-name*')
    # "quoted" -> quoted; and -> and; parentheses -> parentheses; or -> or; key:value -> key, value; model-name* -> model, name
    assert (
        res == '"quoted" OR "and" OR "parentheses" OR "or" OR "key" OR "value" OR "model" OR "name"'
    )


def test_max_tokens_limit() -> None:
    # More than MAX_QUERY_TOKENS tokens: cut to MAX_QUERY_TOKENS
    words = [f"word{i}" for i in range(50)]
    query = " ".join(words)
    res = build_fts_query(query)
    assert res is not None
    parts = res.split(" OR ")
    assert len(parts) == MAX_QUERY_TOKENS
    assert parts[0] == '"word0"'
    assert parts[-1] == f'"word{MAX_QUERY_TOKENS - 1}"'


def test_max_chars_limit() -> None:
    # String longer than MAX_QUERY_CHARS is cut before tokenization
    long_prefix = "a" * (MAX_QUERY_CHARS + 500)
    # The first token exceeds MAX_QUERY_CHARS, so after cutting at MAX_QUERY_CHARS it's just 'a' * 2000
    res = build_fts_query(long_prefix + " unreachable_token")
    assert res is not None
    # unreachable_token should not be present because it was after MAX_QUERY_CHARS
    assert "unreachable_token" not in res


def test_non_ascii_unicode_words() -> None:
    # Non-ASCII words (accents, umlauts, Cyrillic, etc.)
    res = build_fts_query("Café résumé naïve Übergröße Москва")
    assert res is not None
    assert '"café"' in res
    assert '"résumé"' in res
    assert '"naïve"' in res
    assert '"übergröße"' in res
    assert '"москва"' in res


async def test_hostile_input_through_real_sqlite_match(databases: object) -> None:
    from mlops_agents.knowledge.notes import query_fts_candidates
    from mlops_agents.storage.databases import Databases

    assert isinstance(databases, Databases)
    project_id = "prj_hostile1"
    engine = await databases.project(project_id)
    hostile_queries = [
        '"""',
        "NEAR(a, b, 10)",
        "* OR *",
        'title: "hack" AND text: "inject"',
        "-x +y",
        '"unbalanced quote',
        'MATCH "syntax error"',
        "SELECT * FROM notes",
        "'; DROP TABLE notes; --",
    ]
    for hq in hostile_queries:
        fts = build_fts_query(hq)
        if fts is not None:
            # Must execute safely against real FTS5 virtual table without raising OperationalError
            await query_fts_candidates(engine, fts, limit=5, note_type=None)
