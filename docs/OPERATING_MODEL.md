# 多 Agent 运行模型

## 核心原则

协作状态以仓库文件为准，不以某个 Agent 的聊天记忆为准：

- 需求、范围、验收和所有权：`tasks/active/<task-id>.md`
- 前后端共享语义：`contracts/`
- 人类审批：`tasks/approvals/`
- 跨 Agent 持久消息：`coordination/messages/`
- 消息确认：`coordination/acks/`
- 长期技术决策：`docs/decisions/`
- 最终事实：实际 diff、构建和测试输出

## 状态流

```text
INTAKE -> CONTRACT -> IMPLEMENT -> REVIEW -> VERIFY -> DONE
                    \                         /
                     -> NEEDS_USER_APPROVAL -
```

### INTAKE

主 Agent 创建任务卡，写清：

- 目标和用户价值；
- 范围与非范围；
- 可观察的验收标准；
- 已知风险和未决问题；
- 拟分派角色。

需求歧义如果会改变用户体验、数据语义、安全策略或公开接口，交给用户决定。
普通实现细节由主 Agent 决定并记录假设。

### CONTRACT

跨模块任务先定义共享契约：

- API 请求、响应和错误；
- 事件或消息格式；
- 身份与权限规则；
- 数据一致性与幂等约束；
- 向后兼容要求。

契约冻结后，主 Agent 在任务卡记录版本或文件路径。子 Agent 只能提出变更，不能静默修改。

### IMPLEMENT

主 Agent 为每个子任务声明：

```text
目标：
负责人：
可修改：
只读参考：
禁止事项：
依赖：
验收命令：
交付格式：
```

只有在文件范围不重叠、共享契约已稳定时才并行执行。不要让子 Agent 继续拆分，除非任务规模
足以支撑新的独立边界且主 Agent 明确授权。

### REVIEW

- 主 Agent 审查所有代码和跨模块一致性。
- `tester` 独立检查功能、失败路径和回归。
- 涉及身份、数据、文件、网络、依赖或部署时，启用 `security_reviewer`。
- `docs_writer` 只记录经过验证的行为。

审查发现属于原范围的问题，交还原负责人修复；属于产品取舍或范围扩张的问题，交给主 Agent，
必要时交给用户。

### VERIFY

主 Agent 至少收集：

- 任务卡验收项逐条结果；
- 静态检查、构建和测试命令及结果；
- 未运行检查及原因；
- 安全审查结论；
- 最终 diff 范围；
- 剩余风险。

“子 Agent 表示完成”不属于验证证据。

### COMMUNICATION

实时沟通由 Codex 原生线程承担。需要跨会话保留的阻塞、请求、决策和结果使用
`scripts/agent_bus.py`：

- 消息使用独立 JSON 文件，避免并发追加导致损坏；
- 消息正文不可变，确认使用单独的 ack 文件；
- 非主 Agent 之间的消息自动抄送主 Agent；
- 主 Agent 仍是唯一决策者；
- 消息箱不会自动启动或唤醒 Agent。

### NEEDS_USER_APPROVAL

遇到 `AGENTS.md` 中的人类审批边界时：

1. 停止敏感动作；
2. 复制 `tasks/APPROVAL_TEMPLATE.md` 到 `tasks/approvals/`；
3. 填写具体命令、目标、影响、回滚和替代方案；
4. 向用户展示摘要并请求明确批准；
5. 只执行获批的具体动作；
6. 在审批卡记录结果。

等待审批时继续执行不依赖该动作的安全工作。

### DONE

全部必需验收项通过后：

1. 主 Agent 填写任务卡的最终验证和剩余风险；
2. 把状态更新为 `VERIFY`，运行 `python scripts/complete_task.py <task-id> --check`；
3. 检查通过后运行 `python scripts/complete_task.py <task-id>`，由脚本移动到 `tasks/done/`；
4. 向用户汇报完成内容、验证证据和未执行的敏感操作。

## 失败与冲突处理

- 同文件冲突：停止并行，由当前文件负责人完成；另一 Agent 返回建议。
- 契约冲突：主 Agent 暂停依赖方，更新契约后再恢复。
- 测试失败：测试 Agent 给复现证据，原实施 Agent 修复，测试 Agent复验。
- Agent 无响应：主 Agent 保留已获得证据，重新分派一个更小的任务。
- 无法验证：明确标记“未验证”，不要降级为“通过”。
- 用户拒绝审批：记录拒绝，选择无敏感操作的替代方案，或报告功能受阻的确切部分。

## 任务规模与并发建议

- 小改动：主 Agent 单独完成。
- 单模块中型改动：一个实施 Agent，加一个测试或审查 Agent。
- 全栈功能：前端、后端、测试并行；安全按风险加入。
- 大型迁移：先由探索/审查角色建立证据，再分批实施；不要一次铺开全部 Agent。
