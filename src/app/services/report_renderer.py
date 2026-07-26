from __future__ import annotations

from html import escape
import json
from typing import Any

from app.schemas import AssertionResult, CaseResult, TestRunResult


MAX_DYNAMIC_CHARACTERS = 2000
TRUNCATION_MARKER = "…（已截断）"
REPORT_META_CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; "
    "form-action 'none'"
)
REPORT_CONTENT_SECURITY_POLICY = (
    f"{REPORT_META_CSP}; frame-ancestors 'none'"
)


def _truncate(value: str) -> str:
    if len(value) <= MAX_DYNAMIC_CHARACTERS:
        return value
    available = MAX_DYNAMIC_CHARACTERS - len(TRUNCATION_MARKER)
    return f"{value[:available]}{TRUNCATION_MARKER}"


def _display_text(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        try:
            return json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        except (TypeError, ValueError):
            return "无法显示该值"
    return str(value)


def _safe(value: Any) -> str:
    return escape(_truncate(_display_text(value)), quote=True)


def _status_label(value: str) -> str:
    return {
        "passed": "通过",
        "failed": "失败",
        "skipped": "跳过",
    }.get(value, "未知")


def _render_assertion(assertion: AssertionResult) -> str:
    result_label = "通过" if assertion.passed else "失败"
    return f"""
      <li class="assertion">
        <div class="assertion-heading">
          <strong>{_safe(assertion.type.value)}</strong>
          <span>{result_label}</span>
        </div>
        <dl class="assertion-values">
          <div><dt>期望</dt><dd>{_safe(assertion.expected)}</dd></div>
          <div><dt>实际</dt><dd>{_safe(assertion.actual)}</dd></div>
        </dl>
        <p>{_safe(assertion.message)}</p>
      </li>
    """


def _render_optional_detail(label: str, value: Any) -> str:
    if value is None or value == "":
        return ""
    return f"<p><strong>{label}：</strong>{_safe(value)}</p>"


def _render_case(case: CaseResult, index: int) -> str:
    status = case.status.value
    status_class = {
        "passed": "passed",
        "failed": "failed",
        "skipped": "skipped",
    }.get(status, "unknown")
    assertions = "".join(
        _render_assertion(assertion) for assertion in case.assertions
    )
    if not assertions:
        assertions = '<li class="empty">没有断言结果。</li>'
    extracted = "".join(
        f"<li>{_safe(name)}</li>" for name in case.extracted_variables
    )
    extracted_section = ""
    if extracted:
        extracted_section = f"""
        <section class="case-section">
          <h4>提取变量名</h4>
          <ul class="chips">{extracted}</ul>
        </section>
        """

    status_code = "—" if case.status_code is None else case.status_code
    return f"""
    <article class="case-card">
      <header class="case-header">
        <div>
          <p class="case-index">用例 {index}</p>
          <h3>{_safe(case.name)}</h3>
          <p class="case-id">{_safe(case.id)}</p>
        </div>
        <span class="status {status_class}">{_status_label(status)}</span>
      </header>
      <dl class="case-summary">
        <div><dt>HTTP 状态码</dt><dd>{_safe(status_code)}</dd></div>
        <div><dt>响应耗时</dt><dd>{_safe(case.response_time_ms)} ms</dd></div>
      </dl>
      {_render_optional_detail("错误代码", case.error_code)}
      {_render_optional_detail("错误说明", case.error)}
      {_render_optional_detail("跳过原因", case.skip_reason)}
      <section class="case-section">
        <h4>断言</h4>
        <ul class="assertions">{assertions}</ul>
      </section>
      {extracted_section}
    </article>
    """


def render_test_run_report(result: TestRunResult) -> str:
    """Render one already-redacted stored result as standalone static HTML."""
    cases = "".join(
        _render_case(case, index)
        for index, case in enumerate(result.cases, start=1)
    )
    overall_label = "通过" if result.passed else "未通过"
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta
    http-equiv="Content-Security-Policy"
    content="{escape(REPORT_META_CSP, quote=True)}"
  >
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>接口测试报告</title>
  <style>
    :root {{
      color-scheme: light;
      font-family: Arial, "Microsoft YaHei", sans-serif;
      color: #172033;
      background: #f4f6f9;
    }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; padding: 32px 16px; }}
    main {{ width: min(100%, 980px); margin: 0 auto; }}
    h1, h2, h3, h4, p {{ overflow-wrap: anywhere; }}
    .report-header, .case-card {{
      background: #fff;
      border: 1px solid #dce2ea;
      border-radius: 14px;
      box-shadow: 0 8px 24px rgba(20, 35, 60, 0.06);
    }}
    .report-header {{ padding: 28px; }}
    .eyebrow, .case-index {{
      margin: 0 0 6px;
      color: #667085;
      font-size: 12px;
      font-weight: 700;
      letter-spacing: 0.08em;
      text-transform: uppercase;
    }}
    h1 {{ margin: 0; font-size: 28px; }}
    .run-id {{ color: #596579; font-family: monospace; }}
    .summary {{
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 10px;
      margin-top: 22px;
    }}
    dl {{ margin: 0; }}
    .summary div, .case-summary div, .assertion-values div {{
      padding: 12px;
      background: #f7f8fa;
      border-radius: 9px;
    }}
    dt {{ color: #667085; font-size: 12px; }}
    dd {{ margin: 4px 0 0; font-weight: 700; overflow-wrap: anywhere; }}
    .cases {{ display: grid; gap: 16px; margin-top: 22px; }}
    .case-card {{ padding: 22px; }}
    .case-header {{
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 16px;
    }}
    .case-header h3 {{ margin: 0; font-size: 18px; }}
    .case-id {{ margin: 5px 0 0; color: #667085; font-family: monospace; }}
    .status {{
      flex: none;
      padding: 6px 10px;
      border-radius: 999px;
      font-size: 12px;
      font-weight: 800;
    }}
    .passed {{ color: #087443; background: #e8f7ef; }}
    .failed {{ color: #a22828; background: #fdecec; }}
    .skipped {{ color: #855d08; background: #fff3d6; }}
    .unknown {{ color: #4b5565; background: #edf0f4; }}
    .case-summary, .assertion-values {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
      margin-top: 16px;
    }}
    .case-section {{ margin-top: 18px; }}
    .case-section h4 {{ margin: 0 0 8px; }}
    .assertions, .chips {{ margin: 0; padding: 0; list-style: none; }}
    .assertions {{ display: grid; gap: 9px; }}
    .assertion {{ padding: 13px; background: #f7f8fa; border-radius: 9px; }}
    .assertion-heading {{
      display: flex;
      justify-content: space-between;
      gap: 10px;
    }}
    .assertion p {{ margin: 9px 0 0; color: #596579; }}
    .chips {{ display: flex; flex-wrap: wrap; gap: 6px; }}
    .chips li {{
      padding: 5px 8px;
      color: #344b79;
      background: #edf2ff;
      border-radius: 6px;
      font-family: monospace;
      font-size: 12px;
    }}
    .empty {{ color: #667085; }}
    .report-note {{ margin: 18px 2px 0; color: #667085; font-size: 12px; }}
    @media (max-width: 640px) {{
      body {{ padding: 16px 10px; }}
      .report-header, .case-card {{ padding: 18px; }}
      .summary, .case-summary, .assertion-values {{ grid-template-columns: 1fr; }}
      .case-header {{ display: grid; }}
    }}
  </style>
</head>
<body>
  <main>
    <header class="report-header">
      <p class="eyebrow">API Test Platform</p>
      <h1>接口自动化测试报告</h1>
      <p class="run-id">运行 ID：{_safe(result.run_id)}</p>
      <p>创建时间：{_safe(result.created_at.isoformat())}</p>
      <p>整体结果：<strong>{overall_label}</strong></p>
      <p>总耗时：{_safe(result.duration_ms)} ms</p>
      <dl class="summary">
        <div><dt>总数</dt><dd>{_safe(result.total)}</dd></div>
        <div><dt>通过</dt><dd>{_safe(result.passed_count)}</dd></div>
        <div><dt>失败</dt><dd>{_safe(result.failed_count)}</dd></div>
        <div><dt>跳过</dt><dd>{_safe(result.skipped_count)}</dd></div>
      </dl>
    </header>
    <section class="cases" aria-label="测试用例结果">
      <h2>用例详情</h2>
      {cases}
    </section>
    <p class="report-note">
      本报告仅包含平台已保存的脱敏运行结果，不包含请求配置或变量值。
    </p>
  </main>
</body>
</html>
"""
