from __future__ import annotations

import struct


def _write_base_dbf(path, records):
    fields = [
        ("GPDM", "C", 6, 0),
        ("GXRQ", "C", 8, 0),
        ("ZGB", "N", 18, 2),
        ("LTAG", "N", 18, 2),
    ]
    header_length = 32 + len(fields) * 32 + 1
    record_length = 1 + sum(field[2] for field in fields)
    payload = bytearray(header_length + record_length * len(records) + 1)
    payload[0] = 0x03
    struct.pack_into("<I", payload, 4, len(records))
    struct.pack_into("<H", payload, 8, header_length)
    struct.pack_into("<H", payload, 10, record_length)

    offset = 32
    for name, field_type, length, decimals in fields:
        encoded_name = name.encode("ascii")
        payload[offset : offset + len(encoded_name)] = encoded_name
        payload[offset + 11] = ord(field_type)
        payload[offset + 16] = length
        payload[offset + 17] = decimals
        offset += 32
    payload[offset] = 0x0D

    offset = header_length
    for code, updated_at, total_wan, float_wan in records:
        row = (
            b" "
            + f"{code:<6}".encode("ascii")
            + f"{updated_at:<8}".encode("ascii")
            + f"{total_wan:>18.2f}".encode("ascii")
            + f"{float_wan:>18.2f}".encode("ascii")
        )
        payload[offset : offset + record_length] = row
        offset += record_length
    payload[-1] = 0x1A
    path.parent.mkdir(parents=True)
    path.write_bytes(payload)


def test_load_tdx_capital_snapshot_reads_total_and_float_shares(tmp_path):
    from p8_extractors import load_tdx_capital_snapshot

    vipdoc = tmp_path / "vipdoc"
    vipdoc.mkdir()
    _write_base_dbf(
        tmp_path / "T0002" / "hq_cache" / "base.dbf",
        [
            ("688012", "20260630", 95784.22, 94219.70),
            ("002409", "20260331", 47592.76, 31852.27),
        ],
    )

    snapshot = load_tdx_capital_snapshot(str(vipdoc), ["688012"])

    assert set(snapshot) == {"688012"}
    assert snapshot["688012"]["total_shares"] == 957842200.0
    assert snapshot["688012"]["float_shares"] == 942197000.0
    assert snapshot["688012"]["updated_at"] == "20260630"
    assert snapshot["688012"]["source"] == "tdx_base_dbf"


def test_p8_populates_market_cap_tier_and_turnover(monkeypatch, tmp_path):
    import p8_technical

    captured = {}
    records = [
        {
            "date": f"2026{i + 1:04d}",
            "open": 17.0 + i * 0.01,
            "high": 17.5 + i * 0.01,
            "low": 16.5 + i * 0.01,
            "close": 17.0 + i * 0.01,
            "vol": 8_000_000 + i * 10_000,
        }
        for i in range(90)
    ]

    monkeypatch.setattr(p8_technical, "_find_tdx_vipdoc", lambda: str(tmp_path))
    monkeypatch.setattr(
        p8_technical,
        "_load_tdx_capital_snapshot",
        lambda _vipdoc, _codes: {
            "688012": {
                "total_shares": 1_000_000_000.0,
                "float_shares": 400_000_000.0,
                "updated_at": "20260630",
                "source": "tdx_base_dbf",
            }
        },
    )
    monkeypatch.setattr(
        p8_technical,
        "_resolve_tdx_market_and_path",
        lambda _vipdoc, code: ("sh", str(tmp_path / f"{code}.day")),
    )
    monkeypatch.setattr(p8_technical.os.path, "exists", lambda _path: True)
    monkeypatch.setattr(p8_technical, "read_day_file_simple", lambda _path: records)
    monkeypatch.setattr(
        p8_technical,
        "_persist_p8_outputs",
        lambda markdown, data, **kwargs: captured.update(
            markdown=markdown, data=data, kwargs=kwargs
        )
        or markdown,
    )

    p8_technical.p8_search_stock_prices(
        [("中微公司", "688012")],
        {"688012": "前道设备"},
        {"688012": "🔥高弹性"},
    )

    item = captured["data"]["688012"]
    assert item["market_cap_tier"] == "⚡中弹"
    assert item["finance_data"]["total_cap_yi"] == 178.9
    assert item["finance_data"]["liutong_cap_yi"] == 71.6
    assert item["tech_indicators"]["turnover_10d_avg"] > 0
    assert item["tech_indicators"]["turnover_latest_pct"] > 0
    assert "流通市值 71.6 亿元（⚡中弹）" in captured["markdown"]
    assert "换手：未提供" not in captured["markdown"]
    assert captured["kwargs"]["metrics"]["capital_records"] == 1


def test_p9_replaces_missing_market_cap_tier_from_p8():
    import p9_reporter

    payload = {
        "sniper_tables": [
            {
                "targets": [
                    {
                        "code": "688012",
                        "elasticity": "市值档位未提供；ATR 3.2%",
                    }
                ]
            }
        ]
    }
    p8_db = {
        "688012": {
            "market_cap_tier": "🔥高弹",
            "finance_data": {"liutong_cap_yi": 42.3},
            "tech_indicators": {"atr_pct": 3.2},
        }
    }

    metrics = p9_reporter._backfill_sniper_tables_from_p8(payload, p8_db)
    elasticity = payload["sniper_tables"][0]["targets"][0]["elasticity"]

    assert metrics["p8_backfilled_targets"] == 1
    assert elasticity == "🔥高弹（流通市值42.3亿元）；ATR 3.2%"
    assert "市值档位未提供" not in elasticity
