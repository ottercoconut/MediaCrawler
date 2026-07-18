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
        resume_page=3,
        resume_cursor="next-search-id",
        batch_complete=True,
        discovery_phase="frontier",
    )

    event = json.loads(state_path.read_text(encoding="utf-8"))["events"][0]
    assert stopped is False
    assert event["details"]["source_page"] == 2
    assert event["details"]["source_cursor"] == "search-id"
    assert event["details"]["source_has_more"] is True
    assert event["details"]["raw_batch_count"] == 20
    assert event["details"]["resume_page"] == 3
    assert event["details"]["resume_cursor"] == "next-search-id"
    assert event["details"]["batch_complete"] is True


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


def test_known_identity_can_be_skipped_before_detail_fetch() -> None:
    accumulator = AdaptiveAccumulator(
        platform="douyin",
        hard_limit=10,
        target_new=1,
        max_stagnant_batches=3,
        existing_identities={"known-aweme"},
    )

    assert accumulator.is_known("known-aweme") is True
    assert accumulator.candidate_count == 0
    assert accumulator.is_known("new-aweme") is False


def test_xhs_persisted_seen_candidate_is_loaded_before_detail(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "xhs-seen.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE web_posts (platform_key TEXT, platform_post_id TEXT, canonical_url TEXT)"
        )
        conn.execute(
            """
            CREATE TABLE xhs_discovery_seen_candidates (
                target_key TEXT,
                account_id TEXT,
                query_fingerprint TEXT,
                platform_post_id TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO xhs_discovery_seen_candidates VALUES (?, ?, ?, ?)",
            ("qingdao_travel", "xhs-a01", "fingerprint", "seen-invalid-note"),
        )
    monkeypatch.setenv("TRIPPOSTCOLLECT_DB_PATH", str(db_path))
    monkeypatch.setenv("TRIPPOSTCOLLECT_XHS_DISCOVERY_TARGET_KEY", "qingdao_travel")
    monkeypatch.setenv("TRIPPOSTCOLLECT_XHS_ACCOUNT_ID", "xhs-a01")
    monkeypatch.setenv(
        "TRIPPOSTCOLLECT_XHS_DISCOVERY_QUERY_FINGERPRINT",
        "fingerprint",
    )

    accumulator = AdaptiveAccumulator.from_environment("xhs", hard_limit=10)

    assert accumulator.is_known("seen-invalid-note") is True


def test_refresh_batch_does_not_consume_frontier_stagnation(monkeypatch) -> None:
    monkeypatch.setattr(
        "tools.trippostcollect_adaptive.append_execution_event",
        lambda *args, **kwargs: None,
    )
    accumulator = AdaptiveAccumulator(
        platform="weibo",
        hard_limit=10,
        target_new=2,
        max_stagnant_batches=1,
    )
    accumulator.begin_batch()

    stopped = accumulator.finish_batch(
        source_page=1,
        resume_page=2,
        batch_complete=True,
        discovery_phase="refresh",
        count_stagnation=False,
    )

    assert stopped is False
    assert accumulator.stagnant_batches == 0
