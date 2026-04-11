# -*- coding: utf-8 -*-
"""Offline tests for layer ROI metrics."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_build_layer_roi_report_counts_p4_adoption():
    from contribution_metrics import build_layer_roi_report

    p1_results = [
        {
            "category": "RSS",
            "query": "[RSS] Example",
            "text": "1. [Example] Memory pricing rises 《example|https://example.com/a|2026-04-12》",
        }
    ]
    p3_raw_results = {
        "jinnang_2": [
            {
                "title": "Optical module demand accelerates",
                "track": "CPO",
                "summary": "Demand expands into upstream materials",
                "source_url": "https://example.com/p3",
            }
        ]
    }
    p4_text = "- [Catalyst] NVIDIA(NVDA) [Super giant]: $120.00, 区间涨跌 +6.20%"

    report = build_layer_roi_report(
        p1_results=p1_results,
        p3_raw_results=p3_raw_results,
        p4_text=p4_text,
        p75_text="- Backup line without adoption",
        p6_mapping="NVIDIA NVDA +6.20% maps into A-share server names",
        final_report="核心信号: NVIDIA NVDA +6.20% 继续强化服务器链条",
        timings={"p1": 12.0, "p3": 30.0, "p4": 8.0, "p75": 15.0},
    )

    p4_metrics = report["layers"]["p4"]
    assert p4_metrics["adopted_units"] >= 1
    assert p4_metrics["p9_hits"] >= 1
    assert p4_metrics["recommendation"] in {"keep", "keep_but_shrink", "gate_by_event"}


def test_build_layer_roi_report_flags_low_value_p75():
    from contribution_metrics import build_layer_roi_report

    report = build_layer_roi_report(
        p3_raw_results={
            "jinnang_2": [
                {
                    "title": "HBM substrate shortage",
                    "track": "HBM",
                    "summary": "Substrate tightness hurts smaller packaging vendors",
                    "source_url": "https://example.com/hbm",
                }
            ]
        },
        p75_text="- HBM substrate shortage hurts smaller packaging vendors",
        p6_mapping="",
        final_report="",
        timings={"p3": 40.0, "p75": 35.0},
    )

    p75_metrics = report["layers"]["p75"]
    assert p75_metrics["recommendation"] in {"merge_or_gate", "candidate_to_prune"}
    assert p75_metrics["adopted_units"] == 0


def test_render_layer_roi_markdown_contains_stage_rows():
    from contribution_metrics import build_layer_roi_report, render_layer_roi_markdown

    report = build_layer_roi_report(
        p1_results=[
            {
                "category": "RSS",
                "query": "[RSS] Example",
                "text": "1. [Example] AI server shipment rises 《example|https://example.com/1|2026-04-12》",
            }
        ],
        p6_mapping="AI server shipment rises",
        final_report="AI server shipment rises",
        timings={"p1": 10.0},
    )

    markdown = render_layer_roi_markdown(report)
    assert "| P1 |" in markdown
    assert "## P75" in markdown
