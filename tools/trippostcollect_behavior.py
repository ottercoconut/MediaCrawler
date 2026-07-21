"""Bridge MediaCrawler browser pages into TripPostCollect's required behavior stage."""

from __future__ import annotations

import os
import json
import sys
from pathlib import Path
from typing import Any

from playwright.async_api import BrowserContext, Page


def _enabled() -> bool:
    return os.environ.get("TRIPPOSTCOLLECT_HUMAN_BEHAVIOR_ENABLED", "").strip() == "1"


def project_browser_args() -> list[str]:
    try:
        value = json.loads(os.environ.get("TRIPPOSTCOLLECT_BROWSER_ARGS_JSON", "[]"))
    except json.JSONDecodeError:
        return []
    args = [str(item) for item in value] if isinstance(value, list) else []
    if not any(item.startswith("--lang=") for item in args):
        args.append("--lang=zh-CN")
    return args


async def install_project_runtime_hints(context: BrowserContext) -> None:
    if not _enabled():
        return
    scripts_dir = Path(os.environ.get("TRIPPOSTCOLLECT_PROJECT_SCRIPTS", "")).expanduser()
    if not scripts_dir.is_dir():
        raise RuntimeError("required TripPostCollect runtime hint configuration is incomplete")
    scripts_value = str(scripts_dir.resolve())
    if scripts_value not in sys.path:
        sys.path.insert(0, scripts_value)
    from human_flow import install_runtime_hints

    await install_runtime_hints(context)


async def run_required_human_behavior(page: Page, platform_key: str) -> dict[str, Any]:
    if not _enabled():
        return {"status": "disabled", "platform": platform_key}

    scripts_dir = Path(os.environ.get("TRIPPOSTCOLLECT_PROJECT_SCRIPTS", "")).expanduser()
    evidence_path = os.environ.get("TRIPPOSTCOLLECT_HUMAN_BEHAVIOR_EVIDENCE", "").strip()
    profile_name = os.environ.get("TRIPPOSTCOLLECT_HUMAN_BEHAVIOR_PROFILE", "social_high_risk").strip()
    if not scripts_dir.is_dir() or not evidence_path:
        raise RuntimeError("required TripPostCollect human behavior configuration is incomplete")

    scripts_value = str(scripts_dir.resolve())
    if scripts_value not in sys.path:
        sys.path.insert(0, scripts_value)
    from mediacrawler_behavior import run_page_behavior

    return await run_page_behavior(
        page,
        platform_key=platform_key,
        evidence_path=evidence_path,
        profile_name=profile_name,
    )


async def run_required_request_pause(stage: str, minimum: float, maximum: float) -> dict[str, Any]:
    if not _enabled():
        raise RuntimeError("required TripPostCollect request pacing is disabled")
    scripts_dir = Path(os.environ.get("TRIPPOSTCOLLECT_PROJECT_SCRIPTS", "")).expanduser()
    evidence_path = os.environ.get("TRIPPOSTCOLLECT_HUMAN_BEHAVIOR_EVIDENCE", "").strip()
    profile_name = os.environ.get("TRIPPOSTCOLLECT_HUMAN_BEHAVIOR_PROFILE", "").strip()
    if not scripts_dir.is_dir() or not evidence_path:
        raise RuntimeError("required TripPostCollect request pacing configuration is incomplete")
    scripts_value = str(scripts_dir.resolve())
    if scripts_value not in sys.path:
        sys.path.insert(0, scripts_value)
    from mediacrawler_behavior import run_guarded_request_pause

    return await run_guarded_request_pause(
        evidence_path=evidence_path,
        profile_name=profile_name,
        stage=stage,
        minimum=minimum,
        maximum=maximum,
    )


