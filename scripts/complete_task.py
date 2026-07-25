from __future__ import annotations

import argparse
import json
import os
import re
import sys
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
TASK_ID_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")


class CompletionError(ValueError):
    pass


def validate_task_id(value: str) -> str:
    if not TASK_ID_PATTERN.fullmatch(value):
        raise CompletionError(
            "任务 ID 只能包含小写字母、数字和单个连字符"
        )
    return value


def pending_approvals(root: Path, task_id: str) -> list[Path]:
    approval_dir = root / "tasks" / "approvals"
    if not approval_dir.is_dir():
        return []
    pending: list[Path] = []
    marker = f"关联任务：{task_id}"
    for path in approval_dir.glob("*.md"):
        text = path.read_text(encoding="utf-8")
        if marker in text and "状态：等待用户决定" in text:
            pending.append(path)
    return pending


def unacknowledged_critical_messages(root: Path, task_id: str) -> list[str]:
    messages_root = root / "coordination" / "messages"
    ack_root = root / "coordination" / "acks"
    if not messages_root.is_dir():
        return []
    pending: list[str] = []
    for inbox_dir in messages_root.iterdir():
        if not inbox_dir.is_dir():
            continue
        recipient = inbox_dir.name
        if recipient == "security_reviewer":
            continue
        for path in inbox_dir.glob("*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pending.append(f"{path.stem}@{recipient}:损坏")
                continue
            if payload.get("task_id") != task_id:
                continue
            if payload.get("type") not in {"request", "decision", "blocker"}:
                continue
            ack = ack_root / f"{payload.get('id')}--{recipient}.json"
            if not ack.is_file():
                pending.append(f"{payload.get('id')}@{recipient}")
    return pending


def has_validation_record(content: str) -> bool:
    marker = "## 验证记录"
    if marker not in content:
        return False
    section = content.split(marker, 1)[1].split("\n## ", 1)[0]
    for line in section.splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) != 4:
            continue
        if cells[0] == "检查" or all(set(cell) <= {"-", ":"} for cell in cells):
            continue
        if cells[0] and cells[1] and cells[2]:
            return True
    return False


def validate_task_content(root: Path, task_id: str, content: str) -> list[str]:
    errors: list[str] = []
    if "状态：VERIFY" not in content:
        errors.append("任务状态必须为 VERIFY")
    if re.search(r"^\s*-\s*\[\s\]\s+", content, flags=re.MULTILINE):
        errors.append("仍有未勾选的验收项")
    for label in ("完成内容", "剩余风险"):
        if not re.search(
            rf"^\s*-\s*{re.escape(label)}：\s*\S+", content, flags=re.MULTILINE
        ):
            errors.append(f"最终结果中的“{label}”不能为空")
    if not has_validation_record(content):
        errors.append("验证记录表为空")
    approvals = pending_approvals(root, task_id)
    if approvals:
        names = ", ".join(path.name for path in approvals)
        errors.append(f"仍有等待用户决定的审批卡：{names}")
    messages = unacknowledged_critical_messages(root, task_id)
    if messages:
        errors.append(f"仍有未确认的关键消息：{', '.join(messages)}")
    return errors


def check_task(root: Path, task_id: str) -> tuple[Path, str]:
    task_id = validate_task_id(task_id)
    source = root / "tasks" / "active" / f"{task_id}.md"
    if not source.is_file():
        raise CompletionError(f"活动任务卡不存在：{source}")
    content = source.read_text(encoding="utf-8")
    errors = validate_task_content(root, task_id, content)
    if errors:
        raise CompletionError("；".join(errors))
    return source, content


def complete_task(root: Path, task_id: str, check_only: bool = False) -> Path:
    source, content = check_task(root, task_id)
    destination = root / "tasks" / "done" / source.name
    if destination.exists():
        raise CompletionError(f"完成目录已有同名任务卡：{destination}")
    if check_only:
        return source

    destination.parent.mkdir(parents=True, exist_ok=True)
    completed_content = content.replace("状态：VERIFY", "状态：DONE", 1)
    temporary = destination.with_name(
        f".{destination.name}.{uuid.uuid4().hex}.tmp"
    )
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(completed_content)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, destination)
        except FileExistsError as exc:
            raise CompletionError(
                f"完成目录已有同名任务卡：{destination}"
            ) from exc
        source.unlink()
    finally:
        if temporary.exists():
            temporary.unlink()
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(description="验证并完成任务卡")
    parser.add_argument("task_id")
    parser.add_argument("--check", action="store_true", help="只检查，不移动")
    args = parser.parse_args()
    try:
        path = complete_task(ROOT, args.task_id, args.check)
    except (CompletionError, OSError) as exc:
        print(f"完成门失败：{exc}", file=sys.stderr)
        return 2
    if args.check:
        print(f"完成门检查通过：{path}")
    else:
        print(f"任务已完成：{path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
