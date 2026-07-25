from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import agent_bus  # noqa: E402
import complete_task  # noqa: E402
import validate_workspace  # noqa: E402


VALID_TASK = """\
# example — 示例

状态：VERIFY

## 验收标准

- [x] 功能通过

## 验证记录

| 检查 | 命令或方法 | 结果 | 证据/备注 |
|---|---|---|---|
| 单元测试 | python -m unittest | 通过 | 全部通过 |

## 最终结果

- 完成内容：实现并验证示例
- 未完成内容：无
- 剩余风险：无
- 最终验收人：主 Agent
"""


class AgentBusTests(unittest.TestCase):
    def test_direct_message_is_copied_to_main_and_can_be_acknowledged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            message_id = agent_bus.send_message(
                root,
                "frontend",
                "backend",
                "example",
                "request",
                "确认契约",
                "请确认 HTTP 409。",
            )

            backend_message = (
                root
                / "coordination"
                / "messages"
                / "backend"
                / f"{message_id}.json"
            )
            main_copy = (
                root
                / "coordination"
                / "messages"
                / "main"
                / f"{message_id}.json"
            )
            self.assertTrue(backend_message.is_file())
            self.assertTrue(main_copy.is_file())

            payload = json.loads(main_copy.read_text(encoding="utf-8"))
            stored_hash = payload.pop("content_sha256")
            self.assertEqual(stored_hash, agent_bus.content_hash(payload))
            self.assertTrue(payload["copied_to_main"])

            self.assertEqual(len(agent_bus.inbox(root, "backend", unread_only=True)), 1)
            agent_bus.acknowledge(root, "backend", message_id)
            self.assertEqual(agent_bus.inbox(root, "backend", unread_only=True), [])

    def test_rejects_path_traversal_oversized_body_and_secret(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaises(agent_bus.BusError):
                agent_bus.send_message(
                    root,
                    "frontend",
                    "main",
                    "../escape",
                    "info",
                    "bad",
                    "body",
                )
            with self.assertRaises(agent_bus.BusError):
                agent_bus.send_message(
                    root,
                    "frontend",
                    "main",
                    "example",
                    "info",
                    "large",
                    "x" * (agent_bus.MAX_BODY_BYTES + 1),
                )
            with self.assertRaises(agent_bus.BusError):
                agent_bus.send_message(
                    root,
                    "frontend",
                    "main",
                    "example",
                    "info",
                    "secret",
                    "AKIA1234567890ABCDEF",
                )

    def test_refuses_to_overwrite_ack(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            message_id = agent_bus.send_message(
                root,
                "main",
                "backend",
                "example",
                "decision",
                "决定",
                "采用方案 A。",
            )
            agent_bus.acknowledge(root, "backend", message_id)
            with self.assertRaises(agent_bus.BusError):
                agent_bus.acknowledge(root, "backend", message_id)


class CompletionGateTests(unittest.TestCase):
    def make_root(self, temporary: str) -> Path:
        root = Path(temporary)
        (root / "tasks" / "active").mkdir(parents=True)
        (root / "tasks" / "done").mkdir(parents=True)
        (root / "tasks" / "approvals").mkdir(parents=True)
        return root

    def test_valid_task_moves_to_done(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_root(temporary)
            source = root / "tasks" / "active" / "example.md"
            source.write_text(VALID_TASK, encoding="utf-8")

            self.assertEqual(
                complete_task.complete_task(root, "example", check_only=True),
                source,
            )
            destination = complete_task.complete_task(root, "example")
            self.assertFalse(source.exists())
            self.assertTrue(destination.is_file())
            self.assertIn(
                "状态：DONE", destination.read_text(encoding="utf-8")
            )

    def test_blocks_unchecked_acceptance_and_pending_approval(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_root(temporary)
            source = root / "tasks" / "active" / "example.md"
            source.write_text(
                VALID_TASK.replace("- [x] 功能通过", "- [ ] 功能通过"),
                encoding="utf-8",
            )
            with self.assertRaises(complete_task.CompletionError):
                complete_task.complete_task(root, "example", check_only=True)

            source.write_text(VALID_TASK, encoding="utf-8")
            (root / "tasks" / "approvals" / "deploy.md").write_text(
                "状态：等待用户决定\n\n关联任务：example\n",
                encoding="utf-8",
            )
            with self.assertRaises(complete_task.CompletionError):
                complete_task.complete_task(root, "example", check_only=True)

    def test_blocks_unacknowledged_critical_message(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_root(temporary)
            source = root / "tasks" / "active" / "example.md"
            source.write_text(VALID_TASK, encoding="utf-8")
            message_id = agent_bus.send_message(
                root,
                "frontend",
                "main",
                "example",
                "blocker",
                "阻塞",
                "契约尚未确认。",
            )
            with self.assertRaises(complete_task.CompletionError):
                complete_task.complete_task(root, "example", check_only=True)

            agent_bus.acknowledge(root, "main", message_id)
            self.assertEqual(
                complete_task.complete_task(root, "example", check_only=True),
                source,
            )

    def test_direct_request_requires_recipient_and_main_ack(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.make_root(temporary)
            source = root / "tasks" / "active" / "example.md"
            source.write_text(VALID_TASK, encoding="utf-8")
            message_id = agent_bus.send_message(
                root,
                "frontend",
                "backend",
                "example",
                "request",
                "确认",
                "请确认契约。",
            )
            agent_bus.acknowledge(root, "backend", message_id)
            with self.assertRaises(complete_task.CompletionError):
                complete_task.complete_task(root, "example", check_only=True)

            agent_bus.acknowledge(root, "main", message_id)
            self.assertEqual(
                complete_task.complete_task(root, "example", check_only=True),
                source,
            )


class WorkspaceValidationTests(unittest.TestCase):
    def test_current_workspace_is_valid(self) -> None:
        errors, _warnings, names = validate_workspace.validate_workspace(
            Path(__file__).resolve().parent.parent
        )
        self.assertEqual(errors, [])
        self.assertEqual(names, set(validate_workspace.EXPECTED_AGENTS))

    def test_detects_weakened_approval_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_dir = root / ".codex"
            config_dir.mkdir()
            source = (
                Path(__file__).resolve().parent.parent
                / ".codex"
                / "config.toml"
            ).read_text(encoding="utf-8")
            (config_dir / "config.toml").write_text(
                source.replace(
                    'approval_policy = "on-request"',
                    'approval_policy = "never"',
                ),
                encoding="utf-8",
            )
            errors: list[str] = []
            validate_workspace.validate_config(root, errors)
            self.assertTrue(
                any("approval_policy" in error for error in errors), errors
            )

    def test_detects_tampered_message(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            message_id = agent_bus.send_message(
                root,
                "frontend",
                "main",
                "example",
                "result",
                "结果",
                "原始内容",
            )
            path = (
                root
                / "coordination"
                / "messages"
                / "main"
                / f"{message_id}.json"
            )
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["body"] = "被修改"
            path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            errors: list[str] = []
            warnings: list[str] = []
            validate_workspace.validate_coordination(root, errors, warnings)
            self.assertTrue(
                any("消息内容校验失败" in error for error in errors), errors
            )


if __name__ == "__main__":
    unittest.main()
