# 智能接口自动化测试与质量平台

平台使用确定性规则执行接口测试并判定结果；
大模型只规划为后续的用例生成和失败分析辅助能力，不参与最终通过/失败判定。

## 当前能力

- 通过 HTTP API 提交一组按顺序执行的接口测试用例
- 支持 GET、POST、PUT、PATCH、DELETE
- 支持请求头、查询参数和 JSON 请求体
- 支持状态码、JSON 路径等值、响应时间断言
- 支持运行级变量以及 `{{variable_name}}` 模板
- 支持从前序 JSON 响应提取变量，并在后续路径、请求和断言中复用
- 支持显式 `depends_on` 依赖；前序失败时跳过依赖用例，独立用例继续执行
- 支持从 OpenAPI 3.0/3.1 JSON 对象确定性生成基础正向测试用例草稿
- 缺失变量、提取失败和网络异常返回结构化错误
- 结果只报告成功提取的变量名，不返回变量值或完整请求头
- 使用项目内 SQLite 保存已脱敏运行结果，支持最近记录和详情查询
- 支持从已脱敏历史记录下载独立、离线可读的静态 HTML 测试报告
- 每次运行返回 UUIDv4 `run_id` 和 UTC `created_at`
- 默认拒绝所有网络目标；本地目标和其他目标都必须显式允许
- 提供中文可视化控制台、Swagger UI、演示接口和自动化测试

完整占位符会保留变量原始 JSON 类型，例如 `"expected": "{{user_id}}"` 中的
`user_id` 如果是整数，渲染结果仍为整数。嵌入字符串的占位符会转为文本，例如
`"run-{{user_id}}"`。当前不支持占位符转义，也不会渲染 JSON 对象的键。

## 项目结构

```text
api-test-platform/
├─ src/app/
│  ├─ AGENTS.md             # 后端 Agent 的目录边界
│  ├─ main.py               # FastAPI 接口
│  ├─ schemas.py            # 请求、用例、断言和结果模型
│  └─ services/
│     ├─ executor.py        # 顺序执行、依赖、提取与断言引擎
│     ├─ report_renderer.py # 脱敏静态 HTML 报告渲染
│     └─ templating.py      # 运行变量模板渲染
├─ tests/                   # 自动化测试
├─ frontend/                # 无构建依赖的中文可视化控制台
├─ .codex/                  # 项目级权限、子 Agent 与敏感命令规则
├─ tasks/                   # 活动任务、审批卡和完成记录
├─ contracts/               # 跨 Agent 共享契约
├─ coordination/            # 跨会话持久消息与确认记录
├─ docs/                    # 运行模型、安全审计与项目展示说明
├─ scripts/                 # 消息箱、任务完成门和配置验证
├─ examples/demo-run.json   # 提取和依赖链演示
├─ AGENTS.md                # Codex 安全边界和多 Agent 治理规则
└─ pyproject.toml           # 项目和依赖配置
```

## 本地启动

前置条件：Python 3.11 及以上版本，且项目依赖已经安装到 `.venv`。

默认配置不会允许应用请求任何目标，包括自身的本地演示接口。只在本地开发演示时，
在启动服务的 PowerShell 窗口显式开启本地目标：

```powershell
.\.venv\Scripts\Activate.ps1
$env:ALLOW_LOCAL_TARGETS = 'true'
uvicorn app.main:app --app-dir src --reload
```

浏览器打开：

- 中文测试控制台：<http://127.0.0.1:8000/>
- Swagger UI：<http://127.0.0.1:8000/docs>
- 健康检查：<http://127.0.0.1:8000/api/v1/health>

### 使用中文控制台

1. 打开根页面 `/`。页面默认载入使用当前 origin 的安全两用例演示。
2. 保持“被测服务地址”为当前页面地址，点击“运行测试”。
3. 在“运行结果”查看总数、通过、失败、跳过以及每条用例的响应时间、断言和提取变量名。
4. 在“最近运行”查看本机保存的记录，点击任意记录可重新打开完整安全结果，也可点击“下载报告”
   保存独立 HTML 文件。
5. 点击“加载演示”可恢复默认链路；展开“高级设置：查看或编辑 JSON”可以检查或修改请求。

控制台调用的仍是 `/api/v1/runs`，不会绕过目标访问策略。运行默认本地链路前，启动服务的
PowerShell 窗口仍必须设置 `$env:ALLOW_LOCAL_TARGETS = 'true'`；否则页面会显示目标不允许的错误。
Swagger 调试入口继续保留在 `/docs`。

### 在中文页面生成 OpenAPI 用例

