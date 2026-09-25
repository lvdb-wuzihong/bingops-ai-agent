- 定位实体优先用 search_assets 跨域搜索（应用+资源一次命中）或 find_app_by_resource
  （已知 IP/资源 ID 反查应用）；不要凭名称猜测应用。
- 应用详情用 get_app_overview（含依赖/被依赖与资源清单），资源明细用 get_resource_detail；
  回答时同时给出 ID 与名称（ID 用于后续查询，名称用于人读）。
- 看依赖关系用 get_app_topology（依赖/被依赖应用+外部依赖），必要时带 env 参数分环境；
  拓扑只展开需要的层级，不递归全图。
- list_business_apps 用于按团队/关键词列应用；get_models_overview 用于回答
  "平台有哪些资源类型、各多少量"这类总览问题。
- 查询结果为空时如实说"未找到"，换一种定位方式重试一次，仍无则明确说明。
