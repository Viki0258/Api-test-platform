# github-actions-ci — 接入 GitHub Actions 持续集成

状态：DONE

## 目标

让每次面向 `main` 的 PR 和合入 `main` 的提交自动运行同一套质量门，并在 GitHub 页面给出明确的通过或失败结果。

## 范围

- 新增最小权限的 GitHub Actions 工作流
- 自动执行 Python 编译、全量测试、分支覆盖率门槛、前端 JavaScript 语法检查和工作区治理检查
- 为工作流契约增加本地回归测试
- 在 README 说明 CI 的触发条件和结果查看方式

## 非范围

- 自动部署、发布、推送代码或上传测试数据
- 使用 GitHub Secrets 或第三方服务
- Docker、OpenAPI 导入、智能体功能实现
- 配置 GitHub 分支保护规则

## 验收标准

- [x] `push main`、`pull_request main` 和手动触发均已声明
- [x] 工作流权限只有 `contents: read`，检出后不保留 Git 凭据
- [x] 使用 Python 3.11 安装 `.[dev]`
- [x] 自动运行编译、105 个当前测试、分支覆盖率不低于 90%、JS 语法和工作区检查
- [x] 工作流有 10 分钟超时和同分支旧运行自动取消机制
- [x] 本地回归测试验证关键 CI 契约，完整测试通过
- [x] README 与实际工作流一致

## 共享契约

- 文件：`contracts/ci-v0.1.yaml`
- 状态：已冻结

## 风险与假设

- GitHub 托管 runner 的环境可能更新，因此固定 Python 3.11，并把第三方 Action 固定到已发布提交 SHA
- 本机只能验证 YAML 结构和实际命令；首次真实 Linux CI 结果需要推送 PR 后在 GitHub 验证
- 当前项目没有锁文件，依赖安装仍受 `pyproject.toml` 中允许版本范围影响

## 文件所有权

| 工作流 | Agent | 可修改范围 | 依赖 | 状态 |
|---|---|---|---|---|
| 主协调 | 主 Agent | `tasks/**`, `contracts/**`, `.github/workflows/**` | 无 | 完成 |
| 测试 | tester | `tests/test_ci_workflow.py` | 冻结契约 | 完成 |
| 文档 | docs_writer | `README.md` | 工作流实现 | 完成 |
| 安全复核 | security_reviewer | 只读全量 diff | 工作流实现 | 完成 |

## 子任务交接

- tester：从 `contracts/ci-v0.1.yaml` 推导工作流静态测试；只能修改 `tests/test_ci_workflow.py`；只读工作流、契约与 `pyproject.toml`；不得放宽覆盖率或跳过测试；验收命令为 `python -m pytest tests/test_ci_workflow.py`。
- docs_writer：在 README 增加简短 CI 说明；只能修改 `README.md`；只读工作流和契约；不得声称尚未在 GitHub 上实际运行成功。
- security_reviewer：只读检查权限、凭据、第三方 Action 固定方式、触发器和命令注入风险，向主 Agent 返回发现。

## 通信记录

- tester 完成6个工作流静态回归测试，并兼容引用形式的 `"on"` YAML 键。
- docs_writer 完成 README CI 说明，未把尚未发生的云端运行描述为成功。
- security_reviewer 只读确认最小权限、静态命令、无 secrets、无部署、无凭据持久化且无命令注入阻塞；主 Agent 采纳不启用依赖缓存的建议，并使用官方当前发布版本的40位提交 SHA。

## 用户审批

- 当前仅创建本地分支和本地提交，不推送；无需审批。后续由用户手动推送并创建 PR。

## 验证记录

| 检查 | 命令或方法 | 结果 | 证据/备注 |
|---|---|---|---|
| Python 编译 | `python -m compileall -q src tests scripts` | 通过 | 无语法错误 |
| 全量测试与分支覆盖率 | `python -m pytest --cov=app --cov-branch --cov-report=term-missing --cov-fail-under=90` | 通过 | 105 passed；总覆盖率91.90% |
| 前端语法 | `node --check frontend/app.js` | 通过 | 退出码0 |
| 工作区治理 | `python scripts/validate_workspace.py` | 通过 | 5个自定义 Agent 配置有效 |
| Diff 格式 | `git diff --check` | 通过 | 仅提示 README 的本地行尾转换，不存在空白错误 |
| 安全复核 | security_reviewer 只读检查 | 通过 | 无阻塞；无写权限、Secrets、缓存、Artifact或部署 |

## 最终结果

- 完成内容：新增最小权限 CI、冻结契约、6个静态回归测试和 README 使用说明
- 未完成内容：云端工作流尚未触发，等待用户手动推送并创建 PR
- 剩余风险：首次 GitHub 托管 runner 的真实结果需在 PR 中确认；滚动 runner 和未锁定的 Python 间接依赖可能随时间变化
- 最终验收人：主 Agent