1. 在“从 OpenAPI 生成基础用例”区域点击“加载合成演示”，也可以粘贴 JSON，或选择不超过
   1 MiB 的 `.json` 文件。
2. 按需填写被测地址覆盖值和1～50之间的最大用例数，然后点击“生成基础用例”。
3. 查看生成、跳过数量及结构化 warning，确认生成草稿符合预期。
4. 点击“载入测试编辑器”把草稿复制到现有运行配置；此操作不会自动执行测试。
5. 在测试编辑器中人工检查地址、请求和断言，确认后再手动点击“运行测试”。

页面只支持 OpenAPI 3.0/3.1 JSON，不支持 YAML。原始文档和生成草稿只保存在当前页面内存，
不会写入浏览器存储；刷新页面后即丢失。生成和载入都不会自动执行用例，输入文档、示例及覆盖值
中不得放入真实密钥。

### 运行历史 API

- `GET /api/v1/runs?limit=20`：按时间倒序返回运行摘要，`limit` 范围为1～100。
- `GET /api/v1/runs/{run_id}`：返回一条已保存的完整安全结果。
- `GET /api/v1/runs/{run_id}/report`：下载由该安全结果生成的 UTF-8 HTML 报告。

历史数据库固定保存在 `.data/run-history.sqlite3`，最多保留500条，并已被 Git 忽略。平台只保存
已经过脱敏的 `TestRunResult`，不会保存 `base_url`、请求头、查询参数、请求体或运行变量上下文。
未声明为 secret 的断言值仍可能进入历史，因此测试令牌等敏感变量必须加入 `secret_variables`。
SQLite 历史只适合本机单用户演示，不应当作生产数据库使用。

HTML 报告只读取历史中的 `TestRunResult`，不会重新读取 `base_url`、请求头、请求体、变量值或原始响应。
报告不包含 JavaScript、表单、外链和外部资源，动态文本会先限制长度再进行 HTML 转义；下载响应使用
`no-store`、`nosniff` 和内容安全策略。报告继承历史数据的既有脱敏边界，因此未声明为 secret 的
断言值仍可能出现在报告中。

如果更习惯命令行，也可以在另一个 PowerShell 窗口执行演示测试：

```powershell
$result = Invoke-RestMethod `
  -Method Post `
  -Uri 'http://127.0.0.1:8000/api/v1/runs' `
  -ContentType 'application/json' `
  -InFile '.\examples\demo-run.json'

$result | ConvertTo-Json -Depth 10
```

示例先查询用户并提取 `user_id`、`user_name`，再由依赖用例把这些变量用于请求路径、
请求头、查询参数和断言期望值。请以命令的实际返回结果为准。

结束演示后按 `Ctrl+C` 停止服务。`$env:ALLOW_LOCAL_TARGETS` 只影响当前 PowerShell
进程及其子进程；也可以执行以下命令立即清除：

```powershell
Remove-Item Env:ALLOW_LOCAL_TARGETS -ErrorAction SilentlyContinue
```

## 目标访问安全边界

`base_url` 是服务端发起请求的目标，错误放开可能造成 SSRF 风险，因此平台采用默认拒绝策略：

- `ALLOW_LOCAL_TARGETS=false`：默认值；拒绝 `localhost`、`127.0.0.1` 和 `::1`
- `ALLOW_LOCAL_TARGETS=true`：仅额外允许上述本地主机，仅用于可信的本地开发演示
- `ALLOWED_TARGET_ORIGINS`：以英文逗号分隔的明确 HTTP(S) origin 白名单

例如只允许两个受控测试环境：

```powershell
$env:ALLOWED_TARGET_ORIGINS = 'https://api.example.test,http://192.0.2.10:8080'
```

白名单值必须是 origin（协议、主机和可选端口），不能包含路径、查询参数、片段或凭据。
即便目标在白名单中，也只应使用隔离的测试环境和脱敏测试数据。当前 API 没有身份认证，
不应直接暴露到公网，也不应用于生产系统。

运行变量可能包含令牌。把相应名称加入 `secret_variables`，平台会在执行结果中隐藏敏感值。
不要把真实密钥写入示例、代码或 Git；本地密钥只应放入未提交的 `.env`。

## 请求模型速览

```json
{
  "base_url": "http://127.0.0.1:8000",
  "variables": {
    "expected_user_id": 7
  },
  "secret_variables": [],
  "cases": [
    {
      "id": "fetch_user",
      "name": "查询并提取用户",
      "method": "GET",
      "path": "/api/v1/demo/users/{{expected_user_id}}",
      "depends_on": [],
      "extract": [
        {
          "name": "user_id",
          "path": "data.id",
          "secret": false
        }
      ],
      "assertions": [
        {
          "type": "status_code",
          "expected": 200
        }
      ]
    }
  ]
}
```

