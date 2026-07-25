"use strict";

const elements = {
  baseUrl: document.querySelector("#base-url"),
  restoreDemo: document.querySelector("#restore-demo"),
  runTests: document.querySelector("#run-tests"),
  runButtonLabel: document.querySelector("#run-tests .button-label"),
  runStatus: document.querySelector("#run-status"),
  advancedPanel: document.querySelector("#advanced-panel"),
  jsonEditor: document.querySelector("#json-editor"),
  caseCount: document.querySelector("#case-count"),
  caseOverview: document.querySelector("#case-overview"),
  emptyResult: document.querySelector("#empty-result"),
  resultContent: document.querySelector("#result-content"),
  overallStatus: document.querySelector("#overall-status"),
  summaryTotal: document.querySelector("#summary-total"),
  summaryPassed: document.querySelector("#summary-passed"),
  summaryFailed: document.querySelector("#summary-failed"),
  summarySkipped: document.querySelector("#summary-skipped"),
  duration: document.querySelector("#duration"),
  caseResults: document.querySelector("#case-results"),
  refreshHistory: document.querySelector("#refresh-history"),
  historyStatus: document.querySelector("#history-status"),
  historyList: document.querySelector("#history-list"),
};

const assertionNames = {
  status_code: "状态码",
  json_equals: "JSON 字段等值",
  response_time_ms: "响应时间",
};

const statusNames = {
  passed: "通过",
  failed: "失败",
  skipped: "跳过",
};

function createDemoPayload() {
  return {
    base_url: window.location.origin,
    variables: {
      expected_user_id: 7,
      expected_user_name: "demo-user",
      run_label: "local-showcase",
    },
    secret_variables: [],
    cases: [
      {
        id: "fetch_user",
        name: "查询用户并提取链路变量",
        method: "GET",
        path: "/api/v1/demo/users/{{expected_user_id}}",
        headers: {
          "X-Demo-Run": "{{run_label}}",
        },
        query: {},
        assertions: [
          {
            type: "status_code",
            expected: 200,
          },
          {
            type: "json_equals",
            path: "data.id",
            expected: "{{expected_user_id}}",
          },
          {
            type: "json_equals",
            path: "data.name",
            expected: "{{expected_user_name}}",
          },
        ],
        extract: [
          {
            name: "user_id",
            path: "data.id",
            secret: false,
          },
          {
            name: "user_name",
            path: "data.name",
            secret: false,
          },
        ],
      },
      {
        id: "verify_extracted_user",
        name: "复用提取变量验证依赖链",
        method: "GET",
        path: "/api/v1/demo/users/{{user_id}}",
        headers: {
          "X-Demo-User": "{{user_name}}",
        },
        query: {
          source_user_id: "{{user_id}}",
        },
        depends_on: ["fetch_user"],
        assertions: [
          {
            type: "status_code",
            expected: 200,
          },
          {
            type: "json_equals",
            path: "data.id",
            expected: "{{user_id}}",
          },
          {
            type: "json_equals",
            path: "data.name",
            expected: "{{user_name}}",
          },
          {
            type: "response_time_ms",
            expected: 1000,
          },
        ],
      },
    ],
  };
}

let payload = createDemoPayload();

function makeElement(tag, className, text) {
  const node = document.createElement(tag);
  if (className) {
    node.className = className;
  }
  if (text !== undefined && text !== null) {
    node.textContent = String(text);
  }
  return node;
}

function replaceChildren(parent, children) {
  parent.replaceChildren(...children);
}

function syncEditor() {
  elements.jsonEditor.value = JSON.stringify(payload, null, 2);
}

