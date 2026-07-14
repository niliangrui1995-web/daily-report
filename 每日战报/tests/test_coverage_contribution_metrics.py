from __future__ import annotations

import pytest


def _unit(cm, label, **kwargs):
    result = cm._make_unit(label, kwargs.pop("text", label), **kwargs)
    assert result is not None
    return result


def test_contribution_unit_creation_keys_deduplication_and_jaccard():
    import contribution_metrics as cm

    assert cm._jaccard(set(), {"a"}) == 0
    assert cm._jaccard({"a", "b"}, {"b", "c"}) == pytest.approx(1 / 3)
    assert cm._make_unit("", "") is None
    assert cm._make_unit("the market", "and research") is None
    ticker = _unit(cm, "Company", ticker="nvda")
    url = _unit(cm, "Story", url=" HTTPS://EXAMPLE.COM/X ")
    text = _unit(cm, "Unique phrase")
    assert ticker["key"] == "ticker::NVDA"
    assert url["key"] == "url::https://example.com/x"
    assert text["key"]
    assert cm._dedupe_units([{}, ticker, ticker.copy(), url]) == [ticker, url]


def test_contribution_extractors_cover_raw_fallback_sentence_and_query_paths():
    import contribution_metrics as cm

    p1 = cm.extract_p1_units(
        [
            {
                "category": "AI",
                "query": "chips",
                "text": "ignored\n1. [tag] Nvidia accelerator launch | https://example.com/a\n2. the market",
            }
        ]
    )
    assert len(p1) == 2
    assert p1[0]["url"].startswith("https://")

    structured = cm.extract_p3_units(
        {
            "jinnang_1": ["invalid", {"title": "HBM launch", "summary": "capacity expands"}],
            "jinnang_2": [{"title": "HBM launch", "summary": "duplicate"}],
        }
    )
    assert structured
    fallback = cm.extract_p3_units(
        {},
        "[bucket]\nnot numbered\n1. First fallback signal\n2. Second fallback signal",
    )
    assert len(fallback) == 2
    assert fallback[0]["bucket"] == "[bucket]"

    p4 = cm.extract_p4_units("- [strong] Nvidia (NVDA) +12.5%\n- AMD (AMD) -3%")
    assert [item["ticker"] for item in p4] == ["NVDA", "AMD"]

    bullets = cm.extract_p75_units("# heading\n> quote\n```\n- First useful supplemental finding\n1. Second useful finding")
    assert len(bullets) == 2
    sentence = cm.extract_p75_units(
        "This is a sufficiently long supplemental sentence about accelerator capacity. "
        "Another sufficiently detailed sentence describes the supplier bottleneck."
    )
    assert sentence
    queries = cm.extract_p75_units("short", queries=["HBM capacity expansion", "advanced packaging demand"])
    assert len(queries) == 2


def test_contribution_similarity_and_adoption_cover_all_anchor_shapes():
    import contribution_metrics as cm

    assert cm._unit_similarity(
        _unit(cm, "left", ticker="NVDA"),
        _unit(cm, "right", ticker="NVDA"),
    ) == 1
    assert cm._unit_similarity(
        _unit(cm, "left", url="https://example.com/a"),
        _unit(cm, "right", url="https://example.com/a"),
    ) == 1
    left = _unit(cm, "advanced package")
    right = dict(left)
    right["key"] = f"prefix{left['key']}suffix"
    assert cm._unit_similarity(left, right) == 0.9
    assert cm._unit_similarity(
        _unit(cm, "alpha beta"),
        _unit(cm, "beta gamma"),
    ) > 0

    assert cm._unit_adopted(left, "") is False
    assert cm._unit_adopted(_unit(cm, "company", ticker="NVDA"), "NVDA rallied") is True
    assert cm._unit_adopted(
        _unit(cm, "story", url="source-id-123"),
        "source source-id-123",
    ) is True
    assert cm._unit_adopted({"tokens": set()}, "something") is False
    assert cm._unit_adopted({"tokens": {"accelerator"}}, "accelerator demand") is True
    assert cm._unit_adopted({"tokens": {"alpha", "betabet"}}, "betabet appears") is True
    assert cm._unit_adopted({"tokens": {"alpha", "beta", "gamma"}}, "alpha and gamma") is True
    assert cm._unit_adopted({"tokens": {"alpha", "beta", "gamma"}}, "alpha only") is False


@pytest.mark.parametrize(
    ("stage", "total", "unique_ratio", "adoption_ratio", "score", "expected"),
    [
        ("p1", 0, 0, 0, 0, "no_output"),
        ("p4", 1, 1, 0.1, 80, "gate_by_event"),
        ("p75", 1, 0.1, 0.1, 80, "merge_or_gate"),
        ("p3", 1, 0.1, 0.1, 80, "merge_with_search_layers"),
        ("p1", 1, 0.5, 0.3, 20, "keep_core_collector"),
        ("p2", 1, 0.5, 0.3, 65, "keep"),
        ("p2", 1, 0.5, 0.3, 40, "keep_but_shrink"),
        ("p2", 1, 0.5, 0.3, 39, "candidate_to_prune"),
    ],
)
def test_contribution_recommendations(stage, total, unique_ratio, adoption_ratio, score, expected):
    import contribution_metrics as cm

    assert cm._recommendation(stage, total, unique_ratio, adoption_ratio, score) == expected


def test_contribution_scoring_summaries_and_markdown_extra_metrics():
    import contribution_metrics as cm

    assert cm._score_stage(0, 0, 0, None) == 0
    no_adoption = cm._score_stage(2, 2, 0, 300)
    with_adoption = cm._score_stage(2, 2, 1, 300)
    assert no_adoption < with_adoption

    units = [_unit(cm, f"unique accelerator phrase {index}") for index in range(5)]
    summary = cm._summarize_stage(
        "p1",
        units,
        [],
        p6_mapping="unique accelerator phrase 0 unique accelerator phrase 1",
        final_report="unique accelerator phrase 2 unique accelerator phrase 3 unique accelerator phrase 4",
        cost_seconds=10,
        extra_metrics={"queries": 5},
    )
    assert summary["unique_units"] == 5
    assert len(summary["sample_unique_units"]) == 3
    assert len(summary["sample_adopted_units"]) == 3
    assert summary["extra_metrics"] == {"queries": 5}

    report = {
        "generated_at": "2026-07-14",
        "layers": {
            "p1": summary,
            "p3": {},
            "p4": {},
            "p75": {},
        },
    }
    markdown = cm.render_layer_roi_markdown(report)
    assert "Extra metrics: queries=5" in markdown
    assert "## P75" in markdown
