# -*- coding: utf-8 -*-
"""Regression tests for P2/P3 structured semantic-empty guards."""


def test_parse_p2_structured_items_rejects_sentinel_claim():
    from intel_structuring import parse_p2_structured_items

    raw_text = """
<x_leaks>
### [HBM] Supply update
- **信源**: source
- **核心内容**: 暂无相关证据。
- **可信度**: 高
</x_leaks>
"""

    assert parse_p2_structured_items(raw_text) == []


def test_normalize_p3_structured_items_rejects_all_sentinel_core_fields():
    from intel_structuring import normalize_p3_structured_items

    items = normalize_p3_structured_items(
        {
            "jinnang_1": [
                {
                    "title": "暂无",
                    "summary": "未找到相关信息。",
                    "claim": "No relevant evidence found.",
                }
            ]
        }
    )

    assert items == []


def test_normalize_p3_structured_items_keeps_meaningful_explicit_claim():
    from intel_structuring import normalize_p3_structured_items

    items = normalize_p3_structured_items(
        {
            "jinnang_1": [
                {
                    "title": "暂无",
                    "summary": "暂无数据",
                    "claim": "HBM substrate lead times expanded in July.",
                }
            ]
        }
    )

    assert len(items) == 1
    assert items[0]["claim"] == "HBM substrate lead times expanded in July."


def test_checkpoint_structured_claim_count_rejects_sentinel_values():
    import checkpoint

    p2_usable, p2_count = checkpoint._p2_markdown_has_intel(
        "# P2\n\n- Claim: 暂无相关证据。",
        {"structured_items": 1},
    )
    p2_raw_usable, p2_raw_count = checkpoint._p2_markdown_has_intel(
        """
<x_leaks>
### [HBM] Supply update
- **信源**: source
- **核心内容**: 暂无相关证据。
</x_leaks>
""",
        {"structured_items": 1},
    )
    p3_usable, p3_count = checkpoint._p3_markdown_has_intel(
        "# P3\n\n- Claim: No relevant evidence found.",
        manifest_version=3,
    )

    assert (p2_usable, p2_count) == (False, 0)
    assert (p2_raw_usable, p2_raw_count) == (False, 0)
    assert (p3_usable, p3_count) == (False, 0)


def test_checkpoint_structured_claim_count_keeps_real_claims():
    import checkpoint

    markdown = "# Intel\n\n- Claim: HBM substrate lead times expanded in July."

    assert checkpoint._p2_markdown_has_intel(markdown, {"structured_items": 1}) == (True, 1)
    assert checkpoint._p3_markdown_has_intel(markdown, manifest_version=3) == (True, 1)