规则要点：

- 用例 `id` 在一次运行内唯一；省略时生成 `case_1`、`case_2` 等稳定 ID
- `depends_on` 只能引用更早提交且已存在的用例
- 提取路径是点分隔 JSON 路径，列表索引使用数字，例如 `data.items.0.id`
- 提取只在该用例的断言和全部提取规则都成功后一次性发布
- 不允许覆盖已有变量；依赖失败的用例状态为 `skipped`
- 模板可用于路径、请求头值、查询参数值、JSON 请求体值和断言 `expected`

完整示例见 [examples/demo-run.json](examples/demo-run.json)。

## 从 OpenAPI 生成基础用例

`POST /api/v1/openapi/generate` 接收 OpenAPI 3.0.x 或 3.1.x 的 JSON 对象，确定性生成一个
符合现有 `TestRunRequest` 模型的 `run`。当前支持 GET、POST、PUT、PATCH、DELETE，必填的
路径参数和查询参数、`application/json` 请求体、受限的本地 `#/components/...` 引用，以及
最低显式数字 2xx 响应的状态码断言。示例值按参数或 Schema 的 `example`、`default` 和
确定性类型回退规则生成；无法生成的操作会计入 `skipped_count` 并返回结构化 `warnings`。

请求可以提供 `base_url`；省略时使用文档中的第一个 `servers[0].url`。地址不允许包含凭据、
片段或 server 变量。单次最多生成50条用例；每个 operation 最多生成2048个 Schema 节点，
单条生成用例的 UTF-8 序列化结果不得超过256 KiB，文档、路径数和本地引用深度也有上限。
完整的合成示例见 [examples/openapi-demo.json](examples/openapi-demo.json)，可在服务启动后执行：

```powershell
$generated = Invoke-RestMethod `
  -Method Post `
  -Uri 'http://127.0.0.1:8000/api/v1/openapi/generate' `
  -ContentType 'application/json' `
  -InFile '.\examples\openapi-demo.json'

$generated | ConvertTo-Json -Depth 20
```

生成阶段只解析请求中的 JSON，不会联网获取文档、访问 `base_url` 或执行生成的用例。需要执行时，
应由用户检查生成结果后另行把 `$generated.run` 提交到 `/api/v1/runs`；该请求仍会经过默认拒绝的
SSRF 目标策略。生成器不读取或生成 `securitySchemes` 凭据，也不持久化原始 OpenAPI 文档；
输入文档和示例中不得放入真实密钥。本阶段不支持 Swagger 2、外部 `$ref`、认证凭据、
Header/Cookie 参数、multipart/XML、负向或边界用例生成。

## 运行自动化测试

```powershell
.\.venv\Scripts\python.exe -m pytest --cov=app
```

## GitHub Actions 持续集成

CI 会在面向 `main` 的 Pull Request、推送到 `main` 以及手动触发时，使用 Python 3.11
依次执行 Python 编译、全量测试、分支覆盖率不低于 90%、前端 JavaScript 语法检查和
协作工作区检查。运行结果可在 GitHub 仓库的 **Actions** 页面查看。

本地验证不能替代 GitHub 托管的 Linux 环境；首次真实 CI 结果需要等当前分支推送并触发
工作流后再确认。

协作工作区检查：

```powershell
.\.venv\Scripts\python.exe .\scripts\validate_workspace.py
```

本阶段主 Agent 已确认全量测试为 `171 passed`，分支覆盖率为 `90.65%`。后续代码变化后应重新执行
上述命令，并以当前工作区输出为准。

## 多 Agent 协作

在 Codex 中信任并重新打开本项目后，项目级 `.codex/config.toml` 和自定义 Agent
配置才会加载。界面实时权限可能覆盖项目默认值，敏感任务不要选择完全访问模式。

创建任务卡：

```powershell
.\.venv\Scripts\python.exe .\scripts\new_task.py example-task "任务标题"
```

实时 Agent 通信使用 Codex 原生线程；`coordination/` 用于跨会话审计与恢复。
详细运行方式见 [docs/OPERATING_MODEL.md](docs/OPERATING_MODEL.md)。


## 后续路线

1. YAML/JSON 用例文件导入、环境配置和更完整的密钥管理
2. 测试数据准备与清理
3. 历史筛选、Allure 适配和报告模板扩展
4. OpenAPI 用例生成的边界场景扩展与人工确认流程
5. GitHub Actions、Docker Compose 和独立演示被测服务
6. 大模型辅助边界场景生成及失败日志总结
