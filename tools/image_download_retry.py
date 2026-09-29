"""正文图片重试的旧导入出口；实现已迁入根 runtime。"""

from trippostcollect.runtime.image_retry import (
    ImageDownloadFetchError as ImageDownloadFetchError,
    classified_http_image_error as classified_http_image_error,
    is_retryable_image_error as is_retryable_image_error,
    is_runtime_blocking_image_error as is_runtime_blocking_image_error,
    fetch_image_bytes_with_retry as fetch_image_bytes_with_retry,
    IMAGE_DOWNLOAD_MAX_ATTEMPTS as IMAGE_DOWNLOAD_MAX_ATTEMPTS,
    IMAGE_DOWNLOAD_MAX_BYTES as IMAGE_DOWNLOAD_MAX_BYTES,
    IMAGE_DOWNLOAD_RETRY_DELAY_SECONDS as IMAGE_DOWNLOAD_RETRY_DELAY_SECONDS,
    RETRYABLE_IMAGE_ERROR_CODES as RETRYABLE_IMAGE_ERROR_CODES,
    RETRYABLE_IMAGE_HTTP_STATUS_CODES as RETRYABLE_IMAGE_HTTP_STATUS_CODES,
    RUNTIME_BLOCKING_IMAGE_ERROR_CODES as RUNTIME_BLOCKING_IMAGE_ERROR_CODES,
)
