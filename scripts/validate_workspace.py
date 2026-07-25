from __future__ import annotations

import hashlib
import json
import re
import sys
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
EXPECTED_AGENTS = {
    "frontend": "workspace-write",
    "backend": "workspace-write",
    "tester": "workspace-write",
    "security_reviewer": "read-only",
    "docs_writer": "workspace-write",
}

REQUIRED_PATHS = [
    "AGENTS.md",
    "README.md",
    "src/app/AGENTS.md",
    "tests/AGENTS.md",
    ".codex/config.toml",
    ".codex/rules/sensitive.rules",
    ".codex/agents/frontend.toml",
    ".codex/agents/backend.toml",
    ".codex/agents/tester.toml",
    ".codex/agents/security-reviewer.toml",
    ".codex/agents/docs-writer.toml",
    "contracts/api-contract.yaml",
    "tasks/TASK_TEMPLATE.md",
    "tasks/APPROVAL_TEMPLATE.md",
    "docs/OPERATING_MODEL.md",
    "docs/SECURITY_AUDIT.md",
    "coordination/README.md",
    "coordination/AGENTS.md",
    "scripts/agent_bus.py",
    "scripts/complete_task.py",
]


def fail(message: str, errors: list[str]) -> None:
    errors.append(message)


def warn(message: str, warnings: list[str]) -> None:
    warnings.append(message)


