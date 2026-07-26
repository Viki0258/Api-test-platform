# openapi-visual-console — OpenAPI 可视化导入与载入

状态：DONE

## 目标

让非后端用户在中文控制台中加载合成演示、粘贴或选择 OpenAPI JSON，查看生成数量与警告，并在人工确认后把生成结果一键载入现有测试编辑器。

## 范围

- 在根页面新增 OpenAPI 生成区域
- 支持1MiB以内 JSON 文件、JSON编辑器、可选base URL覆盖值和1～50最大用例数
- 调用同源 `/api/v1/openapi/generate`
- 安全展示结构化结果和warning
- 人工点击后只载入现有编辑器，不自动执行
- 响应式、键盘和状态可访问性

## 非范围

- YAML、拖拽、浏览器存储
- 自动运行生成用例
- 认证凭据输入或生成
- AI、负向或边界用例生成
- 后端 API 或生成规则修改

## 验收标准

- [x] 页面具备契约规定的输入、按钮、汇总、warning与状态DOM
- [x] 文件在读取前检查1MiB限制，只接受JSON对象且不持久化
- [x] 生成请求严格同源、POST、`credentials: same-origin`，不会调用外部URL
- [x] 成功响应必须校验run/cases结构，随后才允许人工载入
- [x] 异常200响应中的变量、秘密变量、Header/Cookie凭据、非法base URL/path或非生成器断言必须被拒绝
- [x] 载入动作同步现有payload/base URL/JSON/概览并重置结果，绝不自动调用`/runs`
- [x] 所有文档与API值使用textContent等安全DOM API，不使用HTML字符串注入
- [x] 不使用localStorage/sessionStorage/indexedDB，不记录文档或密钥
- [x] 测试覆盖正常、失败、文件上限、密钥与自动执行边界
- [x] 全量测试、覆盖率90%、JS语法、工作区验证通过
- [x] README和项目展示与真实UI一致

## 共享契约

- 文件：`contracts/openapi-console-v0.1.yaml`
- 状态：已冻结

## 风险与假设

- 浏览器只做易用性校验，安全上限仍由后端强制执行
- 原始OpenAPI和生成run均只保存在当前页面内存；刷新即丢失
- 普通query/body示例会进入生成草稿，因此输入文档不得包含真实密钥

## 文件所有权

| 工作流 | Agent | 可修改范围 | 依赖 | 状态 |
|---|---|---|---|---|
| 主协调 | 主 Agent | `tasks/**`, `contracts/**`, 审批记录 | 无 | 已完成 |
| 前端实现 | frontend | `frontend/index.html`, `frontend/app.js`, `frontend/styles.css` | 冻结契约 | 已完成 |
| 测试实现 | tester | `tests/test_visual_console.py`, `tests/test_openapi_console.py` | 冻结契约 | 已完成 |
| 文档 | docs_writer | `README.md`, `docs/PROJECT_SHOWCASE.md` | UI实现 | 已完成 |
| 安全复核 | security_reviewer | 只读全量diff | 实现完成 | 已完成 |

## 子任务交接

- frontend：只修改三个frontend文件；禁止网络外链、HTML字符串注入、浏览器存储、console记录和自动执行。
- tester：只修改两个授权测试文件；从契约验证DOM、请求、文件读取顺序、安全渲染、内存边界和载入不执行。
- docs_writer：只更新真实操作步骤和限制，不声称支持YAML、AI、自动执行或凭据。
- security_reviewer：只读复核XSS、文件大小、同源请求、密钥、存储、日志与自动执行风险。

## 通信记录

- frontend 完成页面、状态机、严格响应校验与人工载入流程。
- tester 新增契约和恶意响应测试；定向测试15项通过，全量测试160项通过。
- security_reviewer 首轮发现异常200响应可能夹带凭据，修复后复核为无阻塞。
- docs_writer 已按真实页面更新README和项目展示。
- 主 Agent 检查真实diff、自动化结果，并完成桌面与390px移动视口验收。

## 用户审批

- 当前仅本地开发和提交，不推送；无需额外审批。

## 验证记录

| 检查 | 命令或方法 | 结果 | 证据/备注 |
|---|---|---|---|
| Python编译 | `python -m compileall -q src tests scripts` | 通过 | 无语法错误 |
| 全量测试与分支覆盖率 | `pytest --cov=app --cov-branch --cov-fail-under=90` | 通过 | 160 passed；90.52% |
| JavaScript语法 | `node --check frontend/app.js` | 通过 | 无语法错误 |
| 工作区规则 | `python scripts/validate_workspace.py` | 通过 | Agent和契约结构有效 |
| Git差异格式 | `git diff --check` | 通过 | 仅有Windows换行提示 |
| 密钥模式扫描 | 对全部变更文件执行常见凭据模式扫描 | 通过 | 未发现疑似真实密钥 |
| 安全复核 | security_reviewer 只读复核与恶意200响应矩阵 | 通过 | 无阻塞项 |
| 桌面视觉验收 | Edge 1440px截图 | 通过 | 表单、结果区和现有工作区对齐 |
| 移动视觉验收 | CDP模拟390px视口并检查页面宽度 | 通过 | `innerWidth`与`scrollWidth`均为390px，无横向溢出 |

## 最终结果

- 完成内容：OpenAPI JSON演示、文件/文本输入、可选base URL和数量限制、生成汇总与warning、严格响应校验、人工载入现有编辑器、响应式布局、测试与文档
- 未完成内容：无；YAML、自动执行、凭据和AI生成按范围明确不支持
- 剩余风险：输入示例仍必须由用户保证不含真实密钥
- 最终验收人：主 Agent
