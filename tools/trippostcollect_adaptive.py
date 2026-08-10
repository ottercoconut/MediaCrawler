"""TripPostCollect adaptive candidate accounting for MediaCrawler search loops."""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


def env_int(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return max(0, default)


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def existing_platform_identities(platform: str) -> set[str]:
    db_value = os.environ.get("TRIPPOSTCOLLECT_DB_PATH", "").strip()
    rows = []
    if db_value:
        try:
            with sqlite3.connect(Path(db_value).expanduser()) as conn:
                rows = conn.execute(
                    """
                    SELECT platform_post_id, canonical_url
                    FROM web_posts
                    WHERE platform_key=?
                    """,
                    (platform,),
                ).fetchall()
                if platform == "xhs":
                    target_key = os.environ.get(
                        "TRIPPOSTCOLLECT_XHS_DISCOVERY_TARGET_KEY",
                        "",
                    ).strip()
                    account_id = os.environ.get(
                        "TRIPPOSTCOLLECT_XHS_ACCOUNT_ID",
                        "",
                    ).strip()
                    fingerprint = os.environ.get(
                        "TRIPPOSTCOLLECT_XHS_DISCOVERY_QUERY_FINGERPRINT",
                        "",
                    ).strip()
                    if target_key and account_id and fingerprint:
                        try:
                            seen_rows = conn.execute(
                                """
                                SELECT platform_post_id
                                FROM xhs_discovery_seen_candidates
                                WHERE target_key=? AND account_id=? AND query_fingerprint=?
                                """,
                                (target_key, account_id, fingerprint),
                            ).fetchall()
                        except sqlite3.Error:
                            seen_rows = []
                        rows.extend(
                            (platform_post_id, None)
                            for (platform_post_id,) in seen_rows
                        )
                else:
                    job_id = os.environ.get(
                        "TRIPPOSTCOLLECT_DISCOVERY_JOB_ID",
                        "",
                    ).strip()
                    fingerprint = os.environ.get(
                        "TRIPPOSTCOLLECT_DISCOVERY_QUERY_FINGERPRINT",
                        "",
                    ).strip()
                    if job_id and fingerprint:
                        try:
                            seen_rows = conn.execute(
                                """
                                SELECT platform_post_id
                                FROM crawl_discovery_seen_candidates
                                WHERE job_id=? AND platform_key=? AND query_fingerprint=?
                                """,
                                (int(job_id), platform, fingerprint),
                            ).fetchall()
                        except (ValueError, sqlite3.Error):
                            seen_rows = []
                        rows.extend(
                            (platform_post_id, None)
                            for (platform_post_id,) in seen_rows
                        )
        except (OSError, sqlite3.Error):
            rows = []
    identities: set[str] = set()
    for platform_post_id, canonical_url in rows:
        if platform_post_id:
            identities.add(str(platform_post_id))
        if canonical_url:
            path_parts = [part for part in urlparse(str(canonical_url)).path.split("/") if part]
            if path_parts:
                identities.add(path_parts[-1])
    resume_value = os.environ.get("TRIPPOSTCOLLECT_RESUME_IDENTITIES_PATH", "").strip()
    if resume_value:
        try:
            resume_identities = json.loads(Path(resume_value).expanduser().read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            resume_identities = []
        identities.update(str(value) for value in resume_identities if value not in (None, ""))
    return identities


def append_execution_event(event_type: str, details: dict[str, Any]) -> None:
    state_value = os.environ.get("TRIPPOSTCOLLECT_EXECUTION_STATE_PATH", "").strip()
    if not state_value:
        return
    path = Path(state_value)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload.setdefault("events", []).append({"at": _utc_iso(), "type": event_type, "details": details})
        payload["updated_at"] = _utc_iso()
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)
    except (OSError, json.JSONDecodeError, TypeError):
        return


@dataclass
class AdaptiveAccumulator:
    platform: str
    hard_limit: int
    target_new: int
    max_stagnant_batches: int
    completion_mode: str = "target-new-posts"
    stagnation_basis: str = "valid_new"
    candidate_count: int = 0
    existing_identities: set[str] = field(default_factory=set)
    seen_candidate_identities: set[str] = field(default_factory=set)
    new_valid_identities: set[str] = field(default_factory=set)
    existing_valid_identities: set[str] = field(default_factory=set)
    stagnant_batches: int = 0
    batch_no: int = 0
    stop_reason: str = ""
    last_source_page: int | str | None = None
    last_source_offset: int | None = None
    last_source_cursor: int | str | None = None
    last_next_cursor: int | str | None = None
    last_source_has_more: bool | int | None = None
    last_raw_batch_count: int | None = None
    last_resume_page: int | str | None = None
    last_resume_offset: int | None = None
    last_resume_cursor: int | str | None = None
    last_batch_complete: bool = False
    last_discovery_phase: str = "frontier"
    stop_detail: str = ""
    deferred_image_failures: list[dict[str, Any]] = field(default_factory=list)
    deferred_image_identities: set[str] = field(default_factory=set)
    _deferred_resume: dict[str, Any] | None = None
    _batch_new_before: int = 0
    _batch_candidate_before: int = 0

    @classmethod
    def from_environment(cls, platform: str, hard_limit: int) -> "AdaptiveAccumulator":
        completion_mode = os.environ.get(
            "TRIPPOSTCOLLECT_COMPLETION_MODE",
            "target-new-posts",
        ).strip()
        if completion_mode not in {"target-new-posts", "source-exhausted"}:
            completion_mode = "target-new-posts"
        return cls(
            platform=platform,
            hard_limit=max(1, hard_limit),
            target_new=max(1, env_int("TRIPPOSTCOLLECT_TARGET_NEW_POSTS", hard_limit)),
            max_stagnant_batches=max(1, env_int("TRIPPOSTCOLLECT_MAX_STAGNANT_BATCHES", 3)),
            completion_mode=completion_mode,
            stagnation_basis=(
                "candidate_identity" if platform == "weibo" else "valid_new"
            ),
            existing_identities=existing_platform_identities(platform),
        )

    @property
    def exhaustion_mode(self) -> bool:
        return self.completion_mode == "source-exhausted"

    @property
    def can_continue(self) -> bool:
        return bool(
            not self.stop_reason
            and (self.exhaustion_mode or self.candidate_count < self.hard_limit)
        )

    def begin_batch(self) -> None:
        self.batch_no += 1
        self._batch_new_before = len(self.new_valid_identities)
        self._batch_candidate_before = len(self.seen_candidate_identities)

    def is_known(self, identity: str) -> bool:
        return bool(
            identity
            and (
                identity in self.existing_identities
                or identity in self.seen_candidate_identities
                or identity in self.deferred_image_identities
            )
        )

    def consider(self, identity: str, *, valid: bool) -> bool:
        if not self.exhaustion_mode and self.candidate_count >= self.hard_limit:
            self.stop_reason = (
                "deferred_retry_pending"
                if self.deferred_image_failures
                else "candidate_hard_limit_reached"
            )
            return True
        self.candidate_count += 1
        if identity:
            self.seen_candidate_identities.add(identity)
        if valid and identity:
            if identity in self.existing_identities:
                self.existing_valid_identities.add(identity)
            else:
                self.new_valid_identities.add(identity)
        if not self.exhaustion_mode and len(self.new_valid_identities) >= self.target_new:
            self.stop_reason = "target_new_met"
            return True
        if not self.exhaustion_mode and self.candidate_count >= self.hard_limit:
            self.stop_reason = (
                "deferred_retry_pending"
                if self.deferred_image_failures
                else "candidate_hard_limit_reached"
            )
            return True
        return False

    def defer_image_failure(
        self,
        identity: str,
        *,
        detail: str,
        error_code: str,
        attempts: int,
        source_index: int | None = None,
        source_page: int | str | None = None,
        source_offset: int | None = None,
        source_cursor: int | str | None = None,
        discovery_phase: str = "frontier",
    ) -> bool:
        """Record an image-failed candidate without crossing its safe frontier."""

        if not self.exhaustion_mode and self.candidate_count >= self.hard_limit:
            self.stop_reason = (
                "deferred_retry_pending"
                if self.deferred_image_failures
                else "candidate_hard_limit_reached"
            )
            return True
        self.candidate_count += 1
        failure = {
            "platform": self.platform,
            "identity": identity,
            "detail": detail,
            "error_code": error_code,
            "attempts": max(1, int(attempts)),
            "source_index": source_index,
            "source_page": source_page,
            "source_offset": source_offset,
            "source_cursor": source_cursor,
            "discovery_phase": discovery_phase,
        }
        failure["retryable"] = error_code == "image_download_retryable"
        self.deferred_image_failures.append(failure)
        if identity:
            self.deferred_image_identities.add(identity)
        if self._deferred_resume is None:
            self._deferred_resume = {
                "resume_page": source_page,
                "resume_offset": source_offset,
                "resume_cursor": source_cursor,
                "discovery_phase": discovery_phase,
            }
        append_execution_event("candidate_deferred", failure)
        if not self.exhaustion_mode and self.candidate_count >= self.hard_limit:
            self.stop_reason = "deferred_retry_pending"
            return True
        return False

    def _record_source(
        self,
        *,
        source_page: int | str | None = None,
        source_offset: int | None = None,
        source_cursor: int | str | None = None,
        next_cursor: int | str | None = None,
        source_has_more: bool | int | None = None,
        raw_batch_count: int | None = None,
        resume_page: int | str | None = None,
        resume_offset: int | None = None,
        resume_cursor: int | str | None = None,
        batch_complete: bool = False,
        discovery_phase: str = "frontier",
    ) -> None:
        self.last_source_page = source_page
        self.last_source_offset = source_offset
        self.last_source_cursor = source_cursor
        self.last_next_cursor = next_cursor
        self.last_source_has_more = source_has_more
        self.last_raw_batch_count = raw_batch_count
        self.last_resume_page = resume_page
        self.last_resume_offset = resume_offset
        self.last_resume_cursor = resume_cursor
        self.last_batch_complete = batch_complete
        self.last_discovery_phase = discovery_phase

    def finish_batch(
        self,
        *,
        source_page: int | str | None = None,
        source_offset: int | None = None,
        source_cursor: int | str | None = None,
        next_cursor: int | str | None = None,
        source_has_more: bool | int | None = None,
        raw_batch_count: int | None = None,
        resume_page: int | str | None = None,
        resume_offset: int | None = None,
        resume_cursor: int | str | None = None,
        batch_complete: bool = False,
        discovery_phase: str = "frontier",
        count_stagnation: bool = True,
    ) -> bool:
        self._record_source(
            source_page=source_page,
            source_offset=source_offset,
            source_cursor=source_cursor,
            next_cursor=next_cursor,
            source_has_more=source_has_more,
            raw_batch_count=raw_batch_count,
            resume_page=resume_page,
            resume_offset=resume_offset,
            resume_cursor=resume_cursor,
            batch_complete=batch_complete,
            discovery_phase=discovery_phase,
        )
        added = len(self.new_valid_identities) - self._batch_new_before
        candidate_identities_added = len(self.seen_candidate_identities) - self._batch_candidate_before
        stagnation_progress = (
            candidate_identities_added
            if self.stagnation_basis == "candidate_identity"
            else added
        )
        if count_stagnation:
            self.stagnant_batches = (
                self.stagnant_batches + 1 if stagnation_progress == 0 else 0
            )
        if (
            not self.stop_reason
            and
            count_stagnation
            and not self.exhaustion_mode
            and self.stagnant_batches >= self.max_stagnant_batches
        ):
            self.stop_reason = (
                "deferred_retry_pending"
                if self.deferred_image_failures
                else "stagnated"
            )
        details = {
            "platform": self.platform,
            "batch_no": self.batch_no,
            "candidate_count": self.candidate_count,
            "valid_new_count": len(self.new_valid_identities),
            "valid_existing_count": len(self.existing_valid_identities),
            "batch_new_count": added,
            "batch_candidate_identity_count": candidate_identities_added,
            "stagnant_batches": self.stagnant_batches,
            "stagnation_basis": self.stagnation_basis,
            "target_new": self.target_new,
            "hard_limit": self.hard_limit,
            "completion_mode": self.completion_mode,
            "quantity_limits_enforced": not self.exhaustion_mode,
            "stop_reason": self.stop_reason or "continue",
            "source_page": source_page,
            "source_offset": source_offset,
            "source_cursor": source_cursor,
            "next_cursor": next_cursor,
            "source_has_more": source_has_more,
            "raw_batch_count": raw_batch_count,
            "resume_page": resume_page,
            "resume_offset": resume_offset,
            "resume_cursor": resume_cursor,
            "batch_complete": batch_complete,
            "discovery_phase": discovery_phase,
            "candidate_identities": sorted(self.seen_candidate_identities),
        }
        append_execution_event("adaptive_batch_completed", details)
        if self.stop_reason:
            append_execution_event("adaptive_search_stopped", self.summary())
            return True
        return False

    def mark_source_exhausted(
        self,
        detail: str,
        *,
        source_page: int | str | None = None,
        source_offset: int | None = None,
        source_cursor: int | str | None = None,
        next_cursor: int | str | None = None,
        source_has_more: bool | int | None = None,
        raw_batch_count: int | None = None,
        resume_page: int | str | None = None,
        resume_offset: int | None = None,
        resume_cursor: int | str | None = None,
        batch_complete: bool = True,
        discovery_phase: str = "frontier",
    ) -> None:
        self._record_source(
            source_page=source_page,
            source_offset=source_offset,
            source_cursor=source_cursor,
            next_cursor=next_cursor,
            source_has_more=source_has_more,
            raw_batch_count=raw_batch_count,
            resume_page=resume_page,
            resume_offset=resume_offset,
            resume_cursor=resume_cursor,
            batch_complete=batch_complete,
            discovery_phase=discovery_phase,
        )
        if not self.stop_reason or (
            self.stop_reason == "stagnated" and self.deferred_image_failures
        ):
            self.stop_reason = (
                "deferred_retry_pending"
                if self.deferred_image_failures
                else "source_exhausted"
            )
        self.stop_detail = (
            "image_candidate_failures"
            if self.deferred_image_failures
            else detail
        )
        append_execution_event("adaptive_search_stopped", self.summary())

    def mark_runtime_failed(
        self,
        detail: str,
        *,
        source_page: int | str | None = None,
        source_offset: int | None = None,
        source_cursor: int | str | None = None,
        resume_page: int | str | None = None,
        resume_offset: int | None = None,
        resume_cursor: int | str | None = None,
        discovery_phase: str = "frontier",
    ) -> None:
        self._record_source(
            source_page=source_page,
            source_offset=source_offset,
            source_cursor=source_cursor,
            resume_page=resume_page,
            resume_offset=resume_offset,
            resume_cursor=resume_cursor,
            batch_complete=False,
            discovery_phase=discovery_phase,
        )
        if not self.stop_reason:
            self.stop_reason = "runtime_failed"
        self.stop_detail = detail
        append_execution_event("adaptive_search_stopped", self.summary())

    def summary(self) -> dict[str, Any]:
        result = {
            "platform": self.platform,
            "candidate_count": self.candidate_count,
            "valid_new_count": len(self.new_valid_identities),
            "valid_existing_count": len(self.existing_valid_identities),
            "target_new": self.target_new,
            "hard_limit": self.hard_limit,
            "completion_mode": self.completion_mode,
            "quantity_limits_enforced": not self.exhaustion_mode,
            "stagnant_batches": self.stagnant_batches,
            "stagnation_basis": self.stagnation_basis,
            "stop_reason": self.stop_reason or "running",
            "pages_fetched": self.batch_no,
            "source_page": self.last_source_page,
            "source_offset": self.last_source_offset,
            "source_cursor": self.last_source_cursor,
            "next_cursor": self.last_next_cursor,
            "source_has_more": self.last_source_has_more,
            "raw_batch_count": self.last_raw_batch_count,
            "resume_page": self.last_resume_page,
            "resume_offset": self.last_resume_offset,
            "resume_cursor": self.last_resume_cursor,
            "batch_complete": self.last_batch_complete,
            "discovery_phase": self.last_discovery_phase,
            "stop_detail": self.stop_detail,
        }
        if self._deferred_resume is not None:
            result.update(self._deferred_resume)
            result["batch_complete"] = False
            result["source_has_more"] = True
        result["deferred_image_count"] = len(
            self.deferred_image_failures
        )
        result["deferred_image_failures"] = list(
            self.deferred_image_failures
        )
        result["candidate_identities"] = sorted(self.seen_candidate_identities)
        return result


def should_reseed_douyin_frontier(
    *,
    saved_source_exhausted: bool,
    refresh_has_more: bool | int | None,
    refresh_next_cursor: str | None,
    refresh_new_candidate_count: int,
) -> bool:
    """Start a new cursor epoch only when refresh proves new identities and continuation."""
    return bool(
        saved_source_exhausted
        and refresh_has_more in (True, 1)
        and str(refresh_next_cursor or "").strip()
        and refresh_new_candidate_count > 0
    )
