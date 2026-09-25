- 趋势/时段问题用区间查询（query_range / execute_range_query），当前水位用即时查询
  （query_instant / execute_query）；先明确时间窗与步长再发查询。
- PromQL 中 label matcher 的字面 {} 原样保留；聚合保留 by (instance/pod) 维度。
- 返回空序列 = 该窗口无数据，回答"无数据"并说明查询窗口，禁止猜测或编造数值。
- 引用数值时带时间点与查询语句（或图表链接）作为证据。
- 避免超大时间窗 × 过小步长（点数爆炸），必要时分段拉取。
