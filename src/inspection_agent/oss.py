"""阿里云 OSS 上传（oss2）：完整 HTML 报告 + 签名 URL。

红线（SKILL 7）：endpoint/bucket/AK/SK 全部从环境变量读取（配置仅存环境变量名），
密钥不入库不入码；任何失败抛 OSSError，由 runner 降级（消息照发并标注，本地文件保留）。
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

from .config import OssConfig

logger = logging.getLogger(__name__)


class OSSError(RuntimeError):
    """OSS 配置缺失或上传失败。"""


def _resolve_credentials(config: OssConfig) -> tuple[str, str, str, str]:
    """从环境变量读取 endpoint/bucket/AK/SK；缺失即抛 OSSError。"""
    import os

    endpoint = os.environ.get(config.endpoint_env) or ""
    bucket_name = os.environ.get(config.bucket_env) or ""
    access_key_id = os.environ.get(config.access_key_id_env) or ""
    access_key_secret = os.environ.get(config.access_key_secret_env) or ""
    missing = [
        name
        for name, value in (
            (config.endpoint_env, endpoint),
            (config.bucket_env, bucket_name),
            (config.access_key_id_env, access_key_id),
            (config.access_key_secret_env, access_key_secret),
        )
        if not value
    ]
    if missing:
        raise OSSError(f"OSS 环境变量未设置: {', '.join(missing)}")
    return endpoint, bucket_name, access_key_id, access_key_secret


def upload_report(config: OssConfig, html_path: Path, report_date: date | str) -> str:
    """上传 HTML 并返回签名 URL（GET，有效期 url_expires_sec）；失败抛 OSSError。"""
    import oss2  # 延迟导入：oss.enabled=false 的部署无需安装/加载

    endpoint, bucket_name, access_key_id, access_key_secret = _resolve_credentials(config)
    day = report_date.isoformat() if isinstance(report_date, date) else str(report_date)
    key = f"{config.object_prefix}{day}/report.html"
    try:
        bucket = oss2.Bucket(oss2.Auth(access_key_id, access_key_secret), endpoint, bucket_name)
        bucket.put_object_from_file(key, str(html_path))
        url = bucket.sign_url("GET", key, config.url_expires_sec)
    except Exception as exc:
        raise OSSError(f"OSS 上传失败（key={key}）: {exc}") from exc
    logger.info("完整报告已上传 OSS: %s", key)
    return url
