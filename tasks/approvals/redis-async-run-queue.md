# Redis 异步任务队列依赖审批

关联任务：async-run-queue
状态：已确认

## 操作

- 在项目 `pyproject.toml` 增加运行依赖 `redis>=5,<6`。
- 在开发依赖中增加 `fakeredis[lua]>=2.25,<3`，仅用于自动化测试替身。
- 在项目内新增可选的 `docker-compose.yml` Redis 服务，用于本地演示和集成验证。
- 增加 `REDIS_URL` 等未提交 `.env` 配置示例；不写入真实凭据。

## 对象

- 仅限 `F:\api-test-platform` 仓库文件、项目 `.venv` 依赖和项目定义的 Docker Compose 服务。
- 不连接、读取、修改或部署生产 Redis；不修改系统服务、注册表、全局环境变量或防火墙。

## 理由

异步运行平台需要可靠队列、消费确认、失败重试和 Worker 崩溃接管。Redis Streams 是本 change 采用的任务通道，MySQL 继续保存任务状态和脱敏历史。

## 影响

- 安装项目依赖后，应用可以在配置 Redis 时启用异步提交和 Worker。
- 不配置 Redis 时，现有同步接口和 SQLite/MySQL 历史功能保持可用；异步接口返回稳定的 503。
- 本地 Compose 会增加一个项目级 Redis 容器/卷定义，但不会自动启动。

## 可逆性与回滚

- 删除新增依赖、Compose 文件和 Redis 配置即可回滚代码层变更。
- 停止并删除本 change 创建的本地 Redis 容器/卷即可回滚演示环境；不会触碰仓库外资源。
- 不执行生产数据迁移，因此没有生产回滚操作。

## 不执行的后果

不引入 Redis 时无法提供可靠的异步任务队列、跨 Worker 消费、租约接管和重试能力，只能继续使用现有同步执行模型。

## 用户确认

用户于 2026-08-18 明确确认：允许新增 Redis/RabbitMQ 这类队列服务；本 change 选择 Redis Streams + `redis-py`，并允许继续执行。