function renderCaseOverview() {
  const cases = Array.isArray(payload.cases) ? payload.cases : [];
  elements.caseCount.textContent = `共 ${cases.length} 条，按顺序执行`;

  const items = cases.map((testCase, index) => {
    const item = makeElement("li", "overview-item");
    const number = makeElement("span", "case-number", index + 1);
    const content = makeElement("div");
    const name = makeElement(
      "strong",
      "",
      testCase && testCase.name ? testCase.name : `未命名用例 ${index + 1}`,
    );
    const meta = makeElement("div", "overview-meta");
    const method = makeElement(
      "span",
      "",
      testCase && testCase.method ? testCase.method : "—",
    );
    const path = makeElement(
      "span",
      "",
      testCase && testCase.path ? testCase.path : "未设置路径",
    );
    meta.append(method, path);
    content.append(name, meta);
    item.append(number, content);
    return item;
  });

  if (items.length === 0) {
    items.push(makeElement("li", "overview-item", "当前 JSON 中没有测试用例。"));
  }
  replaceChildren(elements.caseOverview, items);
}

function restoreDemo() {
  payload = createDemoPayload();
  elements.baseUrl.value = payload.base_url;
  syncEditor();
  renderCaseOverview();
  resetResults();
  setRunStatus("已恢复安全的两步本地演示。", "idle");
}

function resetResults() {
  elements.emptyResult.hidden = false;
  elements.resultContent.hidden = true;
  elements.overallStatus.className = "result-badge result-idle";
  elements.overallStatus.textContent = "等待运行";
  replaceChildren(elements.caseResults, []);
}

function setRunStatus(message, type) {
  elements.runStatus.className = "status-message";
  if (type === "error") {
    elements.runStatus.classList.add("status-error");
  } else if (type === "success") {
    elements.runStatus.classList.add("status-success");
  }
  elements.runStatus.textContent = message;
}

function setLoading(isLoading) {
  elements.runTests.disabled = isLoading;
  elements.restoreDemo.disabled = isLoading;
  elements.runTests.classList.toggle("is-loading", isLoading);
  elements.runButtonLabel.textContent = isLoading ? "正在运行…" : "运行测试";
  elements.runTests.setAttribute("aria-busy", String(isLoading));
}

function readPayload() {
  let nextPayload;
  try {
    nextPayload = JSON.parse(elements.jsonEditor.value);
  } catch (error) {
    throw new Error(`JSON 格式有误：${error.message}`);
  }

  if (!nextPayload || typeof nextPayload !== "object" || Array.isArray(nextPayload)) {
    throw new Error("请求 JSON 必须是一个对象。");
  }
  nextPayload.base_url = elements.baseUrl.value.trim();
  if (!nextPayload.base_url) {
    throw new Error("请填写被测服务地址。");
  }
  if (!Array.isArray(nextPayload.cases) || nextPayload.cases.length === 0) {
    throw new Error("至少需要一条测试用例。");
  }
  return nextPayload;
}

function describeApiError(data, status) {
  if (data && Array.isArray(data.detail)) {
    const first = data.detail[0];
    if (first && typeof first.msg === "string") {
      return `请求校验失败：${first.msg}`;
    }
  }
  if (data && typeof data.detail === "string") {
    return data.detail;
  }
  if (
    data &&
    data.detail &&
    typeof data.detail === "object" &&
    typeof data.detail.message === "string"
  ) {
    const code =
      typeof data.detail.code === "string" ? `${data.detail.code}：` : "";
    return `${code}${data.detail.message}`;
  }
  return `服务返回 HTTP ${status}，请检查请求配置。`;
}

function formatDate(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return "时间未知";
  }
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(date);
}

function valuePreview(value) {
  if (value === undefined) {
    return "未提供";
  }
  if (typeof value === "string") {
    return value;
  }
  try {
    return JSON.stringify(value);
  } catch (_error) {
    return String(value);
  }
}

