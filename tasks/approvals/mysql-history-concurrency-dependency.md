# 审批请求：增加 MySQL 历史存储与并发控制依赖

状态：已获用户确认（记录于当前任务确认）

关联任务：audit-current-project

## 操作

- 具体操作：在项目依赖中增加 SQLAlchemy Core 和 PyMySQL；添加 DATABASE_URL 配置，使应用可选择 MySQL 历史存储；在项目 .venv 中安装/验证依赖。
- 执行对象：pyproject.toml、src/app/**、本地 .venv；仅使用合成测试数据和 SQLite/SQLAlchemy 方言编译。
- 执行环境：当前仓库 F:\api-test-platform，不连接生产数据库、不执行部署或生产迁移。

## 为什么需要

当前历史存储是单机 SQLite，无法作为多客户端部署的共享历史库；当前应用也没有明确的 run 容量边界。SQLAlchemy 提供 SQLite/MySQL 的同一存储接口，PyMySQL 提供 MySQL 驱动，有界信号量保护应用进程资源。

## 可能影响

- 数据：新 MySQL 实例将创建空的 test_runs 表；现有 SQLite 数据不自动迁移。
- 服务：配置 DATABASE_URL 后，历史读写依赖 MySQL 可用性；未配置仍走 SQLite。
- 用户：容量不足时新增 POST 请求得到 429，客户端需按 Retry-After 重试。
- 成本：生产部署需要 MySQL 实例、连接池和备份策略；本次不创建付费资源。
- 协作状态：更新共享契约和 Native change，保持 API 路径兼容。

## 可逆性与回滚

- 代码和依赖变更可通过回退本 change 恢复 SQLite-only 行为；本次不迁移或删除已有数据。
- MySQL 回滚方案：停止使用 DATABASE_URL 并保留实例备份；仅在得到单独批准后删除 change 创建的表。
- 迁移影响：前向创建 test_runs 及排序索引，初始数据为空；回滚只删除本 change 归属对象。

## 不执行的后果与替代方案

- 不执行将无法提供共享 MySQL 历史和明确的多客户端容量保护，只能继续依赖本机 SQLite 与隐式线程池行为。
- 替代方案是继续使用 SQLite 并限制部署为单进程单用户，但不满足已确认目标。

## 用户决定

- [x] 批准以上具体操作（用户已确认“多客户端并发运行 + MySQL 历史存储 + 有界并发”）
- [ ] 拒绝
- [ ] 要求修改方案
- 决定时间：2026-08-14
- 附加限制：仅当前仓库和项目 .venv；不连接生产、不读取真实密钥、不部署、不推送。

## 执行记录

- 执行人：主 Agent
- 实际执行内容：待实现
- 结果：待验证
- 验证：待验证
