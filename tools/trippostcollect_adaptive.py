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
    stop_detail: str = ""
    _batch_new_before: int = 0
    _batch_candidate_before: int = 0

    @classmethod
    def from_environment(cls, platform: str, hard_limit: int) -> "AdaptiveAccumulator":
        return cls(
            platform=platform,
            hard_limit=max(1, hard_limit),
            target_new=max(1, env_int("TRIPPOSTCOLLECT_TARGET_NEW_POSTS", hard_limit)),
            max_stagnant_batches=max(1, env_int("TRIPPOSTCOLLECT_MAX_STAGNANT_BATCHES", 3)),
            existing_identities=existing_platform_identities(platform),
        )

    def begin_batch(self) -> None:
        self.batch_no += 1
        self._batch_new_before = len(self.new_valid_identities)
        self._batch_candidate_before = len(self.seen_candidate_identities)

    def consider(self, identity: str, *, valid: bool) -> bool:
        if self.candidate_count >= self.hard_limit:
            self.stop_reason = "candidate_hard_limit_reached"
            return True
        self.candidate_count += 1
        if identity:
            self.seen_candidate_identities.add(identity)
        if valid and identity:
            if identity in self.existing_identities:
                self.existing_valid_identities.add(identity)
            else:
                self.new_valid_identities.add(identity)
        if len(self.new_valid_identities) >= self.target_new:
            self.stop_reason = "target_new_met"
            return True
        if self.candidate_count >= self.hard_limit:
            self.stop_reason = "candidate_hard_limit_reached"
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
    ) -> None:
        self.last_source_page = source_page
        self.last_source_offset = source_offset
        self.last_source_cursor = source_cursor
        self.last_next_cursor = next_cursor
        self.last_source_has_more = source_has_more
        self.last_raw_batch_count = raw_batch_count

    def finish_batch(
        self,
        *,
        source_page: int | str | None = None,
        source_offset: int | None = None,
        source_cursor: int | str | None = None,
        next_cursor: int | str | None = None,
        source_has_more: bool | int | None = None,
        raw_batch_count: int | None = None,
    ) -> bool:
        self._record_source(
            source_page=source_page,
            source_offset=source_offset,
            source_cursor=source_cursor,
            next_cursor=next_cursor,
            source_has_more=source_has_more,
            raw_batch_count=raw_batch_count,
        )
        added = len(self.new_valid_identities) - self._batch_new_before
        candidate_identities_added = len(self.seen_candidate_identities) - self._batch_candidate_before
        self.stagnant_batches = self.stagnant_batches + 1 if candidate_identities_added == 0 else 0
        if self.stagnant_batches >= self.max_stagnant_batches:
            self.stop_reason = "stagnated"
        details = {
            "platform": self.platform,
            "batch_no": self.batch_no,
            "candidate_count": self.candidate_count,
            "valid_new_count": len(self.new_valid_identities),
            "valid_existing_count": len(self.existing_valid_identities),
            "batch_new_count": added,
            "batch_candidate_identity_count": candidate_identities_added,
            "stagnant_batches": self.stagnant_batches,
            "target_new": self.target_new,
            "hard_limit": self.hard_limit,
            "stop_reason": self.stop_reason or "continue",
            "source_page": source_page,
            "source_offset": source_offset,
            "source_cursor": source_cursor,
            "next_cursor": next_cursor,
            "source_has_more": source_has_more,
            "raw_batch_count": raw_batch_count,
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
    ) -> None:
        self._record_source(
            source_page=source_page,
            source_offset=source_offset,
            source_cursor=source_cursor,
            next_cursor=next_cursor,
            source_has_more=source_has_more,
            raw_batch_count=raw_batch_count,
        )
        if not self.stop_reason:
            self.stop_reason = "source_exhausted"
        self.stop_detail = detail
        append_execution_event("adaptive_search_stopped", self.summary())

    def mark_runtime_failed(
        self,
        detail: str,
        *,
        source_page: int | str | None = None,
        source_offset: int | None = None,
        source_cursor: int | str | None = None,
    ) -> None:
        self._record_source(
            source_page=source_page,
            source_offset=source_offset,
            source_cursor=source_cursor,
        )
        if not self.stop_reason:
            self.stop_reason = "runtime_failed"
        self.stop_detail = detail
        append_execution_event("adaptive_search_stopped", self.summary())

    def summary(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "candidate_count": self.candidate_count,
            "valid_new_count": len(self.new_valid_identities),
            "valid_existing_count": len(self.existing_valid_identities),
            "target_new": self.target_new,
            "hard_limit": self.hard_limit,
            "stagnant_batches": self.stagnant_batches,
            "stop_reason": self.stop_reason or "running",
            "pages_fetched": self.batch_no,
            "source_page": self.last_source_page,
            "source_offset": self.last_source_offset,
            "source_cursor": self.last_source_cursor,
            "next_cursor": self.last_next_cursor,
            "source_has_more": self.last_source_has_more,
            "raw_batch_count": self.last_raw_batch_count,
            "stop_detail": self.stop_detail,
        }
