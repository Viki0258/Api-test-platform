# run-history — 新增测试运行历史与结果持久化

状态：DONE

创建日期：2026-07-25

## 目标

让每次接口测试运行生成稳定 ID，并把已经脱敏的结果保存到项目本地 SQLite 数据库；
用户可以在中文控制台查看最近运行、刷新列表并打开历史详情。

## 范围

- 使用 Python 标准库 SQLite 持久化安全的运行结果，不新增生产依赖。
- 扩展运行响应，增加 `run_id` 和 `created_at`。
- 新增历史列表和单条详情只读 API。
- 中文控制台增加最近运行区域和详情查看。
- 增加存储、API、前端契约、安全与回归测试。
- 更新 README 和项目展示说明。

## 非范围

- 用户登录、多租户、云数据库和生产部署。
- 保存原始请求头、请求体、运行变量值或未脱敏响应。
- 删除历史、修改历史、导出报告和 Allure 集成。
- 推送分支、创建或合并 PR。

## 验收标准

- [x] 每次运行返回不可预测的 `run_id` 和 UTC `created_at`。
- [x] 只持久化 `TestRunResult` 中已经脱敏的安全结果。
- [x] 历史列表支持有界 limit，按时间倒序返回摘要。
- [x] 历史详情按 ID 返回完整安全结果，不存在时返回稳定 404。
- [x] SQLite 文件位于项目 `.data/` 且被 Git 忽略。
- [x] 中文控制台能刷新最近记录并打开详情。
- [x] 现有执行、目标策略和可视化功能保持兼容。
- [x] 全量测试、分支覆盖率、工作区验证和完成门通过。

## 共享契约

- 文件：`contracts/run-history-v0.1.yaml`
- 状态：已冻结（2026-07-25）

## 风险与假设

- API 仍只适合本机单用户演示，不实现身份授权。
- SQLite 是本地演示存储；并发、备份和迁移能力有限。
- 现有结果已执行 secret 脱敏，但持久化层必须再次限制模型类型，禁止写入原始请求。

## 文件所有权

| 工作流 | Agent | 可修改范围 | 依赖 | 状态 |
|---|---|---|---|---|
| 主协调 | 主 Agent | `tasks/**`, `contracts/**` | 无 | 进行中 |
| 后端审计 | backend_history_audit | 只读 | 当前 API、模型和执行器 | 已完成 |
| 测试审计 | tester_history_audit | 只读 | 当前测试与验收目标 | 已完成 |
| 安全审计 | 主 Agent | 只读 | 当前脱敏、文件与数据边界 | 已完成 |
| 后端实施 | backend_history_impl | `src/app/**`, `.gitignore` | 冻结契约 | 已完成 |
| 测试实施 | tester_history_impl | `tests/**` | 冻结契约 | 已完成 |
| 前端集成 | frontend_impl | `frontend/**` | 冻结契约、后端 API | 已完成 |
| 文档集成 | 主 Agent | `README.md`, `docs/PROJECT_SHOWCASE.md` | 验证结果 | 已完成 |

## 子任务交接

- 三个审计 Agent 均只读，不得修改文件；分别返回最小架构、测试矩阵和数据安全控制。
- `backend_history_impl`：只修改 `src/app/**` 和 `.gitignore`，实现模型、SQLite 存储和 API。
- `tester_history_impl`：只修改 `tests/**`，实现存储、API、脱敏、故障和回归测试。

## 主 Agent 决策

- 历史列表响应为 `{items, limit, total}`，默认20条，范围1..100。
- 排序为 `created_at DESC, sequence DESC`；内部序号不暴露。
- 格式错误和不存在的运行 ID 均返回 `404 RUN_NOT_FOUND`。
- 写入失败返回 `503 HISTORY_PERSISTENCE_FAILED`；读取或损坏返回
  `503 HISTORY_STORAGE_UNAVAILABLE`，不返回 SQLite 原始信息。
- 数据库固定在仓库 `.data/run-history.sqlite3`，最多保留500条。
- 保存完整的已脱敏 `TestRunResult`；未声明为 secret 的断言值属于已知边界。
- 请求校验失败和目标拒绝不保存；实际产生的 passed/failed/skipped 结果均保存。

## 通信记录

- backend_history_impl 在模型和存储接口确定后同步 tester_history_impl，避免测试猜测公开接口。
- 主 Agent 审查发现 sqlite3 连接上下文不会自动关闭，退回后端修复为 `contextlib.closing`。
- 会话子 Agent 线程达到上限后，数据安全审计与文档集成由主 Agent串行完成。
- 无未确认持久消息、用户审批或外部操作。

## 用户审批

- 无。本任务仅在项目内创建代码与被 Git 忽略的本地测试数据，不执行远程推送或部署。

## 验证记录

| 检查 | 命令或方法 | 结果 | 证据/备注 |
|---|---|---|---|
| 全量测试与分支覆盖率 | `.\.venv\Scripts\python.exe -m pytest --cov=app --cov-branch --cov-report=term-missing` | 通过 | 99 passed；总分支覆盖率92% |
| 历史存储专项 | `tests/test_run_history_storage.py` | 通过 | 初始化、事务、排序、隔离、损坏和脱敏字节扫描 |
| 历史 API 专项 | `tests/test_run_history_api.py` | 通过 | POST持久化、列表、详情、404、503和边界 |
| 前端历史契约 | `tests/test_visual_console.py` | 通过 | 13项控制台、历史与安全契约测试 |
| JavaScript 语法 | `node --check .\frontend\app.js` | 通过 | 退出码0 |
| 真实数据目录隔离 | 检查 `.data` 与 `git check-ignore` | 通过 | 测试未创建真实`.data`；规则已忽略数据库 |
| 工作区治理 | `.\.venv\Scripts\python.exe .\scripts\validate_workspace.py` | 通过 | 自定义 Agent 配置有效 |
| 空白错误 | `git diff --check` | 通过 | 仅 Windows LF/CRLF 转换提醒 |
| 主 Agent 安全审查 | 检查参数化SQL、固定路径、错误映射、持久化字段与DOM API | 通过 | 无原始请求持久化或不可信HTML渲染 |

## 最终结果

- 完成内容：UUIDv4运行标识、UTC时间、本地SQLite安全结果存储、500条保留、
  历史摘要/详情API、稳定404/503、中文最近运行列表与详情、存储/API/UI专项测试和文档。
- 未完成内容：用户认证、多租户、历史删除/筛选、报告导出、迁移和备份，均属于后续阶段。
- 剩余风险：仅适合本机单用户；未声明为secret的断言值可能持久化；POST在目标请求完成后
  若写库失败返回503，客户端盲目重试可能重复执行有副作用请求；上游TestClient弃用警告仍不阻塞。
- 最终验收人：主 Agent