function renderAssertion(assertion) {
  const passed = Boolean(assertion && assertion.passed);
  const item = makeElement(
    "li",
    `assertion-item ${passed ? "assertion-pass" : "assertion-fail"}`,
  );
  const icon = makeElement("span", "assertion-icon", passed ? "✓" : "×");
  icon.setAttribute("aria-label", passed ? "通过" : "失败");

  const content = makeElement("div");
  const typeName =
    assertion && assertionNames[assertion.type]
      ? assertionNames[assertion.type]
      : "断言";
  const expected = valuePreview(assertion ? assertion.expected : undefined);
  const actual = valuePreview(assertion ? assertion.actual : undefined);
  const title = makeElement(
    "strong",
    "",
    `${typeName}：期望 ${expected}，实际 ${actual}`,
  );
  const message = makeElement(
    "span",
    "assertion-message",
    assertion && assertion.message ? assertion.message : "无详细说明",
  );
  content.append(title, message);
  item.append(icon, content);
  return item;
}

function renderCaseResult(testCase, index) {
  const safeCase = testCase && typeof testCase === "object" ? testCase : {};
  const status = statusNames[safeCase.status] ? safeCase.status : "failed";
  const details = makeElement("details", "case-result");
  if (status !== "passed") {
    details.open = true;
  }

  const summary = makeElement("summary");
  const heading = makeElement("span");
  heading.append(
    makeElement("strong", "case-title", safeCase.name || `用例 ${index + 1}`),
    makeElement("span", "case-subtitle", safeCase.id || `case_${index + 1}`),
  );
  const meta = makeElement("span", "case-meta");
  const time = Number.isFinite(Number(safeCase.response_time_ms))
    ? `${Number(safeCase.response_time_ms).toFixed(1)} ms`
    : "无耗时";
  meta.append(
    makeElement("span", "response-time", time),
    makeElement("span", `case-status status-${status}`, statusNames[status]),
  );
  summary.append(heading, meta);

  const body = makeElement("div", "case-detail");
  const assertions = Array.isArray(safeCase.assertions)
    ? safeCase.assertions
    : [];
  body.append(makeElement("h4", "detail-heading", `断言（${assertions.length}）`));
  const assertionList = makeElement("ul", "assertion-list");
  if (assertions.length) {
    assertionList.append(...assertions.map(renderAssertion));
  } else {
    assertionList.append(makeElement("li", "assertion-item", "没有断言结果。"));
  }
  body.append(assertionList);

  const variables = Array.isArray(safeCase.extracted_variables)
    ? safeCase.extracted_variables
    : [];
  if (variables.length) {
    body.append(makeElement("h4", "detail-heading", "已提取变量"));
    const variableList = makeElement("ul", "variable-list");
    variables.forEach((name) => {
      variableList.append(makeElement("li", "", name));
    });
    body.append(variableList);
  }

  const safeError =
    safeCase.error || safeCase.skip_reason || safeCase.error_code || null;
  if (safeError) {
    const errorPrefix = safeCase.error_code ? `${safeCase.error_code}：` : "";
    body.append(makeElement("p", "case-error", `${errorPrefix}${safeError}`));
  }

  details.append(summary, body);
  return details;
}

function renderResult(result) {
  const cases = Array.isArray(result && result.cases) ? result.cases : [];
  elements.emptyResult.hidden = true;
  elements.resultContent.hidden = false;
  elements.summaryTotal.textContent = String(result.total ?? cases.length);
  elements.summaryPassed.textContent = String(result.passed_count ?? 0);
  elements.summaryFailed.textContent = String(result.failed_count ?? 0);
  elements.summarySkipped.textContent = String(result.skipped_count ?? 0);
  const duration = Number(result.duration_ms);
  const durationText = Number.isFinite(duration)
    ? `耗时 ${duration.toFixed(1)} ms`
    : "耗时未知";
  const createdText = result.created_at
    ? ` · ${formatDate(result.created_at)}`
    : "";
  const idText = result.run_id ? ` · ID ${String(result.run_id)}` : "";
  elements.duration.textContent = `${durationText}${createdText}${idText}`;

  const overallPassed = Boolean(result.passed);
  elements.overallStatus.className = `result-badge ${
    overallPassed ? "status-passed" : "status-failed"
  }`;
  elements.overallStatus.textContent = overallPassed ? "全部通过" : "存在未通过项";

  if (cases.length) {
    replaceChildren(
      elements.caseResults,
      cases.map((testCase, index) => renderCaseResult(testCase, index)),
    );
  } else {
    replaceChildren(elements.caseResults, [
      makeElement(
        "div",
        "empty-state",
        "服务返回了空结果，没有可展示的用例明细。",
      ),
    ]);
  }

  const message = overallPassed
    ? `运行完成：${result.passed_count ?? 0} 条用例通过。`
    : `运行完成：${result.failed_count ?? 0} 条失败，${
        result.skipped_count ?? 0
      } 条跳过。`;
  setRunStatus(message, overallPassed ? "success" : "error");
}

