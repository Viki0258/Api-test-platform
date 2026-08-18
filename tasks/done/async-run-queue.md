# async-run-queue — Redis 异步运行队列与 Worker

状态：DONE

## 目标

在保留现有同步 `POST /api/v1/runs` 的前提下，新增标准异步任务平台：API 返回 `202 + run_id`，Redis Streams 负责任务传递，独立 Worker 执行，MySQL 保存任务状态和脱敏历史。

## 范围

- `REDIS_URL`、队列、租约、重试、TTL 和 Worker 并发配置。
- `POST /api/v1/runs/async` 与 `GET /api/v1/runs/{run_id}/status`。
- `run_jobs`、`run_outbox` 持久化，以及 Redis Streams、Outbox、任务租约和全局容量租约。
- 独立 `python -m app.worker` Worker 入口，失败重试、过期接管、幂等状态更新和结果历史写入。
- Redis 本地 Compose、README、`.env.example` 和自动化测试。

## 非范围

- 登录、认证、租户隔离、用户级配额和历史权限。
- 生产 Redis/MySQL 连接、迁移、部署、推送和外部数据读取。
- 修改现有同步接口、SSRF 策略、模板规则、run 内依赖顺序或结果脱敏边界。

## 验收标准

- [x] 异步提交返回 202、UUIDv4、状态地址，API 不执行目标请求。
- [x] queued/running/completed/failed 状态可查询，测试断言失败属于 completed。
- [x] Redis Streams 多 Worker 消费具备确认、重试、过期接管和重复投递幂等。
- [x] Redis 全局容量租约和 Worker 本地并发均有界，租约在成功/异常/崩溃后可释放或回收。
- [x] Outbox 覆盖 Redis 发布失败，任务不会因发布瞬时失败丢失。
- [x] 请求载荷只在 Redis 短期保存，不进入 MySQL 历史或状态响应；结果只保存已脱敏 `TestRunResult`。
- [x] 现有同步 API、历史 API、依赖顺序和项目质量门回归通过。

## 关键决策

- 使用 `redis-py + Redis Streams`，不引入 Celery/RabbitMQ/Kafka。
- 使用 MySQL `run_jobs` + `run_outbox` 作为持久化边界；SQLite 仅本地回退，不宣称跨主机队列保证。
- 保留同步接口，新增异步接口，渐进迁移客户端。
- 用户已确认允许新增 Redis 服务和项目依赖；具体审批记录见 `tasks/approvals/redis-async-run-queue.md`。

## 文件所有权

| 工作流 | Agent | 文件范围 |
|---|---|---|
| 后端与 Worker | backend | `src/app/**` |
| 测试 | tester | `tests/**` |
| 文档与运行示例 | docs_writer | `README.md`、`.env.example`、`docker-compose.yml` |
| 主协调与契约 | 主 Agent | `tasks/**`、`contracts/**`、`docs/comet/**`、`docs/superpowers/**` |

## 风险

- 本地测试使用 fakeredis，不证明真实 Redis 网络、ACL、TLS、持久化或连接池配置。
- 本 change 不提供身份认证和租户隔离，不能直接作为不可信公网多租户服务。
- Redis 载荷 TTL 必须大于可接受的排队时间，否则任务会以 payload expired 失败。

## 验证记录

| 检查 | 命令或方法 | 结果 | 证据/备注 |
|---|---|---|---|
| Native Shape/Build/Verify/Archive | `comet native` Runtime | 通过 | `async-run-queue` 已完成 Native Build/Verify/Archive，A1–A32 已登记 |
| 设计文档自检 | brief、spec、README 交叉核对 | 通过 | 无 TODO/TBD 占位，方案和验收项一致 |
| 核心队列/Worker/存储回归 | `pytest tests/test_async_run_worker.py tests/test_redis_run_queue.py tests/test_run_queue_store.py tests/test_mysql_storage.py -q` | 通过 | 43 项聚焦测试通过；覆盖租约、fencing、重试、幂等和 MySQL DDL |
| 全量质量门 | `pytest --cov=app --cov-branch --cov-fail-under=90` | 通过 | 281 passed；分支覆盖率 90.23%；保留 1 条既有 Starlette/httpx 弃用警告 |
| Python 编译、依赖、工作区、前端语法 | compileall、pip check、validate_workspace.py、node --check | 通过 | 均通过 |

## 最终结果

- 完成内容：完成 Redis Streams + MySQL/SQLite 持久化的异步运行队列、多 Worker 有界并发、Outbox 原子幂等发布、租约接管与 fencing、基础设施失败重试和安全历史写入；Native Verify 的 A1–A32 全部通过并已归档。
- 剩余风险：尚未连接真实 Redis/MySQL；身份认证、租户隔离、用户配额和生产迁移/部署不在本 change 范围内；Redis 去重键保留策略需要部署侧制定。
