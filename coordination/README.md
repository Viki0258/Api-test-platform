# Agent 持久通信层

Codex 原生子 Agent 线程负责实时分派、跟进、等待和结果回传。本目录补充一个基于不可变文件的
持久消息箱，用于跨会话恢复、阻塞审计和关键决策追踪。

它不是后台服务，不会轮询或自动唤醒 Agent。

## 设计

- 每条消息是 `messages/<recipient>/<message-id>.json`。
- 消息创建后不可修改。
- 收件确认写入独立的 `acks/<message-id>--<recipient>.json`。
- 非主 Agent 之间直接通信时，系统自动把同一消息抄送到 `main`。
- 文件名只使用程序生成的时间戳和 UUID，任务 ID 与 Agent ID 都经过白名单验证。
- 单条正文最大 32 KiB。
- 消息和确认包含 SHA-256 内容校验，用于发现意外损坏。

## 命令

发送消息：

```powershell
python .\scripts\agent_bus.py send `
  --from-agent frontend `
  --to-agent backend `
  --task user-profile `
  --type request `
  --subject "确认冲突错误码" `
  --body "重复邮箱是否固定返回 HTTP 409？"
```

查看未确认消息：

```powershell
python .\scripts\agent_bus.py inbox --agent backend --unread
```

确认消息：

```powershell
python .\scripts\agent_bus.py ack --agent backend --message <message-id>
```

查看任务消息统计：

```powershell
python .\scripts\agent_bus.py status --task user-profile
```

## 使用规则

- 不要直接编辑消息或确认文件。
- 不要发送密钥、令牌、Cookie、真实用户数据、完整生产日志或其他敏感内容。
- 消息不构成主 Agent 决策；需要决策时将类型设为 `request` 或 `blocker`。
- 主 Agent 作出决策后，使用 `decision` 消息通知受影响 Agent，并在任务卡记录消息 ID。
- `security_reviewer` 处于只读沙箱，其关键结论由主 Agent 代为持久化。

## 信任边界

消息箱不是隔离不同恶意租户的安全系统。所有可写 Agent 共享同一工作区，因此 Agent 身份
不能通过文件系统进行密码学认证，文件不可变性也主要由脚本和治理规则保证。SHA-256 可以发现
意外修改，但无法阻止有工作区写权限的恶意进程重新生成内容。

主 Agent 必须把消息当作可审计线索，并结合原生线程、真实 diff 和验证输出做最终判断。
同理，前端、后端和测试的目录所有权是协作约束；当前稳定 `workspace-write` 沙箱不会为每个
子 Agent 建立不同的目录级 ACL。
