# visual-console — 新增中文可视化测试控制台

状态：DONE

创建日期：2026-07-25

## 目标

提供一个无需理解 Swagger 和手写 JSON 的中文可视化控制台，让用户能一键运行本地两步接口链路，
并直观看到汇总、用例状态、响应时间、断言、提取变量名和安全错误。

## 范围

- 创建无构建依赖的 `frontend/` 静态页面。
- 在 FastAPI 根路由提供页面和静态资源。
- 复用现有 `/api/v1/runs` 契约，不改变执行语义。
- 增加页面路由、静态资产、关键文案和回归测试。
- 更新 README 的可视化使用说明。

## 非范围

- 用户系统、数据库、运行历史和报告持久化。
- React/Vue、Node 工具链、新生产依赖或外部 CDN。
- 公网部署、认证、授权、推送远程仓库。

## 验收标准

- [x] 访问 `/` 能看到中文测试控制台。
- [x] 页面默认加载安全的两用例演示，base URL 使用当前 origin。
- [x] 点击运行可调用现有 `/api/v1/runs`。
- [x] 汇总展示总数、通过、失败、跳过；逐用例展示状态和断言。
- [x] 页面具备加载、错误、空结果状态并支持键盘操作。
- [x] API 返回值使用安全文本渲染，不通过不可信 HTML 注入 DOM。
- [x] 不新增生产依赖，不读取或保存真实密钥。
- [x] 全量测试、工作区验证、示例验证和完成门通过。

## 共享契约

- 文件：`contracts/visual-console-v0.1.yaml`
- 状态：已冻结（2026-07-25）

## 风险与假设

- 页面只面向本机演示；运行本地工作流仍需 `ALLOW_LOCAL_TARGETS=true`。
- 当前 API 无认证和限流，页面不改变“不允许公网开放”的边界。
- 无框架实现降低依赖和构建复杂度，但需要测试关键 DOM 契约与交互代码。

## 文件所有权

| 工作流 | Agent | 可修改范围 | 依赖 | 状态 |
|---|---|---|---|---|
| 主协调 | 主 Agent | `tasks/**`, `contracts/**` | 无 | 进行中 |
| 前端实施 | frontend_impl | `frontend/**` | 冻结契约 | 已完成 |
| 后端集成 | backend_ui | `src/app/**` | 冻结契约、前端入口 | 已完成 |
| 测试实施 | tester_ui | `tests/**` | 冻结契约 | 已完成 |
| 文档更新 | docs_impl | `README.md` | 已验收实现 | 已完成 |

## 子任务交接

- `frontend_impl`：只修改 `frontend/**`，实现中文控制台，不得修改后端、测试或治理文件。
- `backend_ui`：只修改 `src/app/**`，提供根页面和静态路由，不得改变 `/api/v1/runs` 契约。
- `tester_ui`：只修改 `tests/**`，验证路由、静态资产、关键 UI 安全契约和全部回归。

## 通信记录

- frontend_impl 明确静态入口与资源路径后，由主 Agent 同步 backend_ui 和 tester_ui。
- backend_ui 和 tester_ui 曾等待前端文件落盘；文件出现后均完成端到端契约验证。
- 无未确认持久消息、无用户审批或决策阻塞。

## 用户审批

- 无。本阶段不新增生产依赖、不部署、不推送、不连接生产系统。

## 验证记录

| 检查 | 命令或方法 | 结果 | 证据/备注 |
|---|---|---|---|
| 全量测试与分支覆盖率 | `.\.venv\Scripts\python.exe -m pytest --cov=app --cov-branch --cov-report=term-missing` | 通过 | 64 passed；总分支覆盖率 92% |
| JavaScript 语法 | `node --check .\frontend\app.js` | 通过 | 退出码 0 |
| 页面与静态路由 | `tests/test_visual_console.py` | 通过 | 7 项页面、资源、DOM 和安全契约测试 |
| 静态安全审查 | 搜索危险 DOM API、外部 URL 和浏览器存储 | 通过 | 无 innerHTML、外部资源、localStorage/sessionStorage |
| 工作区治理 | `.\.venv\Scripts\python.exe .\scripts\validate_workspace.py` | 通过 | 自定义 Agent 配置有效 |
| 空白错误 | `git diff --check` | 通过 | 仅 Windows LF/CRLF 转换提醒 |
| 文件范围 | 主 Agent 检查实际工作树 | 通过 | 各 Agent 未越过授权目录 |

## 最终结果

- 完成内容：中文可视化测试控制台、两步链路演示、高级 JSON 编辑、结果汇总、
  逐用例与断言详情、加载/错误/空状态、响应式与可访问性、FastAPI 根路由和 7 项页面契约测试。
- 未完成内容：真实浏览器截图回归、数据库运行历史、持久化报告和用户认证，均属于后续阶段。
- 剩余风险：页面和 API 仍只适合本机演示；真实运行必须显式设置 `ALLOW_LOCAL_TARGETS=true`；
  上游 TestClient/httpx2 弃用警告仍不阻塞。
- 最终验收人：主 Agent
