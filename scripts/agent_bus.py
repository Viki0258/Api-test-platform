from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
SCHEMA_VERSION = 1
MAX_BODY_BYTES = 32 * 1024
AGENTS = {
    "main",
    "frontend",
    "backend",
    "tester",
    "security_reviewer",
    "docs_writer",
}
MESSAGE_TYPES = {"info", "request", "decision", "blocker", "result"}
TASK_ID_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
MESSAGE_ID_PATTERN = re.compile(r"[0-9]{8}T[0-9]{12}Z-[0-9a-f]{12}\Z")
HIGH_CONFIDENCE_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
)


class BusError(ValueError):
    pass


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def timestamp(value: datetime | None = None) -> str:
    current = value or utc_now()
    return current.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def make_message_id(value: datetime | None = None) -> str:
    current = value or utc_now()
    return f"{current.strftime('%Y%m%dT%H%M%S%fZ')}-{uuid.uuid4().hex[:12]}"


def validate_agent(value: str) -> str:
    if value not in AGENTS:
        raise BusError(f"未知 Agent：{value}")
    return value


def validate_task_id(value: str) -> str:
    if not TASK_ID_PATTERN.fullmatch(value):
        raise BusError(
            "任务 ID 只能包含小写字母、数字和单个连字符，例如 user-profile"
        )
    return value


def validate_message_id(value: str) -> str:
    if not MESSAGE_ID_PATTERN.fullmatch(value):
        raise BusError("消息 ID 格式无效")
    return value


def validate_text(label: str, value: str, maximum_bytes: int) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise BusError(f"{label}不能为空")
    if len(cleaned.encode("utf-8")) > maximum_bytes:
        raise BusError(f"{label}超过 {maximum_bytes} 字节限制")
    return cleaned


def reject_secret_like_text(value: str) -> None:
    for pattern in HIGH_CONFIDENCE_SECRET_PATTERNS:
        if pattern.search(value):
            raise BusError("消息疑似包含凭据或私钥，拒绝写入持久消息箱")


def content_hash(payload: dict[str, Any]) -> str:
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def ensure_layout(root: Path) -> None:
    for agent in AGENTS:
        (root / "coordination" / "messages" / agent).mkdir(
            parents=True, exist_ok=True
        )
    (root / "coordination" / "acks").mkdir(parents=True, exist_ok=True)


def atomic_json_write(path: Path, payload: dict[str, Any]) -> None:
    if path.exists():
        raise BusError(f"拒绝覆盖已有审计记录：{path.name}")
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    data = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError as exc:
            raise BusError(f"拒绝覆盖已有审计记录：{path.name}") from exc
    finally:
        if temporary.exists():
            temporary.unlink()


def send_message(
    root: Path,
    sender: str,
    recipient: str,
    task_id: str,
    message_type: str,
    subject: str,
    body: str,
    reply_to: str | None = None,
) -> str:
    sender = validate_agent(sender)
    recipient = validate_agent(recipient)
    task_id = validate_task_id(task_id)
    if message_type not in MESSAGE_TYPES:
        raise BusError(f"未知消息类型：{message_type}")
    subject = validate_text("主题", subject, 512)
    body = validate_text("正文", body, MAX_BODY_BYTES)
    reject_secret_like_text(f"{subject}\n{body}")
    if reply_to is not None:
        reply_to = validate_message_id(reply_to)

    ensure_layout(root)
    message_id = make_message_id()
    recipients = {recipient}
    if sender != "main" and recipient != "main":
        recipients.add("main")

    common = {
        "schema_version": SCHEMA_VERSION,
        "id": message_id,
        "created_at": timestamp(),
        "from": sender,
        "to": recipient,
        "task_id": task_id,
        "type": message_type,
        "subject": subject,
        "body": body,
        "reply_to": reply_to,
    }
    for delivered_to in sorted(
        recipients, key=lambda value: (value != "main", value)
    ):
        payload = {
            **common,
            "delivered_to": delivered_to,
            "copied_to_main": delivered_to == "main" and recipient != "main",
        }
        payload["content_sha256"] = content_hash(payload)
        path = (
            root
            / "coordination"
            / "messages"
            / delivered_to
            / f"{message_id}.json"
        )
        atomic_json_write(path, payload)
    return message_id


def ack_path(root: Path, agent: str, message_id: str) -> Path:
    return root / "coordination" / "acks" / f"{message_id}--{agent}.json"


def acknowledge(root: Path, agent: str, message_id: str) -> Path:
    agent = validate_agent(agent)
    message_id = validate_message_id(message_id)
    message_path = (
        root / "coordination" / "messages" / agent / f"{message_id}.json"
    )
    if not message_path.is_file():
        raise BusError("收件箱中不存在该消息")
    destination = ack_path(root, agent, message_id)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "message_id": message_id,
        "agent": agent,
        "acknowledged_at": timestamp(),
    }
    payload["content_sha256"] = content_hash(payload)
    atomic_json_write(destination, payload)
    return destination


