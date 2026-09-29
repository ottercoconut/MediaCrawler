"""图片 staging 旧导入出口；站点稳定键留待各站 parser 迁移。"""

# T06：抖音资产键只保留根 parser 的权威实现。
from trippostcollect.platforms.douyin.parser import douyin_source_asset_key as douyin_source_asset_key
from hashlib import sha256
import re
from urllib.parse import urlsplit

# T07：旧桥保留导入名，知乎资产键由本站 parser 定义。
from trippostcollect.platforms.zhihu.parser import zhihu_source_asset_key as zhihu_source_asset_key

from trippostcollect.runtime.helpers import normalize_image_url as normalize_image_url
from trippostcollect.artifacts.image_staging import (
    ImageStagingError as ImageStagingError,
    ImageAsset as ImageAsset,
    InspectedImage as InspectedImage,
    looks_like_supported_raster as looks_like_supported_raster,
    inspect_image_bytes as inspect_image_bytes,
    _safe_component as _safe_component,
    _fsync_directory as _fsync_directory,
    _manifest_payload as _manifest_payload,
    upsert_manifest_rows_atomic as upsert_manifest_rows_atomic,
    failed_manifest_row as failed_manifest_row,
    stage_post_images as stage_post_images,
    MAX_IMAGE_BYTES as MAX_IMAGE_BYTES,
    MAX_IMAGE_PIXELS as MAX_IMAGE_PIXELS,
    FORMAT_METADATA as FORMAT_METADATA,
)

XHS_STABLE_PATH_MARKERS = ("/notes_pre_post/", "/notes_post/", "/notes/")

ZHIMG_TRANSFORM_SUFFIX_RE = re.compile(
    r"_(?:[1-9]\d{1,4}w|b|r|qhd|hd|xs|s|m|l|xl|xxl|original|watermark)"
    r"\.(?:avif|gif|jpe?g|png|webp)$",
    re.IGNORECASE,
)

RASTER_SUFFIX_RE = re.compile(r"\.(?:avif|gif|jpe?g|png|webp)$", re.IGNORECASE)


from trippostcollect.platforms.weibo.parser import weibo_source_asset_key as weibo_source_asset_key


def xhs_source_asset_key(source_url: str) -> str:
    normalized = normalize_image_url(source_url)
    parsed = urlsplit(normalized)
    identity = f"{parsed.netloc.lower()}{parsed.path}"
    for marker in XHS_STABLE_PATH_MARKERS:
        if marker in parsed.path:
            identity = f"{marker}{parsed.path.split(marker, 1)[1]}"
            break
    return f"xhs:path:{identity}"


