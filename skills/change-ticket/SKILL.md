- 变更类问题（"最近改了什么/为什么出问题"）优先 get_change_context：一次拿全
  活跃工单+近期变更+冻结窗口，避免多工具拼凑。
- 追溯单个工单细节用 get_ticket_timeline（流转+评论）；按类型/状态筛选用 list_tickets。
- 执行事实以 list_job_executions 为准（含 code_ref、状态、目标资源），
  引用时带工单号或执行 ID 作为证据。
- 判断"现在能不能变更"先查 list_freezes 封禁窗口。
- 查无记录时明确说"窗口内无变更/工单记录"，不要把"没有记录"说成"没有变更"。