async def run_required_continuity_behavior(page: Page, stage: str) -> dict[str, Any]:
    if not _enabled():
        raise RuntimeError("required TripPostCollect continuity behavior is disabled")
    scripts_dir = Path(os.environ.get("TRIPPOSTCOLLECT_PROJECT_SCRIPTS", "")).expanduser()
    evidence_path = os.environ.get("TRIPPOSTCOLLECT_HUMAN_BEHAVIOR_EVIDENCE", "").strip()
    profile_name = os.environ.get("TRIPPOSTCOLLECT_HUMAN_BEHAVIOR_PROFILE", "").strip()
    if not scripts_dir.is_dir() or not evidence_path or profile_name != "xhs_guarded":
        raise RuntimeError("required TripPostCollect continuity behavior configuration is incomplete")
    scripts_value = str(scripts_dir.resolve())
    if scripts_value not in sys.path:
        sys.path.insert(0, scripts_value)
    from mediacrawler_behavior import run_xhs_continuity_behavior

    return await run_xhs_continuity_behavior(
        page,
        evidence_path=evidence_path,
        stage=stage,
    )


async def run_required_api_captcha_verification(
    page: Page,
    *,
    verify_type: str,
    verify_uuid: str,
    verify_biz: int,
) -> dict[str, Any]:
    if not _enabled():
        raise RuntimeError("required TripPostCollect API captcha verification is disabled")
    scripts_dir = Path(os.environ.get("TRIPPOSTCOLLECT_PROJECT_SCRIPTS", "")).expanduser()
    evidence_path = os.environ.get("TRIPPOSTCOLLECT_HUMAN_BEHAVIOR_EVIDENCE", "").strip()
    profile_name = os.environ.get("TRIPPOSTCOLLECT_HUMAN_BEHAVIOR_PROFILE", "").strip()
    if not scripts_dir.is_dir() or not evidence_path or profile_name != "xhs_guarded":
        raise RuntimeError("required TripPostCollect API captcha configuration is incomplete")
    scripts_value = str(scripts_dir.resolve())
    if scripts_value not in sys.path:
        sys.path.insert(0, scripts_value)
    from mediacrawler_behavior import run_xhs_api_captcha_verification

    return await run_xhs_api_captcha_verification(
        page,
        evidence_path=evidence_path,
        verify_type=verify_type,
        verify_uuid=verify_uuid,
        verify_biz=verify_biz,
    )


async def inspect_visible_page_state(page: Page) -> tuple[str, dict[str, bool]]:
    """Reuse the project's visible challenge checks without replacing run evidence."""
    scripts_dir = Path(os.environ.get("TRIPPOSTCOLLECT_PROJECT_SCRIPTS", "")).expanduser()
    if not scripts_dir.is_dir():
        raise RuntimeError("required TripPostCollect page inspection configuration is incomplete")
    scripts_value = str(scripts_dir.resolve())
    if scripts_value not in sys.path:
        sys.path.insert(0, scripts_value)
    from mediacrawler_behavior import visible_page_state

    return await visible_page_state(page)


async def run_requested_post_interaction(
    page: Page,
    *,
    platform_key: str,
    requested_mode: str,
    note_id: str,
) -> dict[str, Any]:
    if requested_mode == "none":
        return {"status": "disabled", "platform": platform_key}
    if platform_key != "xhs" or not _enabled():
        raise RuntimeError("XHS post interaction requires the enabled project behavior bridge")
    scripts_dir = Path(os.environ.get("TRIPPOSTCOLLECT_PROJECT_SCRIPTS", "")).expanduser()
    evidence_path = os.environ.get("TRIPPOSTCOLLECT_HUMAN_BEHAVIOR_EVIDENCE", "").strip()
    if not scripts_dir.is_dir() or not evidence_path:
        raise RuntimeError("required TripPostCollect post interaction configuration is incomplete")
    scripts_value = str(scripts_dir.resolve())
    if scripts_value not in sys.path:
        sys.path.insert(0, scripts_value)
    from mediacrawler_behavior import run_xhs_post_interaction

    return await run_xhs_post_interaction(
        page,
        evidence_path=evidence_path,
        requested_mode=requested_mode,
        note_id=note_id,
    )
