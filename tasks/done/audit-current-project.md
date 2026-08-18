# audit-current-project — MySQL 历史与有界并发

状态：DONE

## 目标

将项目从单进程 SQLite/隐式线程并发提升为可配置的 MySQL 历史存储、多个客户端并发运行和应用内有界并发，同时保持既有安全结果和 API 兼容性。

## 范围

- 增加 DATABASE_URL、SQLite 回退和 MySQL/InnoDB 历史存储适配。
- 增加默认 4 个槽位的进程内有界并发，容量不足返回稳定 429。
- 保持 run 内串行依赖、历史 API、脱敏和 SSRF 边界。
- 用测试和本地 SQLAlchemy 方言验证覆盖并发、容量、回收、存储和错误处理。

## 非范围

- 不实现认证、租户隔离、跨 worker 全局租约或后台异步任务队列。
- 不连接生产环境、不执行生产数据库迁移、不读写真实密钥或真实用户数据。
- 不执行 git push、部署或破坏性清理。

## 验收标准

- [x] DATABASE_URL 未配置时 SQLite 回退，配置 MySQL URL 时选择 MySQL 方言且不泄漏连接信息。
- [x] 多客户端可并发运行；单个 run 内依赖仍串行且状态不串线。
- [x] 并发达到上限时返回 429、RUN_CAPACITY_EXCEEDED 和 Retry-After，不执行目标请求。
- [x] 所有终止路径释放容量；并发历史写入无丢失、重复或损坏。
- [x] 运行完整测试、覆盖率、工作区校验、编译、依赖和差异检查。
- [x] 更新 README/契约/迁移说明；最终完成门待 Native 验收后执行。

## 共享契约

- 文件：contracts/run-history-v0.2.yaml、docs/comet/changes/audit-current-project/specs/concurrent-history/spec.md
- 状态：已冻结；实现和测试必须遵循该版本，用户可见行为变化需回到 Shape。

## 风险与假设

- 当前工作区已有 Comet/OpenSpec 初始化产生的未提交文件，实施不得覆盖或清理这些改动。
- 依赖联网、外部服务或生产数据的结论仅能标记为未验证。
- 当前只做本地 SQLite/SQLAlchemy 方言验证，不证明生产网络、权限或容量配置。
- 应用内信号量只限制单进程；多 worker 的全局容量属于后续 change。

## 文件所有权

| 工作流 | Agent | 可修改范围 | 依赖 | 状态 |
|---|---|---|---|---|
| 主协调与契约 | 主 Agent | tasks/**、contracts/**、Native change 产物 | 无 | 进行中 |
| 后端实现 | backend | src/app/** | 契约 | 待实施 |
| 测试实现 | tester | tests/** | 契约 | 待实施 |
| 文档实现 | docs_writer | README.md、docs/**（Native 产物除外） | 已验证行为 | 待实施 |

## 用户审批

- 依赖/服务审批：tasks/approvals/mysql-history-concurrency-dependency.md
- 状态：用户已确认；仅更新项目依赖和本地 .venv，不连接生产 MySQL。

## 验证记录

| 检查 | 命令/方法 | 结果 | 证据/备注 |
|---|---|---|---|
| 初始工作区 | git status --short | 已记录 | 保留既有未提交改动 |
| Native 状态 | comet native status audit-current-project --details --json | Build，待提交 handoff | 21 个验收项待 Verifier 独立判定 |
| 任务卡格式 | pytest tests/test_workspace_tools.py -q | 先前通过 | 初始状态已符合枚举 |
| 全量质量门 | pytest --cov=app --cov-branch --cov-report=term-missing --cov-fail-under=90 | 通过 | 218 passed；91.18% 分支覆盖率；1 条 Starlette/httpx 弃用警告 |
| 工作区治理 | python scripts/validate_workspace.py | 通过 | 工作区验证通过 |
| Python/前端/依赖 | compileall、node --check frontend/app.js、pip check、git diff --check | 通过 | 无编译、前端语法、依赖或差异空白错误 |

## 最终结果

- 完成内容：实现 MySQL/SQLite 存储、有界并发、API 状态、配置文档和自动化验证
- 未完成内容：MySQL 生产连接、跨 worker 全局并发和身份隔离不在本 change
- 剩余风险：当前只做本地 SQLite/SQLAlchemy 方言验证，不证明生产网络、权限或容量配置
- 最终验收人：主 Agent
