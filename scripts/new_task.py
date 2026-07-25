from __future__ import annotations

import argparse
import re
import sys
from datetime import date
from pathlib import Path


def valid_slug(value: str) -> str:
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", value):
        raise argparse.ArgumentTypeError(
            "任务 ID 只能包含小写字母、数字和单个连字符，例如 user-profile"
        )
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description="从模板创建活动任务卡")
    parser.add_argument("task_id", type=valid_slug)
    parser.add_argument("title")
    args = parser.parse_args()

    root = Path(__file__).resolve().parent.parent
    template_path = root / "tasks" / "TASK_TEMPLATE.md"
    destination = root / "tasks" / "active" / f"{args.task_id}.md"

    if destination.exists():
        print(f"拒绝覆盖已有任务卡：{destination}", file=sys.stderr)
        return 2

    content = template_path.read_text(encoding="utf-8")
    content = content.replace("{{TASK_ID}}", args.task_id)
    content = content.replace("{{TITLE}}", args.title)
    content = content.replace("状态：INTAKE", f"状态：INTAKE\n\n创建日期：{date.today().isoformat()}")
    destination.write_text(content, encoding="utf-8", newline="\n")
    print(f"已创建：{destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

