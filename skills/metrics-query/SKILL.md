- 即时水位用 query，趋势/时段用 range_query；先明确时间窗与步长再发查询。
- 拼 PromQL 前先用 label_names/label_values 确认 label 名与实际取值（如 pod 前缀、namespace），
  用 series 确认序列存在；禁止凭想象写 label 值。
- PromQL 中 label matcher 的字面 {} 原样保留；聚合保留 by (instance/pod) 维度。
- 返回空序列 = 该窗口无数据，回答"无数据"并说明查询窗口，禁止猜测或编造数值。
- 引用数值时带时间点与查询语句（或图表链接）作为证据。
- 避免超大时间窗 × 过小步长（点数爆炸），必要时分段拉取。
- 基数/用量类问题用 tsdb_stats；告警与规则只读查询用 list_alerts / list_rules。
- 多实例部署时工具名带实例前缀（如 prometheus_juice__range_query），按实例的业务含义
  选对实例；不确定用户指哪套环境时先确认再查。
