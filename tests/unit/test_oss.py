"""P1.5 OSS 单测：上传与日报产物读取（monkeypatch oss2，覆盖成功/失败/凭据缺失，无真实网络）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from inspection_agent.config import OssConfig
from inspection_agent.oss import OSSError, read_report, upload_report

CFG = OssConfig(enabled=True)


@pytest.fixture()
def oss_env(monkeypatch):
    monkeypatch.setenv("OSS_ENDPOINT", "http://fake-endpoint")
    monkeypatch.setenv("OSS_BUCKET", "fake-bucket")
    monkeypatch.setenv("OSS_ACCESS_KEY_ID", "fake-ak")
    monkeypatch.setenv("OSS_ACCESS_KEY_SECRET", "fake-sk")


def test_upload_missing_env_raises(monkeypatch):
    for name in ("OSS_ENDPOINT", "OSS_BUCKET", "OSS_ACCESS_KEY_ID", "OSS_ACCESS_KEY_SECRET"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(OSSError, match="环境变量未设置"):
        upload_report(CFG, Path("report.html"), "2026-09-05")


def test_upload_success_returns_signed_url(monkeypatch, tmp_path, oss_env):
    uploaded: dict = {}

    class FakeBucket:
        def __init__(self, auth, endpoint, bucket_name) -> None:
            pass

        def put_object_from_file(self, key, path) -> None:
            uploaded["key"] = key
            uploaded["path"] = path

        def sign_url(self, method, key, expires) -> str:
            uploaded["method"] = method
            uploaded["expires"] = expires
            return f"http://fake-oss/{key}?sig=1"

    monkeypatch.setattr("oss2.Bucket", FakeBucket)
    html_path = tmp_path / "report.html"
    html_path.write_text("<html/>", encoding="utf-8")

    url = upload_report(CFG, html_path, "2026-09-05")
    assert url == "http://fake-oss/inspection-report/2026-09-05/report.html?sig=1"
    assert uploaded["key"] == "inspection-report/2026-09-05/report.html"
    assert uploaded["expires"] == CFG.url_expires_sec


def test_upload_failure_raises_oserror(monkeypatch, tmp_path, oss_env):
    class BrokenBucket:
        def __init__(self, *args) -> None:
            pass

        def put_object_from_file(self, key, path) -> None:
            raise ConnectionError("refused")

    monkeypatch.setattr("oss2.Bucket", BrokenBucket)
    with pytest.raises(OSSError, match="OSS 上传失败"):
        upload_report(CFG, tmp_path / "report.html", "2026-09-05")


# ---------------------------------------------------------------- 日报产物读取（latest_report 数据通道）


def _fake_oss_read(monkeypatch, objects: dict[str, bytes]):
    """key → bytes 的假 Bucket + 假 ObjectIterator（key 列表来自 objects）。"""

    class FakeGetResult:
        def __init__(self, data: bytes) -> None:
            self._data = data

        def read(self) -> bytes:
            return self._data

    class FakeBucket:
        def __init__(self, *args) -> None:
            pass

        def get_object(self, key: str):
            return FakeGetResult(objects[key])

    class FakeObj:
        def __init__(self, key: str) -> None:
            self.key = key

    monkeypatch.setattr("oss2.Bucket", FakeBucket)
    monkeypatch.setattr(
        "oss2.ObjectIterator",
        lambda bucket, prefix=None: [
            FakeObj(key) for key in sorted(objects) if key.startswith(prefix or "")
        ],
    )


def test_read_report_latest_picks_max_date(monkeypatch, oss_env):
    payload = {"report_date": "2026-09-05", "anomalies": []}
    _fake_oss_read(
        monkeypatch,
        {
            "inspection-report/2026-09-04/anomalies.json": json.dumps({"report_date": "2026-09-04"}).encode(),
            "inspection-report/2026-09-05/anomalies.json": json.dumps(payload).encode(),
        },
    )
    assert read_report(CFG) == payload


def test_read_report_specific_date(monkeypatch, oss_env):
    _fake_oss_read(
        monkeypatch,
        {"inspection-report/2026-09-04/anomalies.json": json.dumps({"report_date": "2026-09-04"}).encode()},
    )
    assert read_report(CFG, "2026-09-04")["report_date"] == "2026-09-04"


def test_read_report_invalid_date_raises(monkeypatch, oss_env):
    with pytest.raises(OSSError, match="YYYY-MM-DD"):
        read_report(CFG, "2026/09/04")


def test_read_report_empty_bucket_raises(monkeypatch, oss_env):
    _fake_oss_read(monkeypatch, {})
    with pytest.raises(OSSError, match="没有任何日报产物"):
        read_report(CFG)


def test_read_report_disabled_raises(monkeypatch):
    with pytest.raises(OSSError, match="OSS 存储未启用"):
        read_report(OssConfig(enabled=False))
