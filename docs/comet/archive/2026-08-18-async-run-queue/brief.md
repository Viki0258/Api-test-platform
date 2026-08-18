# Outcome

将运行执行从“HTTP 请求直接占用应用进程执行”升级为可恢复的异步任务平台：API 负责校验和入队，Redis Streams 负责传递任务，独立 Python Worker 负责执行，MySQL 保存任务状态和脱敏历史。保留现有同步接口，新增异步提交接口，避免破坏已有客户端。

# Scope

- 新增 `REDIS_URL`、队列名称、Worker 并发数、任务租约、重试次数和请求载荷 TTL 配置。
- 新增 `POST /api/v1/runs/async`，复用现有 `TestRunRequest`，返回 HTTP 202、UUIDv4 `run_id` 和状态地址。
- 新增 `GET /api/v1/runs/{run_id}/status`，返回 `queued`、`running`、`completed` 或 `failed` 状态、尝试次数和安全错误摘要。
- 新增 MySQL/SQLite 兼容的 `run_jobs` 和 `run_outbox` 持久化接口；MySQL 部署使用 InnoDB 和事务更新。
- 使用 Redis Streams consumer group 传递只包含 `run_id` 的事件；执行请求载荷只短期存放在带 TTL 的 Redis key 中，不进入运行历史。
- 新增独立 Worker 入口，支持多进程/多实例消费、有限并发、确认、失败重试和过期任务接管。
- 使用状态版本或执行令牌保证重复投递不会重复执行或重复保存历史。
- 保留现有 `POST /api/v1/runs` 同步执行、运行历史路由、单个 run 内依赖顺序和结果脱敏边界。
- 更新配置示例、README、Docker Compose/本地 Redis 运行说明和自动化测试。

# Non-goals

- 本 change 不实现登录、认证、租户隔离、用户表、用户级配额或按用户过滤历史。
- 本 change 不删除或改变现有同步 `POST /api/v1/runs` 的请求/响应契约。
- 本 change 不连接、迁移、重启或部署生产 Redis/MySQL。
- 本 change 不把请求目标、请求头、查询参数、请求体、运行变量或凭据写入运行历史；Redis 的安全部署、TLS、ACL 和磁盘加密属于部署配置边界。
- 本 change 不引入 Celery、RabbitMQ、Kafka 或第二种任务代理；队列实现固定为 `redis-py` + Redis Streams。
- 本 change 不改变 SSRF 目标白名单、模板渲染、依赖判断或结果脱敏规则。

# Acceptance examples

- A1: 配置合法 `REDIS_URL` 后，异步提交服务可初始化 Redis Streams consumer group；缺少 Redis 配置或 Redis 不可用时，异步提交返回稳定的 503 `ASYNC_RUNS_UNAVAILABLE`，不执行目标请求。
- A2: 合法请求调用 `POST /api/v1/runs/async` 时返回 202、UUIDv4 `run_id`、`status_url` 和 `queued` 状态；API 请求线程不调用 `TestExecutor`。
- A3: `GET /api/v1/runs/{run_id}/status` 能反映 queued、running、completed 和 failed 状态；未完成任务不会被伪装成已完成结果。
- A4: 多个 Worker 使用同一个 Redis consumer group 时，每个任务最多只有一个有效执行令牌；重复投递不会产生重复目标请求或重复历史记录。
- A5: Worker 并发数是有界的；多个 Worker 实例的总执行数由 Redis 队列消费和任务租约共同限制，不依赖单个进程内信号量。
- A6: API 创建 MySQL 任务记录后，即使首次 Redis 发布暂时失败，Outbox dispatcher 也能重试发布，任务不会永久丢失。
- A7: Worker 崩溃或租约过期后，另一 Worker 能接管未确认消息；接管遵循最大尝试次数并避免并发重复执行。
- A8: TestExecutor 返回测试失败结果时，任务状态为 completed，结果的 `passed` 为 false；基础设施异常在重试耗尽后才进入 failed，并返回脱敏稳定错误。
- A9: 任务完成后只把已经脱敏的 `TestRunResult` 保存到历史；请求载荷不出现在 `test_runs` 或状态响应中，并在完成后删除或依 TTL 过期。
- A10: 现有同步 `POST /api/v1/runs`、历史列表、详情和报告 API 继续通过原有回归测试；单个 run 内仍按依赖顺序串行。
- A11: MySQL 表定义使用 InnoDB、参数化事务和稳定唯一键；未配置 MySQL 时本地 SQLite 测试仍可运行，但 SQLite 不宣称跨主机全局队列保证。
- A12: 配置、Worker 启动、Redis Compose 演示、状态流转、重试、幂等、并发和错误脱敏均有自动化测试与文档说明。

# Constraints and invariants

- Python 3.11、现有 FastAPI/Pydantic/SQLAlchemy 版本范围和同步执行器保持兼容。
- Redis 依赖只用于异步任务通道；MySQL 仍是任务状态和历史的持久化来源。
- Outbox 记录和任务状态更新使用事务；Redis 发布失败不得让 API 成功响应后任务无记录。
- Redis 事件只携带 `run_id`、尝试序号和版本，不携带请求凭据；执行载荷使用独立 key 并设置有限 TTL。
- 状态更新必须带执行令牌/版本条件；只有持有有效租约的 Worker 能把任务从 running 推进到终态。
- 所有终态路径都必须释放租约、确认或重新安排消息，并保持错误响应不泄露连接串、目标地址、SQL 或驱动细节。
- API 只在输入和目标校验通过后创建任务；无效输入、被禁止目标和队列不可用都不触发目标请求。
- 密钥只能来自未提交 `.env`；不读取、写入或提交真实用户数据和生产凭据。

# Decisions

- 采用 Redis Streams + consumer group + `redis-py`，不引入 Celery 或 RabbitMQ；Worker 入口与 API 进程分离。
- 采用 MySQL `run_jobs` + `run_outbox` 作为任务状态和可靠发布的持久化边界；复用现有 SQLAlchemy Core 风格。
- 保留同步接口，新增异步接口；这是向 202 + run_id 演进而不破坏现有客户端的兼容策略。
- 任务状态使用 `queued`、`running`、`completed`、`failed`；测试断言失败属于 completed，基础设施故障才属于 failed。
- 请求载荷短期存在 Redis，不写历史；Redis ACL/TLS/持久化保护由部署配置负责，本 change 提供最小 TTL 和安全文档。
- 认证、租户隔离和用户级配额单独立项，避免在本 change 中混入身份模型。

# Open questions

无。Redis 作为新增外部服务已获用户确认；异步接口、Outbox、Worker、MySQL 状态和短期载荷边界已确认。

# Verification expectations

- 先为队列状态、Outbox、Redis 发布失败、重复投递、租约接管和异步 API 编写失败测试，再实现最小行为。
- 运行完整 pytest、分支覆盖率不低于 90%、Python 编译、前端语法、pip check、工作区校验和 git diff --check。
- 使用 fakeredis/可控测试替身验证 Redis Streams 语义；不要求连接真实生产 Redis/MySQL。
- 通过 Docker Compose 提供本地 Redis 演示配置，但不自动启动外部服务、不部署、不推送。