def calculated_content_hash(payload: dict[str, object]) -> str:
    unsigned = dict(payload)
    unsigned.pop("content_sha256", None)
    canonical = json.dumps(
        unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_config(root: Path, errors: list[str]) -> None:
    config_path = root / ".codex" / "config.toml"
    if not config_path.is_file():
        return
    try:
        with config_path.open("rb") as handle:
            config = tomllib.load(handle)
    except (tomllib.TOMLDecodeError, OSError) as exc:
        fail(f".codex/config.toml 无法解析：{exc}", errors)
        return

    expected_scalars = {
        "approval_policy": "on-request",
        "approvals_reviewer": "user",
        "sandbox_mode": "workspace-write",
        "allow_login_shell": False,
    }
    for key, expected in expected_scalars.items():
        if config.get(key) != expected:
            fail(f"{key} 必须为 {expected!r}", errors)

    workspace = config.get("sandbox_workspace_write", {})
    if workspace.get("network_access") is not False:
        fail("sandbox_workspace_write.network_access 必须为 false", errors)

    environment = config.get("shell_environment_policy", {})
    if environment.get("inherit") != "core":
        fail("shell_environment_policy.inherit 必须为 core", errors)
    if environment.get("ignore_default_excludes") is not False:
        fail("shell_environment_policy.ignore_default_excludes 必须为 false", errors)
    excludes = set(environment.get("exclude", []))
    for required in ("*PASSWORD*", "*CREDENTIAL*", "AWS_*", "OPENAI_*"):
        if required not in excludes:
            fail(f"shell 环境排除列表缺少 {required}", errors)

    apps = config.get("apps", {}).get("_default", {})
    if apps.get("approvals_reviewer") != "user":
        fail("apps._default.approvals_reviewer 必须为 user", errors)
    if apps.get("default_tools_approval_mode") != "writes":
        fail("连接器默认审批模式必须为 writes", errors)

    agents = config.get("agents", {})
    if agents.get("enabled") is not True:
        fail("agents.enabled 必须为 true", errors)
    concurrency = agents.get("max_concurrent_threads_per_session")
    if not isinstance(concurrency, int) or not 1 <= concurrency <= 4:
        fail("子 Agent 并发数必须是 1 到 4 的整数", errors)
    if config.get("features", {}).get("multi_agent") is not True:
        fail("features.multi_agent 必须为 true", errors)


def validate_agents(root: Path, errors: list[str]) -> set[str]:
    names: set[str] = set()
    agents_dir = root / ".codex" / "agents"
    if not agents_dir.is_dir():
        return names

    for path in sorted(agents_dir.glob("*.toml")):
        try:
            with path.open("rb") as handle:
                agent = tomllib.load(handle)
        except (tomllib.TOMLDecodeError, OSError) as exc:
            fail(f"{path.relative_to(root)} 无法解析：{exc}", errors)
            continue

        for field in ("name", "description", "developer_instructions"):
            value = agent.get(field)
            if not isinstance(value, str) or not value.strip():
                fail(f"{path.relative_to(root)} 缺少非空字段 {field}", errors)

        name = agent.get("name")
        if not isinstance(name, str):
            continue
        if name in names:
            fail(f"Agent 名称重复：{name}", errors)
        names.add(name)
        expected_sandbox = EXPECTED_AGENTS.get(name)
        if expected_sandbox is None:
            fail(f"存在未登记的自定义 Agent：{name}", errors)
        elif agent.get("sandbox_mode") != expected_sandbox:
            fail(f"{name} 的 sandbox_mode 必须为 {expected_sandbox}", errors)

        instructions = agent.get("developer_instructions", "")
        if name == "security_reviewer":
            if "只读" not in instructions or "主 Agent" not in instructions:
                fail("security_reviewer 必须明确只读并向主 Agent 回传", errors)
        elif "agent_bus.py" not in instructions:
            fail(f"{name} 未说明持久消息箱使用方式", errors)

    missing = set(EXPECTED_AGENTS) - names
    extra = names - set(EXPECTED_AGENTS)
    if missing:
        fail(f"缺少 Agent：{', '.join(sorted(missing))}", errors)
    if extra:
        fail(f"多出未登记 Agent：{', '.join(sorted(extra))}", errors)
    return names


def validate_rules(root: Path, errors: list[str]) -> None:
    rules_path = root / ".codex" / "rules" / "sensitive.rules"
    if not rules_path.is_file():
        return
    rules = rules_path.read_text(encoding="utf-8")
    try:
        compile(rules, str(rules_path), "exec")
    except SyntaxError as exc:
        fail(f"敏感规则语法无效：{exc}", errors)
        return
    required_fragments = (
        'pattern = ["git", "push"]',
        'pattern = ["git", "reset", "--hard"]',
        'pattern = ["git", ["restore", "rm"]]',
        'pattern = ["terraform", ["apply", "destroy", "import"]]',
        'pattern = ["kubectl", ["apply", "delete", "rollout", "scale"]]',
    )
    for fragment in required_fragments:
        if fragment not in rules:
            fail(f"敏感规则缺少：{fragment}", errors)

    blocks = re.findall(r"prefix_rule\((.*?)^\)", rules, flags=re.DOTALL | re.MULTILINE)
    if len(blocks) < 15:
        fail("敏感命令规则数量异常，预期至少 15 条", errors)
    for index, block in enumerate(blocks, start=1):
        if 'decision = "prompt"' not in block:
            fail(f"第 {index} 条规则不是 prompt", errors)
        if "match = [" not in block:
            fail(f"第 {index} 条规则缺少内联 match 测试", errors)


def validate_coordination(
    root: Path, errors: list[str], warnings: list[str]
) -> None:
    bus_agents = {"main", *EXPECTED_AGENTS}
    for agent in bus_agents:
        directory = root / "coordination" / "messages" / agent
        if not directory.is_dir():
            fail(f"缺少消息箱目录：{directory.relative_to(root)}", errors)
            continue
        for path in directory.glob("*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                fail(f"消息文件损坏 {path.relative_to(root)}：{exc}", errors)
                continue
            if payload.get("id") != path.stem:
                fail(f"消息 ID 与文件名不一致：{path.relative_to(root)}", errors)
            if payload.get("delivered_to") != agent:
                fail(f"消息投递对象与目录不一致：{path.relative_to(root)}", errors)
            if payload.get("schema_version") != 1:
                fail(f"未知消息 schema：{path.relative_to(root)}", errors)
            if payload.get("content_sha256") != calculated_content_hash(payload):
                fail(f"消息内容校验失败：{path.relative_to(root)}", errors)

    ack_dir = root / "coordination" / "acks"
    if not ack_dir.is_dir():
        fail("缺少 coordination/acks 目录", errors)
    else:
        for path in ack_dir.glob("*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                fail(f"确认文件损坏 {path.relative_to(root)}：{exc}", errors)
                continue
            expected_name = (
                f"{payload.get('message_id')}--{payload.get('agent')}.json"
            )
            if path.name != expected_name:
                fail(f"确认文件名与内容不一致：{path.relative_to(root)}", errors)
            if payload.get("content_sha256") != calculated_content_hash(payload):
                fail(f"确认内容校验失败：{path.relative_to(root)}", errors)

    pending = []
    approval_dir = root / "tasks" / "approvals"
    if approval_dir.is_dir():
        for path in approval_dir.glob("*.md"):
            text = path.read_text(encoding="utf-8")
            task_match = re.search(
                r"^关联任务：([a-z0-9]+(?:-[a-z0-9]+)*)$",
                text,
                flags=re.MULTILINE,
            )
            if task_match is None:
                fail(
                    f"审批卡缺少有效关联任务：{path.relative_to(root)}",
                    errors,
                )
            if "状态：等待用户决定" in text:
                pending.append(path.name)
    if pending:
        warn(f"存在待用户决定的审批卡：{', '.join(sorted(pending))}", warnings)


def validate_task_cards(root: Path, errors: list[str], warnings: list[str]) -> None:
    active_dir = root / "tasks" / "active"
    active_ids: set[str] = set()
    if active_dir.is_dir():
        for path in active_dir.glob("*.md"):
            active_ids.add(path.stem)
            text = path.read_text(encoding="utf-8")
            if not re.search(
                r"^状态：(INTAKE|CONTRACT|IMPLEMENT|REVIEW|VERIFY)$",
                text,
                flags=re.MULTILINE,
            ):
                fail(f"活动任务状态无效：{path.relative_to(root)}", errors)
            if "状态：VERIFY" in text:
                warn(f"任务已到 VERIFY，尚未通过完成门：{path.name}", warnings)

    done_dir = root / "tasks" / "done"
    done_ids: set[str] = set()
    if done_dir.is_dir():
        for path in done_dir.glob("*.md"):
            done_ids.add(path.stem)
            text = path.read_text(encoding="utf-8")
            if "状态：DONE" not in text:
                fail(f"完成任务状态不是 DONE：{path.relative_to(root)}", errors)
            if re.search(r"^\s*-\s*\[\s\]\s+", text, flags=re.MULTILINE):
                fail(f"完成任务仍有未勾选项：{path.relative_to(root)}", errors)
    duplicates = active_ids & done_ids
    if duplicates:
        fail(f"活动与完成目录存在重复任务：{', '.join(sorted(duplicates))}", errors)


def validate_workspace(
    root: Path = ROOT,
) -> tuple[list[str], list[str], set[str]]:
    errors: list[str] = []
    warnings: list[str] = []

    for relative in REQUIRED_PATHS:
        path = root / relative
        if not path.is_file():
            fail(f"缺少必需文件：{relative}", errors)

    validate_config(root, errors)
    names = validate_agents(root, errors)
    validate_rules(root, errors)
    validate_coordination(root, errors, warnings)
    validate_task_cards(root, errors, warnings)
    return errors, warnings, names


def main() -> int:
    errors, warnings, names = validate_workspace()

    if errors:
        print("工作区验证失败：")
        for error in errors:
            print(f"  - {error}")
        return 1

    print("工作区验证通过。")
    print(f"根目录：{ROOT}")
    print(f"自定义 Agent：{', '.join(sorted(names))}")
    if warnings:
        print("提醒：")
        for warning in warnings:
            print(f"  - {warning}")
    print("提醒：项目必须在 Codex 中标记为可信，项目 .codex 配置才会加载。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
