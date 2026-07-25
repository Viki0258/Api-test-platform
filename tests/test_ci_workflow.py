from __future__ import annotations

import re
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_PATH = REPOSITORY_ROOT / ".github" / "workflows" / "ci.yml"


def workflow_source() -> str:
    assert WORKFLOW_PATH.is_file(), "GitHub Actions workflow is missing"
    return WORKFLOW_PATH.read_text(encoding="utf-8")


def indented_block(source: str, key: str, *, indent: int = 0) -> str:
    prefix = " " * indent
    match = re.search(
        rf"""(?m)^{re.escape(prefix)}["']?{re.escape(key)}["']?:"""
        rf"\s*(?:#.*)?$\n"
        rf"(?P<body>(?:(?:^{' ' * (indent + 1)}.*$|^\s*$)\n?)*)",
        source,
    )
    assert match is not None, f"missing YAML block: {key}"
    return match.group("body")


def normalized(source: str) -> str:
    return " ".join(source.split())


def test_ci_triggers_main_push_main_pull_request_and_manual_runs() -> None:
    source = workflow_source()
    trigger_block = indented_block(source, "on")

    push_block = indented_block(trigger_block, "push", indent=2)
    pull_request_block = indented_block(
        trigger_block,
        "pull_request",
        indent=2,
    )
    branch_rule = (
        r"(?m)^\s{4}branches:\s*(?:"
        r"\[main\]\s*$|"
        r"\n\s{6}-\s*main\s*$"
        r")"
    )
    assert re.search(branch_rule, push_block)
    assert re.search(branch_rule, pull_request_block)
    assert re.search(r"(?m)^\s{2}workflow_dispatch:\s*(?:\{\})?\s*$", trigger_block)


def test_ci_uses_read_only_repository_permissions() -> None:
    source = workflow_source()
    permissions = indented_block(source, "permissions")
    permission_entries = re.findall(
        r"(?m)^\s{2}([A-Za-z_-]+):\s*([A-Za-z_-]+)\s*$",
        permissions,
    )

    assert permission_entries == [("contents", "read")]
    assert "secrets." not in source


def test_ci_checkout_does_not_persist_credentials() -> None:
    source = workflow_source()

    checkout = re.search(
        r"(?ms)^\s*-\s+name:\s*[^\n]*(?:[Cc]heck\s*out|[Cc]heckout)"
        r"[^\n]*\n"
        r"(?P<step>(?:(?!^\s*-\s+name:).)*)",
        source,
    )
    assert checkout is not None, "checkout step is missing"
    step = checkout.group("step")
    assert re.search(
        r"(?m)^\s+uses:\s*actions/checkout@[0-9a-f]{40}"
        r"(?:\s+#.*)?$",
        step,
    )
    assert re.search(
        r"(?m)^\s+persist-credentials:\s*false\s*$",
        step,
    )
    action_references = re.findall(
        r"(?m)^\s+uses:\s*([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)@"
        r"([^\s#]+)",
        source,
    )
    assert action_references
    assert all(re.fullmatch(r"[0-9a-f]{40}", revision) for _, revision in action_references)


def test_ci_uses_linux_python_311_and_installs_the_dev_group() -> None:
    source = workflow_source()

    assert re.search(r"(?m)^\s+runs-on:\s*ubuntu-latest\s*$", source)
    assert re.search(
        r"(?m)^\s+python-version:\s*[\"']?3\.11[\"']?\s*$",
        source,
    )
    assert re.search(
        r"""(?m)^\s*run:\s*python\s+-m\s+pip\s+install"""
        r"""\s+(?:-e\s+)?["']?\.\[dev\]["']?\s*$""",
        source,
    )


def test_ci_runs_all_four_frozen_quality_gates() -> None:
    source = workflow_source()
    compact = normalized(source)

    assert "python -m compileall -q src tests scripts" in compact
    assert "node --check frontend/app.js" in compact
    assert "python scripts/validate_workspace.py" in compact

    pytest_command = re.search(
        r"python\s+-m\s+pytest\b(?P<args>.*?)(?=(?:\s+node\s+--check|\s+python\s+scripts/validate_workspace\.py|$))",
        compact,
    )
    assert pytest_command is not None
    arguments = pytest_command.group("args")
    for required in (
        "--cov=app",
        "--cov-branch",
        "--cov-report=term-missing",
    ):
        assert required in arguments

    threshold = re.search(r"--cov-fail-under(?:=|\s+)(\d+)", arguments)
    assert threshold is not None
    assert int(threshold.group(1)) >= 90


def test_ci_has_bounded_runtime_and_cancels_stale_branch_runs() -> None:
    source = workflow_source()

    assert re.search(r"(?m)^\s+timeout-minutes:\s*10\s*$", source)
    concurrency = indented_block(source, "concurrency")
    assert re.search(
        r"""(?m)^\s{2}group:\s*"""
        r"""["']?ci-\$\{\{\s*github\.workflow\s*\}\}-"""
        r"""\$\{\{\s*github\.ref\s*\}\}["']?\s*$""",
        concurrency,
    )
    assert re.search(
        r"(?m)^\s{2}cancel-in-progress:\s*true\s*$",
        concurrency,
    )
