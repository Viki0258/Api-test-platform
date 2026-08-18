# Outcome
在保留现有同步单次工作流语义的前提下，让多个客户端可以安全并发提交测试运行；运行历史默认可切换到 MySQL 持久化，并通过应用内有界容量保护服务和历史库。

# Scope
- 增加 DATABASE_URL 配置，使用 SQLAlchemy Core 统一 SQLite 与 MySQL 存储适配；默认无配置时继续使用仓库内 SQLite 演示库。
- 保留现有运行历史 API 的路径、响应字段、脱敏边界和排序语义；MySQL 使用 InnoDB，结果 JSON 使用跨方言可用的 JSON/LONGTEXT 表达。
- 增加单进程内的有界运行容量，默认同时执行 4 个 run；容量耗尽时 POST 返回 429 RUN_CAPACITY_EXCEEDED 和 Retry-After。
- 确保每个 run 的变量、执行器和历史写入上下文彼此隔离；run 内仍按提交顺序和依赖顺序串行。
- 为并发写入、容量释放、SQLite 回归、MySQL 方言编译、错误脱敏和 API 状态补充测试与配置文档。

# Non-goals
- 不增加登录、认证、授权、用户表、租户隔离或按用户过滤历史。
- 不实现跨多 worker 的全局并发租约、MySQL 任务队列、后台异步 202 + run_id 模型、重试队列或分布式锁。
- 不连接、迁移或写入生产数据库；不迁移现有用户历史数据。
- 不改变单个 run 内的依赖判定、请求顺序、目标 SSRF 策略或结果脱敏规则。

# Acceptance examples
- A1: 配置 DATABASE_URL=mysql+pymysql://... 后，历史存储使用 MySQL/InnoDB；未配置时本地 SQLite 演示和现有单元测试仍可用。
- A2: 使用相同 API 并发提交多个 run 时，每个请求都得到自己的 UUIDv4 结果，变量和案例结果不串线，单个 run 内的依赖顺序保持不变。
- A3: 同时运行数达到配置上限（默认 4）时，后续 POST /api/v1/runs 在执行目标请求前返回 HTTP 429、错误码 RUN_CAPACITY_EXCEEDED 和正整数 Retry-After。
- A4: run 在成功、执行失败或历史保存异常时都会释放容量；释放后新的客户端可以进入，且不会因异常永久占用槽位。
- A5: 多个并发完成的 run 写入历史后，列表无丢失、无重复、无损坏，仍按 created_at DESC, sequence DESC 稳定排序；详情可读取完整安全结果。
- A6: SQLite 和 MySQL 存储错误都映射为稳定的 503 历史错误，不向 API 返回驱动错误、凭据、绝对数据库路径或 SQL 细节。
- A7: 历史库只保存已脱敏的 TestRunResult 和摘要字段，不保存 base_url、请求头、查询参数、请求体或运行变量上下文。
- A8: 配置、依赖、数据库初始化/迁移说明和自动化测试覆盖本变更；不需要真实 MySQL 服务也能完成默认验证。

# Constraints and invariants
- Python 3.11、FastAPI、Pydantic 和 HTTPX 的既有版本范围保持兼容；所有密钥只能来自未提交的 .env。
- DATABASE_URL 为空时只能在仓库内 .data/run-history.sqlite3 建立默认 SQLite 文件；不会从任意环境变量构造仓库外本地路径。
- 允许的 MySQL 驱动 URL 使用 mysql+pymysql，生产引擎使用 InnoDB；默认池与连接存活检查由存储层配置。
- 容量限制是应用进程内的有界信号量；不把本进程限制宣称为多 worker 全局限制。
- 槽位只在目标校验通过后获取，且通过 finally 释放；输入校验、目标拒绝和容量拒绝都不发送目标请求、也不写历史。
- 历史写入使用参数化 SQL/SQLAlchemy 表达式和事务；失败时不返回底层异常文本。
- 现有 SQLite 存储测试和响应兼容性优先于新实现的内部结构。

# Decisions
- 采用单一 Native change，不拆分 child；存储、并发容量和 API 状态共同决定用户可见行为，且共享核心边界。
- 采用 SQLAlchemy Core + 明确的表元数据和版本化初始化约束；不引入 ORM 模型或业务实体层。
- DATABASE_URL 是唯一数据库选择入口；未配置时保留 SQLite 适配器，配置 MySQL 时由 PyMySQL 驱动连接。
- 默认 MAX_CONCURRENT_RUNS=4，允许通过配置调整但始终有正数上限；容量不足使用立即失败的 429，不排队。
- 不做身份/租户隔离；所有客户端读取同一历史空间，这是本阶段的明确边界。

# Open questions
本阶段没有待用户决定的问题；MySQL 生产连接、迁移执行和跨 worker 容量租约保留到后续变更。

# Verification expectations
- 先运行新增的存储、容量和 API 并发测试，确认测试在实现前因能力缺失而失败，再实现最小行为并回归。
- 运行完整 pytest --cov=app --cov-branch --cov-report=term-missing --cov-fail-under=90、Python 编译、pip check、前端语法检查和 git diff --check。
- 运行 python scripts/validate_workspace.py 和 python scripts/complete_task.py audit-current-project --check；仅使用 SQLite/SQLAlchemy 方言编译验证，不连接真实 MySQL。
