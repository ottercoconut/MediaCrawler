from __future__ import annotations

import json

from tools.trippostcollect_adaptive import AdaptiveAccumulator


def test_batch_event_records_pagination_metadata(monkeypatch, tmp_path) -> None:
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({"events": []}), encoding="utf-8")
    monkeypatch.setenv("TRIPPOSTCOLLECT_EXECUTION_STATE_PATH", str(state_path))
    accumulator = AdaptiveAccumulator(
        platform="xhs",
        hard_limit=100,
        target=50,
        max_stagnant_batches=3,
    )

    accumulator.begin_batch()
    accumulator.consider("note-1", valid=True)
    stopped = accumulator.finish_batch(
        source_page=2,
        source_cursor="search-id",
        source_has_more=True,
        raw_batch_count=20,
    )

    event = json.loads(state_path.read_text(encoding="utf-8"))["events"][0]
    assert stopped is False
    assert event["details"]["source_page"] == 2
    assert event["details"]["source_cursor"] == "search-id"
    assert event["details"]["source_has_more"] is True
    assert event["details"]["raw_batch_count"] == 20


def test_runtime_failure_has_distinct_stop_reason(monkeypatch, tmp_path) -> None:
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({"events": []}), encoding="utf-8")
    monkeypatch.setenv("TRIPPOSTCOLLECT_EXECUTION_STATE_PATH", str(state_path))
    accumulator = AdaptiveAccumulator(
        platform="douyin",
        hard_limit=100,
        target=50,
        max_stagnant_batches=3,
    )

    accumulator.mark_runtime_failed(
        "search_request_failed",
        source_page=3,
        source_cursor="cursor-2",
    )

    event = json.loads(state_path.read_text(encoding="utf-8"))["events"][0]
    assert event["details"]["stop_reason"] == "runtime_failed"
    assert event["details"]["stop_detail"] == "search_request_failed"
    assert event["details"]["source_page"] == 3
