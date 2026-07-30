# ADR 0001：AI 只生成候选用例，并采用后端 Provider 适配器

状态：Accepted
日期：2026-07-30

## 背景

平台需要展示大模型应用开发能力，同时必须保持确定性测试结果、安全的密钥边界和无 Key 可演示性。
OpenAPI 原文可能包含误放的令牌、示例用户数据或内部地址，模型输出也不能直接视为可信测试配置。

## 决策

- 定义统一 `AiProvider.generate` 协议，首批实现 `mock` 与 `openai`。
- 默认启用 Mock Provider，不联网、不需要密钥，也不把 Mock 输出描述成真实模型推理。
- OpenAI Provider 固定调用官方 HTTPS Responses API，使用严格 JSON Schema，并设置 `store=false`。
- API Key 只从后端 `OPENAI_API_KEY` 读取；不提供浏览器密钥输入框，不记录或持久化密钥。
- 调用 Provider 前构造受限结构摘要，移除 server、示例、默认值、枚举值、认证和疑似敏感字段。
- Provider 只返回中间候选结构；后端再校验 operation、路径、查询参数、JSON、大小和用例模型。
- 生成和载入均不执行测试。用户必须人工检查，再单独触发确定性执行器。

## 结果

优点是密钥和执行权限不会交给浏览器或模型，Mock 模式可稳定演示，新增 Provider 只需实现相同协议。
代价是摘要会丢失部分业务语义，AI 候选质量依赖接口结构和测试目标，且真实调用会产生费用和延迟。

## 回滚

把 `AI_PROVIDER` 恢复为 `mock` 并重启应用即可停止所有模型网络调用；确定性 OpenAPI 生成器与测试
执行器不依赖 AI Provider，仍可独立工作。
