# 智能接口自动化测试与质量平台

这是一个面向测试开发岗位的工程化项目。平台使用确定性规则执行接口测试并判定结果；
大模型只规划为后续的用例生成和失败分析辅助能力，不参与最终通过/失败判定。

## 当前能力

- 通过 HTTP API 提交一组按顺序执行的接口测试用例
- 支持 GET、POST、PUT、PATCH、DELETE
- 支持请求头、查询参数和 JSON 请求体
- 支持状态码、JSON 路径等值、响应时间断言
- 支持运行级变量以及 `{{variable_name}}` 模板
- 支持从前序 JSON 响应提取变量，并在后续路径、请求和断言中复用
- 支持显式 `depends_on` 依赖；前序失败时跳过依赖用例，独立用例继续执行
- 缺失变量、提取失败和网络异常返回结构化错误
- 结果只报告成功提取的变量名，不返回变量值或完整请求头
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
4. 点击“加载演示”可恢复默认链路；展开“高级设置：查看或编辑 JSON”可以检查或修改请求。

控制台调用的仍是 `/api/v1/runs`，不会绕过目标访问策略。运行默认本地链路前，启动服务的
PowerShell 窗口仍必须设置 `$env:ALLOW_LOCAL_TARGETS = 'true'`；否则页面会显示目标不允许的错误。
Swagger 调试入口继续保留在 `/docs`。

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

## 运行自动化测试

```powershell
.\.venv\Scripts\python.exe -m pytest --cov=app
```

协作工作区检查：

```powershell
.\.venv\Scripts\python.exe .\scripts\validate_workspace.py
```

本阶段主 Agent 已确认全量测试为 `64 passed`，分支覆盖率为 `92%`。后续代码变化后应重新执行
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

## 项目展示

面试展示建议、可安全使用的简历表述和现场演示顺序见
[docs/PROJECT_SHOWCASE.md](docs/PROJECT_SHOWCASE.md)。其中的数量和质量指标必须在实际验证后填写，
不要把规划能力写成已完成功能。

## 后续路线

1. YAML/JSON 用例文件导入、环境配置和更完整的密钥管理
2. 测试数据准备与清理
3. SQLite/PostgreSQL 持久化、任务历史和 HTML/Allure 报告
4. OpenAPI 文档导入与基础用例生成
5. GitHub Actions、Docker Compose 和独立演示被测服务
6. 大模型辅助边界场景生成及失败日志总结
