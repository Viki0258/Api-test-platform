# 后端应用目录规则

- 本目录默认由 `backend` Agent 负责。
- 开始实现前读取根目录 `AGENTS.md`、活动任务卡和 `contracts/` 中的冻结契约。
- 当前技术栈为 Python 3.11、FastAPI、Pydantic 和 HTTPX。
- 每个 API 入口都必须检查输入、身份、对象级权限和错误处理。
- 网络访问功能必须防止任意目标访问、SSRF、凭据泄漏和敏感日志落盘。
- 修改后优先运行 `.\.venv\Scripts\python.exe -m pytest --cov=app`。
- 数据库迁移必须包含前向方案、回滚方案和数据影响说明。
- 不得连接生产系统，或在仓库中保存密钥和真实用户数据。
