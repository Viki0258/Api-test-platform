# openapi-case-generator — OpenAPI 基础用例生成

状态：DONE

## 目标

把用户提供的 OpenAPI 3.x JSON 对象确定性转换为可直接提交到现有执行器的基础测试运行请求，为后续边界用例生成和测试智能体提供安全、可验证的输入层。

## 范围

- 新增 `POST /api/v1/openapi/generate`
- 支持 OpenAPI 3.0/3.1、五种现有 HTTP 方法、路径/查询参数、JSON 请求体和成功状态码断言
- 支持受限本地 `$ref`、确定性示例值、稳定 ID、警告与跳过统计
- 不发起网络请求，不生成认证凭据
- 补齐服务、API、失败路径测试和展示文档

## 非范围

- Swagger 2、外部 `$ref`、远程文档下载
- 直接执行生成用例
- multipart/form-data、XML、Cookie/Header 参数生成
- 负向/边界/AI 用例生成
- 前端文件上传界面

## 验收标准

- [x] 有效 OpenAPI 3.0/3.1 文档生成可通过 `TestRunRequest` 校验的稳定结果
- [x] `base_url` 遵循请求覆盖值优先、文档首个 server 次之，凭据/片段/变量 URL 被拒绝
- [x] 参数覆盖、示例优先级、URL 编码、JSON body 和最低显式 2xx 状态码符合冻结契约
- [x] 本地 `$ref` 有深度限制，外部/循环/无法解析引用不会触发网络访问
- [x] 文档大小、路径数量、最大用例数、递归深度、每操作2048个生成节点和单用例256KiB均受限
- [x] 原始文档不持久化/记录，securitySchemes、Header/Cookie 认证值和敏感 warning 内容不会进入响应
- [x] 无法生成的操作返回结构化 warning；完全无用例时返回稳定 422 错误
- [x] 全量测试、分支覆盖率90%门槛、CI静态检查和工作区验证通过
- [x] README 与项目展示文档只描述已验证能力

## 共享契约

- 文件：`contracts/openapi-generation-v0.1.yaml`
- 状态：已冻结

## 风险与假设

- OpenAPI 规范范围很大，本阶段只实现明确子集并通过 warning 暴露降级
- 生成结果是“基础正向用例草稿”，不是业务正确性的证明
- server URL 只被解析并返回，不会在生成阶段访问；后续执行仍经过现有 SSRF 白名单
- OpenAPI 示例值可能由提交者自行填写；平台不得把真实密钥放入文档，生成器也不得从安全方案、Header 或 Cookie 生成凭据
- FastAPI 在进入模型前已经解析请求体；1MiB是生成器的序列化文档上限，不替代反向代理或ASGI层的原始请求体限制

## 文件所有权

| 工作流 | Agent | 可修改范围 | 依赖 | 状态 |
|---|---|---|---|---|
| 主协调 | 主 Agent | `tasks/**`, `contracts/**`, 审批记录 | 无 | 完成 |
| 后端实现 | backend | `src/app/schemas.py`, `src/app/main.py`, `src/app/services/openapi_generator.py` | 冻结契约 | 完成 |
| 测试实现 | tester | `tests/test_openapi_generator.py`, `tests/test_openapi_api.py` | 冻结契约 | 完成 |
| 文档 | docs_writer | `README.md`, `docs/PROJECT_SHOWCASE.md`, `examples/openapi-demo.json` | 后端响应模型 | 完成 |
| 安全复核 | security_reviewer | 只读全量 diff | 实现完成 | 完成 |

## 子任务交接

- backend：严格按冻结契约实现模型、服务与 API；不得修改测试/文档，不得发起网络请求或新增依赖。
- tester：从契约独立设计服务与 API 测试；不得修改生产代码，不得通过跳过或放宽断言修复失败。
- docs_writer：只写已验证的子集、限制和演示示例；不得声称支持外部引用、AI 或直接执行。
- security_reviewer：只读检查输入规模、递归/引用、URL、网络访问、凭据与错误信息风险。

## 通信记录

- backend 完成模型、API 和确定性生成器；最终全量测试145项通过。
- tester 独立补齐40项 OpenAPI 服务/API测试，覆盖密钥不回显、无网络/执行/持久化、资源预算与失败路径。
- docs_writer 完成 README、项目展示和合成示例，并同步真实指标。
- security_reviewer 首轮发现 Schema 分支递归可指数放大的 P1 阻塞；后端加入2048节点和256KiB预算，复审确认 P1 关闭且无新阻塞。
- 主 Agent 检查真实 diff、重跑质量门并用 `examples/openapi-demo.json` 实际调用接口，生成2条稳定用例。

## 用户审批

- 本阶段仅本地开发和提交，不推送；无需额外审批。

## 验证记录

| 检查 | 命令或方法 | 结果 | 证据/备注 |
|---|---|---|---|
| Python 编译 | `python -m compileall -q src tests scripts` | 通过 | 无语法错误 |
| OpenAPI 定向测试 | `python -m pytest tests/test_openapi_generator.py tests/test_openapi_api.py` | 通过 | 40 passed |
| 全量测试与分支覆盖率 | `python -m pytest --cov=app --cov-branch --cov-report=term-missing --cov-fail-under=90` | 通过 | 145 passed；90.52% |
| 前端语法 | `node --check frontend/app.js` | 通过 | 退出码0 |
| 工作区治理 | `python scripts/validate_workspace.py` | 通过 | 5个自定义 Agent 配置有效 |
| 示例 JSON | `python -m json.tool examples/openapi-demo.json` | 通过 | 合成数据语法有效 |
| 示例联调 | TestClient POST `/api/v1/openapi/generate` | 通过 | HTTP 200；生成 `getUser`、`createBooking` |
| 密钥模式扫描 | 当前变更文件常见密钥格式扫描 | 通过 | 未命中；仓库既有唯一命中是安全测试中的合成 AWS key |
| 安全复核 | 独立只读初审与修复后复审 | 通过 | P1资源放大已关闭；无剩余阻塞 |
| Diff 格式 | `git diff --check` | 通过 | 仅本地行尾转换提示 |

## 最终结果

- 完成内容：新增安全受限的 OpenAPI 3.0/3.1 基础用例生成 API、40项专项测试、合成示例和展示文档
- 未完成内容：Swagger 2、外部引用、认证、负向/边界/AI生成和前端上传仍为后续阶段
- 剩余风险：完整 OpenAPI 规范超出当前子集；理论总响应可接近12.5MiB；1MiB限制发生在FastAPI解析请求体之后；普通query/body示例仍必须由用户保证不含真实密钥
- 最终验收人：主 Agent
