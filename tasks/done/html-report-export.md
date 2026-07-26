# html-report-export — 脱敏 HTML 测试报告导出

状态：DONE

创建日期：2026-07-26

## 目标

让用户可以从最近运行记录下载一份独立、可离线打开且不含请求凭据的 HTML 测试报告，
补齐“生成用例—执行—历史—报告”的项目闭环。

## 范围

- 新增历史运行 HTML 报告端点
- 报告仅使用已保存的 `TestRunResult` 安全模型
- 报告展示运行汇总、用例状态、耗时、断言、错误、跳过原因和提取变量名
- 控制台每条有效历史记录提供同源“下载报告”入口
- 报告和下载响应提供离线 XSS 防护、禁止缓存与稳定文件名
- 更新自动化测试、README 和项目展示文档

## 非范围

- PDF、Allure、邮件发送、云端上传和报告模板自定义
- 保存请求头、请求体、运行变量或原始响应
- 修改历史数据库结构、认证模型或目标访问策略
- 报告内 JavaScript、外部字体、图片或其他网络资源

## 验收标准

- [x] `GET /api/v1/runs/{run_id}/report` 下载 UTF-8 HTML，文件名只含固定前缀和 UUID
- [x] 无效、非 UUIDv4 或不存在的运行 ID 返回既有 `RUN_NOT_FOUND`
- [x] 历史存储不可用返回既有 `HISTORY_STORAGE_UNAVAILABLE`
- [x] 报告只渲染已脱敏 `TestRunResult`，动态文本全部 HTML 转义且长值受限
- [x] HTML 不含脚本、外部资源、表单或可点击外链，并内置严格 CSP 元信息
- [x] HTTP 响应包含 `no-store`、`nosniff`、CSP 和 attachment 下载头
- [x] 控制台只为合法 UUID 历史项构造同源报告链接，不使用 HTML 字符串注入
- [x] 点击“查看详情”仍保持原行为，下载入口键盘可访问且移动端不溢出
- [x] 测试覆盖正常、404、503、XSS、Unicode、长值、秘密边界和前端链接安全
- [x] 全量测试、分支覆盖率90%、JS语法、工作区验证和视觉验收通过
- [x] README、项目展示和诚实边界与真实实现一致

## 共享契约

- 文件：`contracts/html-report-v0.1.yaml`
- 状态：已冻结

## 风险与假设

- 历史结果已是唯一允许的报告数据源；不重新读取执行请求或原始响应
- 未声明为 secret 的断言值仍可能已进入历史，报告会继承这项既有边界
- 报告是单文件静态 HTML，最多50个用例；每个动态长值会截断以限制内存和文件体积

## 文件所有权

| 工作流 | Agent | 可修改范围 | 依赖 | 状态 |
|---|---|---|---|---|
| 主协调 | 主 Agent | `tasks/**`, `contracts/**` | 无 | 已完成 |
| 后端实现 | backend | `src/app/main.py`, `src/app/services/report_renderer.py` | 冻结契约 | 已完成 |
| 前端实现 | frontend | `frontend/index.html`, `frontend/app.js`, `frontend/styles.css` | 冻结契约 | 已完成 |
| 测试实现 | tester | `tests/test_html_report.py`, `tests/test_visual_console.py` | 冻结契约 | 已完成 |
| 文档 | docs_writer | `README.md`, `docs/PROJECT_SHOWCASE.md` | 实现和测试结果 | 已完成 |
| 安全复核 | security_reviewer | 只读全量 diff | 实现完成 | 已完成 |

## 子任务交接

- backend：实现纯函数 HTML 渲染器和下载端点；禁止读取请求原文、添加外部资源或修改数据库。
- frontend：为合法 UUID 历史项创建安全同源下载链接；禁止 blob、外部导航、HTML 字符串注入和存储。
- tester：按冻结契约独立验证 API、XSS、响应头、长值和前端安全边界；不得放宽既有断言。
- docs_writer：只描述真实完成的 HTML 报告能力和剩余限制，不声称支持 PDF/Allure/发送。
- security_reviewer：只读检查报告注入、CSP、响应拆分、缓存、敏感字段和前端 URL 构造。

## 通信记录

- 主 Agent 将报告数据源限定为已脱敏历史结果，不允许接触原始执行请求。
- backend 完成纯函数渲染器、下载端点和既有404/503语义复用。
- frontend 完成合法 UUIDv4 同源下载入口，并保留详情按钮行为。
- tester 新增11项报告契约与恶意输入测试；全量171项通过。
- security_reviewer 只读复核数据源、XSS、CSP、响应头、UUID和前端URL构造，结论无阻塞。
- 主 Agent 完成真实 diff、390px控制台与报告视觉验收及密钥模式扫描。

## 用户审批

- 本地实现和验证无需额外审批；远程发布按项目审批规则另建卡。

## 验证记录

| 检查 | 命令或方法 | 结果 | 证据/备注 |
|---|---|---|---|
| Python编译 | `python -m compileall -q src tests scripts` | 通过 | 无语法错误 |
| 全量测试与分支覆盖率 | `pytest --cov=app --cov-branch --cov-fail-under=90` | 通过 | 171 passed；90.65% |
| 报告契约与前端定向测试 | `pytest tests/test_html_report.py tests/test_visual_console.py` | 通过 | 24 passed |
| JavaScript语法 | `node --check frontend/app.js` | 通过 | 无语法错误 |
| 工作区规则 | `python scripts/validate_workspace.py` | 通过 | Agent和契约结构有效 |
| Git差异格式 | `git diff --check` | 通过 | 仅有Windows换行提示 |
| 密钥模式扫描 | 对全部变更文件执行常见凭据模式扫描 | 通过 | 未发现疑似真实密钥 |
| 安全复核 | 恶意HTML、长值、404/503和完整diff只读检查 | 通过 | 无阻塞项 |
| 控制台移动验收 | Edge CDP 390px视口 | 通过 | 下载入口可见且无横向溢出 |
| 报告移动验收 | Edge CDP 390px视口 | 通过 | Unicode正常；无脚本/外部资源；无横向溢出 |

## 最终结果

- 完成内容：脱敏历史报告端点、独立静态HTML、控制台下载入口、XSS/CSP/缓存防护、测试与文档
- 未完成内容：无；PDF、Allure、发送和模板自定义按范围明确不支持
- 剩余风险：未声明为 secret 的断言值可能已存在于历史结果中
- 最终验收人：主 Agent
