# pluggable-ai-test-assistant — 可拔插 AI 测试助手

状态：DONE

创建日期：2026-07-30

## 目标

在不配置 API Key 时提供可演示的 Mock Provider，并在后端配置 OpenAI Key 后切换为真实模型，
根据 OpenAPI 的脱敏结构生成边界与异常候选用例，始终由用户确认后再载入测试编辑器。

## 范围

- 冻结 AI 生成接口、Provider 协议和安全边界。
- 实现 Mock 与 OpenAI Responses API Provider。
- 仅向 Provider 发送脱敏后的 OpenAPI 结构，不发送 example、default、认证信息或请求凭据。
- 对模型输出执行严格结构与安全校验。
- 在中文控制台加入 Provider 状态、生成、预览和人工载入流程。
- 补充配置示例、文档和自动化测试。

## 非范围

- 自动执行 AI 生成用例。
- 在浏览器输入或保存 API Key。
- 自动分析生产日志、失败历史或真实用户数据。
- 支持任意自定义 Provider URL。
- 推送、创建 PR、合并或删除分支。

## 验收标准

- [x] 默认 Mock 模式无需网络和 API Key 即可生成候选用例。
- [x] OpenAI 模式只从后端环境变量读取密钥，并使用 Responses API 结构化输出。
- [x] Provider 输入不包含 OpenAPI 示例值、默认值、安全方案或 Header/Cookie 参数。
- [x] Provider 输出不能携带请求头、依赖、提取规则、绝对 URL 或超过上限的用例。
- [x] 页面生成后必须人工点击“载入测试编辑器”，且不会自动调用运行接口。
- [x] 错误响应不回显密钥、上游正文或原始文档内容。
- [x] 全量测试、覆盖率、JavaScript 语法和工作区检查通过。

## 共享契约

- 文件：`contracts/ai-case-generation-v0.1.yaml`
- 状态：已冻结

## 风险与假设

- AI 输出具有不确定性，因此只作为候选草稿，最终通过/失败仍由确定性执行器判断。
- OpenAI 调用会产生费用；默认 Mock 不联网，只有显式配置 Provider 和 Key 后才调用。
- 当前应用无认证，不应暴露公网；AI 入口沿用本地单用户演示边界。

## 文件所有权

| 工作流 | Agent | 可修改范围 | 依赖 | 状态 |
|---|---|---|---|---|
| 主协调与实现 | 主 Agent | `src/app/**`, `frontend/**`, `tests/**`, `docs/**`, `README.md`, `.env.example`, `tasks/**`, `contracts/**` | 冻结契约 | 进行中 |

## 子任务交接

- 本任务由主 Agent 串行实现，未创建子 Agent。

## 通信记录

- 用户要求实现可拔插 AI，并说明 API Key 可在需要时另行提供。
- 主 Agent 决策：默认 Mock；真实 Key 不进入代码、前端、日志、历史或 Git。

## 用户审批

- 用户已批准实现可拔插 AI 集成；本任务不读取密钥、不推送、不合并。

## 验证记录

| 检查 | 命令或方法 | 结果 | 证据/备注 |
|---|---|---|---|
| Python 编译 | `python -m compileall -q src tests scripts` | 通过 | 无编译错误 |
| 全量测试与分支覆盖率 | `python -m pytest --cov=app --cov-branch --cov-report=term --cov-fail-under=90` | 通过 | 191 passed，91.48% |
| 前端语法 | `node --check frontend/app.js` | 通过 | 无语法错误 |
| Git 差异检查 | `git diff --check` | 通过 | 无空白错误 |
| 协作工作区 | `python scripts/validate_workspace.py` | 通过 | 工作区验证通过 |

## 最终结果

- 完成内容：Mock/OpenAI Provider、脱敏结构摘要、严格输出校验、状态与生成 API、中文人工确认界面、文档与测试。
- 未完成内容：未使用用户真实 API Key 发起 OpenAI 请求；未推送、创建 PR 或合并。
- 剩余风险：真实模型质量、账号额度、网络和上游错误需要在用户提供 Key 后做一次不含敏感数据的联调；AI 结果仍需人工评审。
- 最终验收人：主 Agent
