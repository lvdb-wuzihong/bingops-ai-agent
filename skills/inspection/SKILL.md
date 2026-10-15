# 巡检工作流（先判定再下钻）

数字与判定只引用工具返回；quick_inspect/latest_report 的 rule/severity 是规则引擎结论，禁止自行按阈值下结论。

## 1 实时快检

问"某应用现在正常吗/有什么问题"先调 quick_inspect(app, hours=3)：逐条引用 anomalies/risks 的 rule/severity/observed/threshold/evidence_url；data_missing 非空时明确说数据缺失及原因，禁止编造；多命中报错时列候选让用户精确指定。

## 2 日报解读

问"昨天巡检结果/近期异常"调 latest_report(date?)，引用 anomalies 与 risks（含趋势类）；完整 HTML 报告以日报推送消息里的 OSS 链接为准，不要杜撰链接。

## 3 下钻排查

变更：get_change_context、list_job_executions、get_ticket_timeline；拓扑归属：get_app_overview、get_app_topology、search_assets；指标：range_query、query、label_values；告警：list_alerts。

## 4 结论格式

一句话结论（健康/异常/数据缺失）→ 逐条证据 → 下一步建议；查不到就说查不到，禁止猜测。
