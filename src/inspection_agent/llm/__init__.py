"""LLM 接入层：OpenAI 兼容 chat client（P1 叙事成稿与 bot agent loop 共用）。

红线：密钥从环境变量读取（配置仅存环境变量名）；任何失败抛 LLMError，
由调用方降级处理（日报退 P0 模板 / bot 回复可用性提示），绝不允许 LLM 故障中断服务。
"""
