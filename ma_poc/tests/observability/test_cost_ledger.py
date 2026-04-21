"""Tests for cost_ledger — SQLite cost accumulator."""

from __future__ import annotations

import threading
from pathlib import Path

from ma_poc.observability.cost_ledger import CostLedger


def test_cost_record_llm_persists(tmp_path: Path) -> None:
    cl = CostLedger(tmp_path / "cost.db")
    cl.record_llm("p1", "entrata", "entrata:widget_api", 0.01, "gpt-4o-mini", 1000)
    total = cl.total()
    assert total["llm"] > 0
    cl.close()


def test_cost_rollup_by_pms_aggregates(tmp_path: Path) -> None:
    cl = CostLedger(tmp_path / "cost.db")
    cl.record_llm("p1", "entrata", "t1", 0.01, "gpt-4o", 100)
    cl.record_llm("p2", "entrata", "t1", 0.02, "gpt-4o", 200)
    cl.record_llm("p3", "rentcafe", "t2", 0.05, "gpt-4o", 500)
    rollup = cl.rollup_by_pms()
    assert rollup["entrata"]["llm"] == 0.03
    assert rollup["rentcafe"]["llm"] == 0.05
    cl.close()


def test_cost_total_sums_all_categories(tmp_path: Path) -> None:
    cl = CostLedger(tmp_path / "cost.db")
    cl.record_llm("p1", "ent", "t1", 0.10, "gpt", 100)
    cl.record_vision("p1", "ent", "t2", 0.05, "gpt-4o")
    total = cl.total()
    assert total["llm"] == 0.1
    assert total["vision"] == 0.05
    cl.close()


def test_cost_wasted_calls_identifies_zero_units_with_cost(tmp_path: Path) -> None:
    cl = CostLedger(tmp_path / "cost.db")
    cl.record_llm("p1", "ent", "t1", 0.01, "gpt", 100)
    wasted = cl.wasted_calls()
    # All LLM calls are "wasted" at the cost_ledger level (it doesn't track units)
    assert len(wasted) >= 1
    cl.close()


def test_cost_db_survives_reopen(tmp_path: Path) -> None:
    db = tmp_path / "cost.db"
    cl = CostLedger(db)
    cl.record_llm("p1", "ent", "t1", 0.01, "gpt", 100)
    cl.close()
    cl2 = CostLedger(db)
    total = cl2.total()
    assert total["llm"] == 0.01
    cl2.close()


def test_cost_concurrent_writes_do_not_corrupt(tmp_path: Path) -> None:
    cl = CostLedger(tmp_path / "cost.db")

    def writer(tid: int) -> None:
        for i in range(25):
            cl.record_llm(f"p{tid}_{i}", "pms", "t1", 0.001, "gpt", 10)

    threads = [threading.Thread(target=writer, args=(t,)) for t in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    total = cl.total()
    assert abs(total["llm"] - 0.1) < 0.001  # 100 * 0.001
    cl.close()


def test_cost_records_detail_as_json(tmp_path: Path) -> None:
    cl = CostLedger(tmp_path / "cost.db")
    cl.record_llm("p1", "ent", "t1", 0.01, "gpt-4o-mini", 500)
    rows = cl._conn.execute("SELECT detail FROM cost_entries").fetchall()
    import json

    detail = json.loads(rows[0]["detail"])
    assert detail["model"] == "gpt-4o-mini"
    assert detail["tokens"] == 500
    cl.close()
