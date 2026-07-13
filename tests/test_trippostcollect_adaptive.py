from __future__ import annotations

import json
import sqlite3

from tools.trippostcollect_adaptive import AdaptiveAccumulator


def test_batch_event_records_pagination_metadata(monkeypatch, tmp_path) -> None:
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({"events": []}), encoding="utf-8")
    monkeypatch.setenv("TRIPPOSTCOLLECT_EXECUTION_STATE_PATH", str(state_path))
    accumulator = AdaptiveAccumulator(
        platform="xhs",
        hard_limit=100,
        target_new=50,
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
        target_new=50,
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


def test_existing_database_identity_does_not_advance_new_target(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "posts.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE web_posts (platform_key TEXT, platform_post_id TEXT, canonical_url TEXT)"
        )
        conn.execute("INSERT INTO web_posts VALUES ('xhs', 'existing-note', NULL)")
    monkeypatch.setenv("TRIPPOSTCOLLECT_DB_PATH", str(db_path))
    monkeypatch.setenv("TRIPPOSTCOLLECT_TARGET_NEW_POSTS", "1")

    accumulator = AdaptiveAccumulator.from_environment("xhs", hard_limit=10)
    accumulator.begin_batch()

    assert accumulator.consider("existing-note", valid=True) is False
    assert len(accumulator.new_valid_identities) == 0
    assert len(accumulator.existing_valid_identities) == 1
    assert accumulator.consider("new-note", valid=True) is True
    assert accumulator.stop_reason == "target_new_met"


def test_resume_identity_does_not_advance_continuation_target(monkeypatch, tmp_path) -> None:
    resume_path = tmp_path / "resume.json"
    resume_path.write_text(json.dumps(["prior-note"]), encoding="utf-8")
    monkeypatch.delenv("TRIPPOSTCOLLECT_DB_PATH", raising=False)
    monkeypatch.setenv("TRIPPOSTCOLLECT_RESUME_IDENTITIES_PATH", str(resume_path))
    monkeypatch.setenv("TRIPPOSTCOLLECT_TARGET_NEW_POSTS", "1")

    accumulator = AdaptiveAccumulator.from_environment("xhs", hard_limit=10)
    accumulator.begin_batch()

    assert accumulator.consider("prior-note", valid=True) is False
    assert accumulator.consider("new-note", valid=True) is True
    assert len(accumulator.existing_valid_identities) == 1
    assert len(accumulator.new_valid_identities) == 1