async function runTests() {
  let requestPayload;
  try {
    requestPayload = readPayload();
  } catch (error) {
    elements.advancedPanel.open = true;
    setRunStatus(error.message, "error");
    elements.jsonEditor.focus();
    return;
  }

  payload = requestPayload;
  syncEditor();
  renderCaseOverview();
  setLoading(true);
  setRunStatus("正在执行测试，请稍候…", "idle");

  try {
    const response = await fetch("/api/v1/runs", {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify(payload),
    });

    let data;
    try {
      data = await response.json();
    } catch (_error) {
      throw new Error(`服务返回 HTTP ${response.status}，但响应不是有效 JSON。`);
    }

    if (!response.ok) {
      throw new Error(describeApiError(data, response.status));
    }
    renderResult(data);
    void loadHistory();
  } catch (error) {
    resetResults();
    elements.overallStatus.className = "result-badge status-failed";
    elements.overallStatus.textContent = "请求失败";
    setRunStatus(
      error instanceof Error
        ? `无法完成运行：${error.message}`
        : "无法完成运行，请确认服务仍在启动。",
      "error",
    );
  } finally {
    setLoading(false);
  }
}

function setHistoryStatus(message, type) {
  elements.historyStatus.className = "history-status";
  if (type === "error") {
    elements.historyStatus.classList.add("status-error");
  }
  elements.historyStatus.textContent = message;
}

function setHistoryLoading(isLoading) {
  elements.refreshHistory.disabled = isLoading;
  elements.refreshHistory.textContent = isLoading ? "正在刷新…" : "刷新记录";
  elements.refreshHistory.setAttribute("aria-busy", String(isLoading));
}

function renderHistoryItem(item) {
  const safeItem = item && typeof item === "object" ? item : {};
  const listItem = makeElement("li", "history-item");
  const button = makeElement("button", "history-button");
  button.type = "button";
  button.setAttribute(
    "aria-label",
    `查看 ${formatDate(safeItem.created_at)} 的运行详情`,
  );

  const main = makeElement("span", "history-main");
  main.append(
    makeElement("span", "history-date", formatDate(safeItem.created_at)),
    makeElement("span", "history-id", safeItem.run_id || "运行 ID 未知"),
  );
  const counts = makeElement("span", "history-counts");
  counts.append(
    makeElement("span", "", `总数 ${safeItem.total ?? 0}`),
    makeElement("span", "", `通过 ${safeItem.passed_count ?? 0}`),
    makeElement("span", "", `失败 ${safeItem.failed_count ?? 0}`),
    makeElement("span", "", `跳过 ${safeItem.skipped_count ?? 0}`),
  );
  main.append(counts);

  const side = makeElement("span", "history-side");
  const passed = Boolean(safeItem.passed);
  side.append(
    makeElement(
      "span",
      `case-status ${passed ? "status-passed" : "status-failed"}`,
      passed ? "通过" : "未通过",
    ),
    makeElement(
      "span",
      "response-time",
      Number.isFinite(Number(safeItem.duration_ms))
        ? `${Number(safeItem.duration_ms).toFixed(1)} ms`
        : "无耗时",
    ),
  );
  button.append(main, side);
  button.addEventListener("click", () => {
    void loadHistoryDetail(safeItem.run_id, button);
  });
  listItem.append(button);
  return listItem;
}

