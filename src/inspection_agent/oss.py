"""阿里云 OSS 读写（oss2）：完整 HTML 报告上传 + 日报结构化产物读取。

红线（SKILL 7）：endpoint/bucket/AK/SK 全部从环境变量读取（配置仅存环境变量名），
密钥不入库不入码；任何失败抛 OSSError，由调用方降级（上传失败消息照发；读取失败回错误提示）。
"""

from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path
from typing import Any

from .config import OssConfig

logger = logging.getLogger(__name__)


class OSSError(RuntimeError):
    """OSS 配置缺失或上传/读取失败。"""


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


def read_report(config: OssConfig, report_date: str | None = None) -> dict[str, Any]:
    """读取日报结构化产物 anomalies.json（默认最新一期）；失败抛 OSSError。

    bot latest_report 本地工具的数据通道（只读）；产物 JSON 的数字全部来自规则引擎。
    """
    import oss2  # 延迟导入：oss.enabled=false 的部署无需安装/加载

    if not config.enabled:
        raise OSSError("OSS 存储未启用（oss.enabled=false），日报产物读取不可用")
    endpoint, bucket_name, access_key_id, access_key_secret = _resolve_credentials(config)
    bucket = oss2.Bucket(oss2.Auth(access_key_id, access_key_secret), endpoint, bucket_name)
    day = _resolve_report_day(bucket, config.object_prefix, report_date)
    key = f"{config.object_prefix}{day}/anomalies.json"
    try:
        payload = json.loads(bucket.get_object(key).read().decode("utf-8"))
    except OSSError:
        raise
    except Exception as exc:
        raise OSSError(f"日报读取失败（key={key}）: {exc}") from exc
    if not isinstance(payload, dict):
        raise OSSError(f"日报产物格式非法（非 JSON 对象）: {key}")
    logger.info("日报产物已读取: %s", key)
    return payload


def _resolve_report_day(bucket: Any, prefix: str, report_date: str | None) -> str:
    """指定日期直接返回（ISO 校验）；未指定时列举产物取字典序最大日期。"""
    if report_date is not None:
        text = str(report_date).strip()
        try:
            return date.fromisoformat(text).isoformat()
        except ValueError as exc:
            raise OSSError(f"报告日期格式需为 YYYY-MM-DD: {text!r}") from exc
    import oss2

    latest = ""
    for obj in oss2.ObjectIterator(bucket, prefix=prefix):
        key = str(getattr(obj, "key", ""))
        if key.startswith(prefix) and key.endswith("/anomalies.json"):
            day = key[len(prefix) : -len("/anomalies.json")]
            if day > latest:
                latest = day
    if not latest:
        raise OSSError(f"OSS 上没有任何日报产物（prefix={prefix}）")
    return latest
