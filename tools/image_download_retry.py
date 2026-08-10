"""Bounded retry support for authoritative body-image byte requests."""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from typing import Any


IMAGE_DOWNLOAD_MAX_ATTEMPTS = 3
IMAGE_DOWNLOAD_RETRY_DELAY_SECONDS = (1.0, 2.0)
RETRYABLE_IMAGE_ERROR_CODES = frozenset({"image_download_retryable"})


def is_retryable_image_error(code: str | None) -> bool:
    """Return whether a recorded image failure may cross the deferred branch."""

    return str(code or "") in RETRYABLE_IMAGE_ERROR_CODES


async def fetch_image_bytes_with_retry(
    fetcher: Callable[[], Awaitable[bytes | None]],
    *,
    logger: Any,
    label: str,
    max_attempts: int = IMAGE_DOWNLOAD_MAX_ATTEMPTS,
) -> tuple[bytes | None, int]:
    """Retry transient empty/timeout image responses with bounded backoff.

    Platform clients already log the underlying HTTP error. This helper adds a
    stable per-attempt retry trail while returning the total attempt count for
    the image manifest on both recovery and final failure.
    """

    if not 1 <= max_attempts <= IMAGE_DOWNLOAD_MAX_ATTEMPTS:
        raise ValueError(
            f"max_attempts must be between 1 and {IMAGE_DOWNLOAD_MAX_ATTEMPTS}"
        )

    for attempt in range(1, max_attempts + 1):
        timed_out = False
        try:
            content = await fetcher()
        except TimeoutError:
            content = None
            timed_out = True

        if content is not None:
            if attempt > 1:
                logger.info(
                    f"[image_download_retry_recovered] {label}, attempts={attempt}"
                )
            return content, attempt

        if attempt >= max_attempts:
            logger.error(
                f"[image_download_retry_exhausted] {label}, attempts={attempt}, "
                f"last_failure={'timeout' if timed_out else 'empty_response'}"
            )
            return None, attempt

        delay = random.uniform(*IMAGE_DOWNLOAD_RETRY_DELAY_SECONDS) * (
            2 ** (attempt - 1)
        )
        logger.warning(
            f"[image_download_retry] {label}, attempt={attempt}/{max_attempts}, "
            f"failure={'timeout' if timed_out else 'empty_response'}, "
            f"next_delay_seconds={delay:.3f}"
        )
        await asyncio.sleep(delay)

    raise AssertionError("image retry loop terminated unexpectedly")