def inbox(
    root: Path,
    agent: str,
    task_id: str | None = None,
    unread_only: bool = False,
) -> list[dict[str, Any]]:
    agent = validate_agent(agent)
    if task_id is not None:
        task_id = validate_task_id(task_id)
    ensure_layout(root)
    results: list[dict[str, Any]] = []
    directory = root / "coordination" / "messages" / agent
    for path in sorted(directory.glob("*.json")):
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        required = {
            "schema_version",
            "id",
            "from",
            "to",
            "task_id",
            "type",
            "delivered_to",
            "content_sha256",
        }
        if not required.issubset(payload):
            raise BusError(f"消息结构不完整：{path.name}")
        if payload["id"] != path.stem or payload["delivered_to"] != agent:
            raise BusError(f"消息投递信息不一致：{path.name}")
        if payload["content_sha256"] != content_hash(
            {key: value for key, value in payload.items() if key != "content_sha256"}
        ):
            raise BusError(f"消息内容校验失败：{path.name}")
        if task_id is not None and payload.get("task_id") != task_id:
            continue
        acknowledged = ack_path(root, agent, payload["id"]).is_file()
        if unread_only and acknowledged:
            continue
        results.append({**payload, "acknowledged": acknowledged})
    return results


def task_status(root: Path, task_id: str) -> dict[str, Any]:
    task_id = validate_task_id(task_id)
    per_agent: dict[str, dict[str, int]] = {}
    for agent in sorted(AGENTS):
        messages = inbox(root, agent, task_id)
        per_agent[agent] = {
            "total": len(messages),
            "unacknowledged": sum(
                1 for message in messages if not message["acknowledged"]
            ),
        }
    return {"task_id": task_id, "agents": per_agent}


def read_body_argument(root: Path, body: str | None, body_file: str | None) -> str:
    if body is not None:
        return body
    if body_file is None:
        raise BusError("必须提供 --body 或 --body-file")
    candidate = Path(body_file)
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved_root = root.resolve()
    resolved_candidate = candidate.resolve()
    try:
        resolved_candidate.relative_to(resolved_root)
    except ValueError as exc:
        raise BusError("--body-file 必须位于项目目录内") from exc
    if not resolved_candidate.is_file():
        raise BusError("--body-file 不存在或不是文件")
    if resolved_candidate.stat().st_size > MAX_BODY_BYTES:
        raise BusError(f"正文文件超过 {MAX_BODY_BYTES} 字节限制")
    return resolved_candidate.read_text(encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="项目级 Agent 持久消息箱")
    subparsers = parser.add_subparsers(dest="command", required=True)

    send = subparsers.add_parser("send", help="发送不可变消息")
    send.add_argument("--from-agent", required=True, choices=sorted(AGENTS))
    send.add_argument("--to-agent", required=True, choices=sorted(AGENTS))
    send.add_argument("--task", required=True)
    send.add_argument("--type", required=True, choices=sorted(MESSAGE_TYPES))
    send.add_argument("--subject", required=True)
    body_group = send.add_mutually_exclusive_group(required=True)
    body_group.add_argument("--body")
    body_group.add_argument("--body-file")
    send.add_argument("--reply-to")

    inbox_parser = subparsers.add_parser("inbox", help="查看收件箱")
    inbox_parser.add_argument("--agent", required=True, choices=sorted(AGENTS))
    inbox_parser.add_argument("--task")
    inbox_parser.add_argument("--unread", action="store_true")
    inbox_parser.add_argument("--json", action="store_true")

    ack = subparsers.add_parser("ack", help="确认一条消息")
    ack.add_argument("--agent", required=True, choices=sorted(AGENTS))
    ack.add_argument("--message", required=True)

    status = subparsers.add_parser("status", help="查看任务消息统计")
    status.add_argument("--task", required=True)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        if args.command == "send":
            body = read_body_argument(ROOT, args.body, args.body_file)
            message_id = send_message(
                ROOT,
                args.from_agent,
                args.to_agent,
                args.task,
                args.type,
                args.subject,
                body,
                args.reply_to,
            )
            print(message_id)
        elif args.command == "inbox":
            messages = inbox(ROOT, args.agent, args.task, args.unread)
            if args.json:
                print(json.dumps(messages, ensure_ascii=False, indent=2))
            else:
                for message in messages:
                    marker = "ACK" if message["acknowledged"] else "NEW"
                    print(
                        f"[{marker}] {message['id']} {message['from']} -> "
                        f"{message['to']} [{message['type']}] "
                        f"{message['subject']}"
                    )
                print(f"共 {len(messages)} 条")
        elif args.command == "ack":
            print(acknowledge(ROOT, args.agent, args.message))
        elif args.command == "status":
            print(
                json.dumps(
                    task_status(ROOT, args.task), ensure_ascii=False, indent=2
                )
            )
    except (BusError, json.JSONDecodeError, OSError, KeyError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
