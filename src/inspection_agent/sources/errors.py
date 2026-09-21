"""数据源错误基类：pipeline 统一捕获后按"数据缺失"降级（SKILL 容错红线）。"""


class SourceError(RuntimeError):
    """数据源调用/装配失败基类。"""


class PlatformError(SourceError):
    """bingops 平台 REST 调用失败。"""


class MCPlessSourceError(SourceError):
    """外部系统直连（prometheus/gitlab REST）调用失败。"""