function renderHistoryList(data) {
  const items = Array.isArray(data && data.items) ? data.items : [];
  if (items.length === 0) {
    replaceChildren(elements.historyList, [
      makeElement(
        "li",
        "history-empty",
        "还没有运行记录。完成一次测试后，记录会出现在这里。",
      ),
    ]);
    setHistoryStatus("历史记录为空。", "idle");
    return;
  }

  replaceChildren(elements.historyList, items.map(renderHistoryItem));
  const total = Number.isFinite(Number(data.total)) ? Number(data.total) : items.length;
  setHistoryStatus(`已显示最近 ${items.length} 条，共保存 ${total} 条。`, "idle");
}

async function loadHistory() {
  setHistoryLoading(true);
  setHistoryStatus("正在加载最近运行…", "idle");
  try {
    const response = await fetch("/api/v1/runs?limit=10", {
      method: "GET",
      credentials: "same-origin",
      headers: {
        Accept: "application/json",
      },
    });
    let data;
    try {
      data = await response.json();
    } catch (_error) {
      throw new Error(`服务返回 HTTP ${response.status}，但响应不是有效 JSON。`);
    }
    if (!response.ok) {
      throw new Error(describeApiError(data, response.status));
    }
    renderHistoryList(data);
  } catch (error) {
    replaceChildren(elements.historyList, []);
    setHistoryStatus(
      error instanceof Error
        ? `无法加载历史：${error.message}`
        : "无法加载历史，请稍后重试。",
      "error",
    );
  } finally {
    setHistoryLoading(false);
  }
}

async function loadHistoryDetail(runId, button) {
  if (typeof runId !== "string" || !runId) {
    setHistoryStatus("这条记录缺少有效运行 ID，无法打开。", "error");
    return;
  }
  button.disabled = true;
  button.setAttribute("aria-busy", "true");
  setHistoryStatus("正在加载运行详情…", "idle");
  try {
    const response = await fetch(`/api/v1/runs/${encodeURIComponent(runId)}`, {
      method: "GET",
      credentials: "same-origin",
      headers: {
        Accept: "application/json",
      },
    });
    let data;
    try {
      data = await response.json();
    } catch (_error) {
      throw new Error(`服务返回 HTTP ${response.status}，但响应不是有效 JSON。`);
    }
    if (!response.ok) {
      throw new Error(describeApiError(data, response.status));
    }
    renderResult(data);
    setRunStatus(`已打开 ${formatDate(data.created_at)} 的历史运行。`, "success");
    setHistoryStatus("历史详情已显示在“运行结果”区域。", "idle");
    document.querySelector("#results-title").scrollIntoView({
      behavior: "smooth",
      block: "start",
    });
  } catch (error) {
    setHistoryStatus(
      error instanceof Error
        ? `无法打开详情：${error.message}`
        : "无法打开详情，请稍后重试。",
      "error",
    );
  } finally {
    button.disabled = false;
    button.setAttribute("aria-busy", "false");
  }
}

elements.baseUrl.addEventListener("change", () => {
  payload.base_url = elements.baseUrl.value.trim();
  syncEditor();
});

elements.jsonEditor.addEventListener("change", () => {
  try {
    const parsed = JSON.parse(elements.jsonEditor.value);
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
      payload = parsed;
      if (typeof parsed.base_url === "string") {
        elements.baseUrl.value = parsed.base_url;
      }
      renderCaseOverview();
      setRunStatus("高级 JSON 已更新，尚未运行。", "idle");
    }
  } catch (_error) {
    setRunStatus("高级 JSON 暂时无法解析，请修正后再运行。", "error");
  }
});

elements.restoreDemo.addEventListener("click", restoreDemo);
elements.runTests.addEventListener("click", runTests);
elements.refreshHistory.addEventListener("click", loadHistory);

restoreDemo();
void loadHistory();
