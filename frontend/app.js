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
  openApiFile: document.querySelector("#openapi-file"),
  openApiEditor: document.querySelector("#openapi-editor"),
  openApiBaseUrl: document.querySelector("#openapi-base-url"),
  openApiMaxCases: document.querySelector("#openapi-max-cases"),
  loadOpenApiDemo: document.querySelector("#load-openapi-demo"),
  generateOpenApi: document.querySelector("#generate-openapi"),
  applyGeneratedRun: document.querySelector("#apply-generated-run"),
  openApiStatus: document.querySelector("#openapi-status"),
  openApiGeneratedCount: document.querySelector("#openapi-generated-count"),
  openApiSkippedCount: document.querySelector("#openapi-skipped-count"),
  openApiWarnings: document.querySelector("#openapi-warnings"),
  aiProviderBadge: document.querySelector("#ai-provider-badge"),
  aiProviderDetail: document.querySelector("#ai-provider-detail"),
  aiObjective: document.querySelector("#ai-objective"),
  aiMaxCases: document.querySelector("#ai-max-cases"),
  generateAi: document.querySelector("#generate-ai"),
  applyAiRun: document.querySelector("#apply-ai-run"),
  aiStatus: document.querySelector("#ai-status"),
  aiInsights: document.querySelector("#ai-insights"),
  reviewCount: document.querySelector("#review-count"),
  reviewStatus: document.querySelector("#review-status"),
  candidateList: document.querySelector("#candidate-list"),
  selectAllCandidates: document.querySelector("#select-all-candidates"),
  clearCandidateSelection: document.querySelector("#clear-candidate-selection"),
  replaceEditorCandidates: document.querySelector("#replace-editor-candidates"),
  appendEditorCandidates: document.querySelector("#append-editor-candidates"),
};

const MAX_OPENAPI_FILE_BYTES = 1048576;
const UUID_V4_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

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
let generatedRun = null;
let runIsLoading = false;
let openApiIsLoading = false;
let openApiRequestSequence = 0;
let openApiAbortController = null;
let generatedAiRun = null;
let aiIsLoading = false;
let aiRequestSequence = 0;
let aiAbortController = null;
let reviewCandidates = [];

function createOpenApiDemo() {
  return {
    openapi: "3.1.0",
    info: {
      title: "本地合成用户接口",
      version: "1.0.0",
    },
    servers: [
      {
        url: window.location.origin,
      },
    ],
    paths: {
      "/api/v1/demo/users/{user_id}": {
        get: {
          operationId: "get_demo_user",
          summary: "查询合成用户",
          parameters: [
            {
              name: "user_id",
              in: "path",
              required: true,
              schema: {
                type: "integer",
                example: 7,
              },
            },
            {
              name: "page_size",
              in: "query",
              required: true,
              schema: {
                type: "integer",
                minimum: 1,
                default: 20,
              },
            },
          ],
          responses: {
            200: {
              description: "合成用户响应",
            },
          },
        },
      },
    },
  };
}

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

function setReviewStatus(message, type) {
  elements.reviewStatus.className = "status-message";
  if (type === "error") {
    elements.reviewStatus.classList.add("status-error");
  } else if (type === "success") {
    elements.reviewStatus.classList.add("status-success");
  }
  elements.reviewStatus.textContent = message;
}

function candidateSourceLabel(source) {
  return source === "ai" ? "AI 候选" : "OpenAPI 基础";
}

function removeReviewSource(source) {
  const next = reviewCandidates.filter((candidate) => candidate.source !== source);
  if (next.length === reviewCandidates.length) {
    return;
  }
  reviewCandidates = next;
  renderCandidateReview();
  setReviewStatus(
    reviewCandidates.length
      ? `已移除失效的${candidateSourceLabel(source)}，其余候选仍保留。`
      : "候选已失效，请根据最新输入重新生成。",
    "idle",
  );
}

function registerReviewSource(source, run, insights) {
  const insightByCaseId = new Map(
    (Array.isArray(insights) ? insights : []).map((insight) => [
      insight.case_id,
      insight,
    ]),
  );
  const retained = reviewCandidates.filter(
    (candidate) => candidate.source !== source,
  );
  const additions = run.cases.map((testCase, index) => {
    const statusAssertion = testCase.assertions.find(
      (assertion) => assertion.type === "status_code",
    );
    const insight = insightByCaseId.get(testCase.id) || null;
    return {
      key: `${source}:${testCase.id}:${index}`,
      source,
      baseUrl: run.base_url,
      caseId: testCase.id,
      selected: true,
      name: testCase.name,
      method: testCase.method,
      path: testCase.path,
      queryText: JSON.stringify(testCase.query || {}, null, 2),
      bodyText:
        testCase.json_body === null || testCase.json_body === undefined
          ? ""
          : JSON.stringify(testCase.json_body, null, 2),
      expectedStatus: String(statusAssertion.expected),
      category: insight ? insight.category : null,
      rationale: insight ? insight.rationale : null,
      error: null,
    };
  });
  reviewCandidates = [...retained, ...additions];
  renderCandidateReview();
  setReviewStatus(
    `已更新${candidateSourceLabel(source)}：${additions.length} 条，` +
      "请逐条审核后再载入运行配置。",
    "success",
  );
  document.querySelector("#candidate-review-title").scrollIntoView({
    behavior: "smooth",
    block: "start",
  });
}

function makeCandidateField(labelText, control, className) {
  const label = makeElement("label", `field ${className || ""}`.trim());
  label.append(makeElement("span", "", labelText), control);
  return label;
}

function updateCandidateValue(candidate, field, value) {
  candidate[field] = value;
  candidate.error = null;
}

function renderCandidateCard(candidate, index) {
  const fieldset = makeElement("fieldset", "candidate-card");
  if (candidate.error) {
    fieldset.classList.add("has-error");
  }
  const legend = makeElement(
    "legend",
    "candidate-legend",
    `${index + 1}. ${candidate.caseId}`,
  );
  const heading = makeElement("div", "candidate-heading");
  const selectLabel = makeElement("label", "candidate-select");
  const checkbox = makeElement("input");
  checkbox.type = "checkbox";
  checkbox.checked = candidate.selected;
  checkbox.setAttribute("aria-label", `选择候选 ${candidate.caseId}`);
  checkbox.addEventListener("change", () => {
    candidate.selected = checkbox.checked;
    updateReviewCount();
  });
  selectLabel.append(
    checkbox,
    makeElement("span", "", candidate.name || candidate.caseId),
  );
  const sourceBadge = makeElement(
    "span",
    `candidate-source${candidate.source === "ai" ? " source-ai" : ""}`,
    candidateSourceLabel(candidate.source),
  );
  heading.append(selectLabel, sourceBadge);
  fieldset.append(legend, heading);

  if (candidate.rationale) {
    const categoryNames = {
      boundary: "边界",
      negative: "异常",
      robustness: "健壮性",
    };
    fieldset.append(
      makeElement(
        "p",
        "candidate-reason",
        `${categoryNames[candidate.category] || "AI"}：${candidate.rationale}`,
      ),
    );
  }

  const fields = makeElement("div", "candidate-fields");
  const nameInput = makeElement("input");
  nameInput.type = "text";
  nameInput.maxLength = 120;
  nameInput.value = candidate.name;
  nameInput.addEventListener("input", () => {
    updateCandidateValue(candidate, "name", nameInput.value);
  });

  const methodSelect = makeElement("select");
  ["GET", "POST", "PUT", "PATCH", "DELETE"].forEach((method) => {
    const option = makeElement("option", "", method);
    option.value = method;
    option.selected = method === candidate.method;
    methodSelect.append(option);
  });
  methodSelect.addEventListener("change", () => {
    updateCandidateValue(candidate, "method", methodSelect.value);
  });

  const pathInput = makeElement("input");
  pathInput.type = "text";
  pathInput.value = candidate.path;
  pathInput.spellcheck = false;
  pathInput.addEventListener("input", () => {
    updateCandidateValue(candidate, "path", pathInput.value);
  });

  const statusInput = makeElement("input");
  statusInput.type = "number";
  statusInput.min = "100";
  statusInput.max = "599";
  statusInput.step = "1";
  statusInput.value = candidate.expectedStatus;
  statusInput.addEventListener("input", () => {
    updateCandidateValue(candidate, "expectedStatus", statusInput.value);
  });

  const queryEditor = makeElement("textarea");
  queryEditor.rows = 4;
  queryEditor.spellcheck = false;
  queryEditor.value = candidate.queryText;
  queryEditor.addEventListener("input", () => {
    updateCandidateValue(candidate, "queryText", queryEditor.value);
  });

  const bodyEditor = makeElement("textarea");
  bodyEditor.rows = 4;
  bodyEditor.spellcheck = false;
  bodyEditor.placeholder = "留空表示不发送 JSON 请求体";
  bodyEditor.value = candidate.bodyText;
  bodyEditor.addEventListener("input", () => {
    updateCandidateValue(candidate, "bodyText", bodyEditor.value);
  });

  fields.append(
    makeCandidateField("用例名称", nameInput, "field-name"),
    makeCandidateField("请求方法", methodSelect, "field-method"),
    makeCandidateField("相对路径", pathInput, "field-path"),
    makeCandidateField("预期状态码", statusInput, "field-status"),
    makeCandidateField("查询参数 JSON", queryEditor, "field-query"),
    makeCandidateField("请求体 JSON", bodyEditor, "field-body"),
  );
  fieldset.append(fields);
  if (candidate.error) {
    fieldset.append(makeElement("p", "candidate-error", candidate.error));
  }
  return fieldset;
}

function updateReviewCount() {
  const selectedCount = reviewCandidates.filter(
    (candidate) => candidate.selected,
  ).length;
  elements.reviewCount.textContent =
    `${reviewCandidates.length} 条候选 · 已选 ${selectedCount}`;
}

function renderCandidateReview() {
  if (reviewCandidates.length === 0) {
    replaceChildren(elements.candidateList, [
      (() => {
        const empty = makeElement("div", "empty-state review-empty");
        empty.append(
          makeElement("h3", "", "候选将在这里逐条展示"),
          makeElement(
            "p",
            "",
            "可以直接编辑名称、方法、路径、查询参数、请求体和预期状态码。",
          ),
        );
        return empty;
      })(),
    ]);
  } else {
    replaceChildren(
      elements.candidateList,
      reviewCandidates.map(renderCandidateCard),
    );
  }
  updateReviewCount();
  syncMutatingControlState();
}

function parseCandidateJson(source, label, candidate) {
  if (!source.trim() && label === "请求体") {
    return null;
  }
  let parsed;
  try {
    parsed = JSON.parse(source);
  } catch (_error) {
    throw new Error(`${candidate.caseId} 的${label}不是有效 JSON。`);
  }
  if (
    label === "查询参数" &&
    (parsed === null || typeof parsed !== "object" || Array.isArray(parsed))
  ) {
    throw new Error(`${candidate.caseId} 的查询参数必须是 JSON 对象。`);
  }
  return parsed;
}

function candidateToTestCase(candidate) {
  const name = candidate.name.trim();
  const path = candidate.path.trim();
  const expectedStatus = Number(candidate.expectedStatus);
  if (!name || name.length > 120) {
    throw new Error(`${candidate.caseId} 的用例名称长度必须是1到120个字符。`);
  }
  if (
    !["GET", "POST", "PUT", "PATCH", "DELETE"].includes(candidate.method)
  ) {
    throw new Error(`${candidate.caseId} 的请求方法不受支持。`);
  }
  if (
    !path.startsWith("/") ||
    path.startsWith("//") ||
    path.includes("://")
  ) {
    throw new Error(`${candidate.caseId} 必须使用以单个 / 开头的相对路径。`);
  }
  if (
    !Number.isInteger(expectedStatus) ||
    expectedStatus < 100 ||
    expectedStatus > 599
  ) {
    throw new Error(`${candidate.caseId} 的预期状态码必须是100到599的整数。`);
  }
  return {
    id: candidate.caseId,
    name,
    method: candidate.method,
    path,
    headers: {},
    query: parseCandidateJson(candidate.queryText, "查询参数", candidate),
    json_body: parseCandidateJson(candidate.bodyText, "请求体", candidate),
    assertions: [
      {
        type: "status_code",
        expected: expectedStatus,
        path: null,
      },
    ],
    depends_on: [],
    extract: [],
  };
}

function normalizedBaseUrl(value) {
  const parsed = new URL(value);
  if (
    !["http:", "https:"].includes(parsed.protocol) ||
    parsed.username ||
    parsed.password ||
    parsed.hash
  ) {
    throw new Error("候选用例的被测地址无效。");
  }
  return parsed.href.replace(/\/$/, "");
}

function readSelectedCandidates() {
  const selected = reviewCandidates.filter((candidate) => candidate.selected);
  if (selected.length === 0) {
    throw new Error("请至少选择一条候选用例。");
  }
  const baseUrls = new Set(
    selected.map((candidate) => normalizedBaseUrl(candidate.baseUrl)),
  );
  if (baseUrls.size !== 1) {
    throw new Error("选中候选来自不同被测地址，不能放入同一次测试运行。");
  }
  const cases = [];
  const ids = new Set();
  for (const candidate of selected) {
    try {
      const testCase = candidateToTestCase(candidate);
      if (ids.has(testCase.id)) {
        throw new Error(`选中候选存在重复用例 ID：${testCase.id}。`);
      }
      ids.add(testCase.id);
      cases.push(testCase);
      candidate.error = null;
    } catch (error) {
      candidate.error = error.message;
      renderCandidateReview();
      throw error;
    }
  }
  return {
    baseUrl: selected[0].baseUrl,
    cases,
  };
}

function applyReviewedCandidates(mode) {
  if (runIsLoading || openApiIsLoading || aiIsLoading) {
    setReviewStatus("当前有任务正在处理，暂时不能载入候选。", "error");
    return;
  }
  let reviewed;
  try {
    reviewed = readSelectedCandidates();
  } catch (error) {
    setReviewStatus(error.message, "error");
    return;
  }

  if (mode === "append") {
    let current;
    try {
      current = readPayload();
    } catch (error) {
      setReviewStatus(`当前测试编辑器无法追加：${error.message}`, "error");
      return;
    }
    if (
      normalizedBaseUrl(current.base_url) !==
      normalizedBaseUrl(reviewed.baseUrl)
    ) {
      setReviewStatus(
        "候选与当前运行配置的被测地址不同；请改用“替换测试编辑器”。",
        "error",
      );
      return;
    }
    const existingIds = new Set(
      current.cases.map((testCase, index) => testCase.id || `case_${index + 1}`),
    );
    const collision = reviewed.cases.find((testCase) =>
      existingIds.has(testCase.id),
    );
    if (collision) {
      setReviewStatus(
        `无法追加：用例 ID ${collision.id} 已存在。`,
        "error",
      );
      return;
    }
    payload = {
      ...current,
      cases: [...current.cases, ...reviewed.cases],
    };
  } else {
    payload = {
      base_url: reviewed.baseUrl,
      variables: {},
      secret_variables: [],
      cases: reviewed.cases,
    };
  }

  elements.baseUrl.value = payload.base_url;
  syncEditor();
  renderCaseOverview();
  resetResults();
  elements.advancedPanel.open = true;
  setReviewStatus(
    mode === "append"
      ? `已追加 ${reviewed.cases.length} 条候选，没有自动运行。`
      : `已用 ${reviewed.cases.length} 条候选替换测试编辑器，没有自动运行。`,
    "success",
  );
  setRunStatus("候选已载入，请检查运行配置后再手动点击运行测试。", "idle");
  document.querySelector("#config-title").scrollIntoView({
    behavior: "smooth",
    block: "start",
  });
}

function setOpenApiStatus(message, type) {
  elements.openApiStatus.className = "status-message";
  if (type === "error") {
    elements.openApiStatus.classList.add("status-error");
  } else if (type === "success") {
    elements.openApiStatus.classList.add("status-success");
  }
  elements.openApiStatus.textContent = message;
}

function renderOpenApiWarnings(warnings) {
  const safeWarnings = Array.isArray(warnings) ? warnings : [];
  if (safeWarnings.length === 0) {
    replaceChildren(elements.openApiWarnings, [
      makeElement("li", "openapi-empty-warning", "没有警告。"),
    ]);
    return;
  }

  const visibleWarnings = safeWarnings.slice(0, 100);
  const items = visibleWarnings.map((warning) => {
    const safeWarning =
      warning && typeof warning === "object" ? warning : {};
    const item = makeElement("li", "openapi-warning-item");
    const heading = makeElement(
      "strong",
      "",
      typeof safeWarning.code === "string"
        ? safeWarning.code
        : "OPENAPI_WARNING",
    );
    const location = makeElement(
      "span",
      "openapi-warning-location",
      typeof safeWarning.location === "string"
        ? safeWarning.location
        : "位置未知",
    );
    const message = makeElement(
      "p",
      "",
      typeof safeWarning.message === "string"
        ? safeWarning.message
        : "该操作未能完整生成。",
    );
    item.append(heading, location, message);
    return item;
  });
  if (safeWarnings.length > visibleWarnings.length) {
    items.push(
      makeElement(
        "li",
        "openapi-empty-warning",
        `另有 ${safeWarnings.length - visibleWarnings.length} 条警告未展开。`,
      ),
    );
  }
  replaceChildren(elements.openApiWarnings, items);
}

function invalidateGeneratedOpenApi(message) {
  generatedRun = null;
  removeReviewSource("openapi");
  elements.applyGeneratedRun.disabled = true;
  elements.openApiGeneratedCount.textContent = "0";
  elements.openApiSkippedCount.textContent = "0";
  replaceChildren(elements.openApiWarnings, [
    makeElement("li", "openapi-empty-warning", "尚未生成，没有警告。"),
  ]);
  if (message) {
    setOpenApiStatus(message, "idle");
  }
}

function syncMutatingControlState() {
  const busy = runIsLoading || openApiIsLoading || aiIsLoading;
  elements.runTests.disabled = busy;
  elements.restoreDemo.disabled = busy;
  elements.baseUrl.disabled = busy;
  elements.jsonEditor.disabled = busy;
  elements.openApiFile.disabled = busy;
  elements.openApiEditor.disabled = busy;
  elements.openApiBaseUrl.disabled = busy;
  elements.openApiMaxCases.disabled = busy;
  elements.loadOpenApiDemo.disabled = busy;
  elements.generateOpenApi.disabled = busy;
  elements.applyGeneratedRun.disabled = busy || generatedRun === null;
  elements.aiObjective.disabled = busy;
  elements.aiMaxCases.disabled = busy;
  elements.generateAi.disabled = busy;
  elements.applyAiRun.disabled = busy || generatedAiRun === null;
  const hasCandidates = reviewCandidates.length > 0;
  elements.selectAllCandidates.disabled = busy || !hasCandidates;
  elements.clearCandidateSelection.disabled = busy || !hasCandidates;
  elements.replaceEditorCandidates.disabled = busy || !hasCandidates;
  elements.appendEditorCandidates.disabled = busy || !hasCandidates;
  elements.candidateList
    .querySelectorAll("input, select, textarea")
    .forEach((control) => {
      control.disabled = busy;
    });
}

function setOpenApiLoading(isLoading) {
  openApiIsLoading = isLoading;
  syncMutatingControlState();
  elements.generateOpenApi.textContent = isLoading
    ? "正在生成…"
    : "生成基础用例";
  elements.generateOpenApi.setAttribute("aria-busy", String(isLoading));
}

function loadOpenApiDemo() {
  elements.openApiEditor.value = JSON.stringify(createOpenApiDemo(), null, 2);
  elements.openApiBaseUrl.value = "";
  elements.openApiMaxCases.value = "20";
  elements.openApiFile.value = "";
  invalidateGeneratedOpenApi("已加载同源合成演示，尚未生成或运行。");
  invalidateGeneratedAi("OpenAPI 输入已变化，请重新生成 AI 候选。");
}

async function handleOpenApiFile(event) {
  const file = event.target.files && event.target.files[0];
  if (!file) {
    return;
  }
  invalidateGeneratedOpenApi();
  invalidateGeneratedAi();
  if (file.size > MAX_OPENAPI_FILE_BYTES) {
    elements.openApiFile.value = "";
    setOpenApiStatus("文件超过1 MiB，未读取任何内容。", "error");
    return;
  }
  const fileName = typeof file.name === "string" ? file.name.toLowerCase() : "";
  const fileType = typeof file.type === "string" ? file.type.toLowerCase() : "";
  const hasJsonExtension = fileName.endsWith(".json");
  const hasJsonMime = fileType === "application/json";
  if (!hasJsonExtension && !hasJsonMime) {
    elements.openApiFile.value = "";
    setOpenApiStatus("请选择扩展名为 .json 的 JSON 文件。", "error");
    return;
  }

  setOpenApiLoading(true);
  setOpenApiStatus("正在读取 JSON 文件…", "idle");
  try {
    const source = await file.text();
    const parsed = JSON.parse(source);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      throw new Error("文件内容必须是 JSON 对象。");
    }
    elements.openApiEditor.value = JSON.stringify(parsed, null, 2);
    setOpenApiStatus("JSON 文件已载入内存，尚未生成或运行。", "success");
  } catch (error) {
    setOpenApiStatus(
      error instanceof SyntaxError
        ? `JSON 文件格式有误：${error.message}`
        : error instanceof Error
          ? error.message
          : "无法读取该文件，请选择有效的 JSON 文件。",
      "error",
    );
  } finally {
    elements.openApiFile.value = "";
    setOpenApiLoading(false);
  }
}

function readOpenApiRequest() {
  const source = elements.openApiEditor.value.trim();
  if (!source) {
    throw new Error("请粘贴 OpenAPI JSON 或加载合成演示。");
  }
  if (new TextEncoder().encode(source).byteLength > MAX_OPENAPI_FILE_BYTES) {
    throw new Error("OpenAPI JSON 超过1 MiB，请缩小文档后重试。");
  }

  let openApiDocument;
  try {
    openApiDocument = JSON.parse(source);
  } catch (error) {
    throw new Error(`OpenAPI JSON 格式有误：${error.message}`);
  }
  if (
    !openApiDocument ||
    typeof openApiDocument !== "object" ||
    Array.isArray(openApiDocument)
  ) {
    throw new Error("OpenAPI JSON 必须是一个对象。");
  }

  const maxCases = Number(elements.openApiMaxCases.value);
  if (!Number.isInteger(maxCases) || maxCases < 1 || maxCases > 50) {
    throw new Error("最大用例数必须是1到50之间的整数。");
  }

  const request = {
    document: openApiDocument,
    max_cases: maxCases,
  };
  const baseUrlOverride = elements.openApiBaseUrl.value.trim();
  if (baseUrlOverride) {
    request.base_url = baseUrlOverride;
  }
  return request;
}

function validateGeneratedResponse(data) {
  const cases =
    data && data.run && Array.isArray(data.run.cases) ? data.run.cases : [];
  const isPlainObject = (value) =>
    value !== null &&
    typeof value === "object" &&
    !Array.isArray(value) &&
    Object.getPrototypeOf(value) === Object.prototype;
  const warningsAreValid =
    data &&
    Array.isArray(data.warnings) &&
    !data.warnings.some(
      (warning) =>
        !warning ||
        typeof warning !== "object" ||
        typeof warning.location !== "string" ||
        typeof warning.code !== "string" ||
        typeof warning.message !== "string",
    );
  let baseUrlIsValid = false;
  if (
    data &&
    data.run &&
    typeof data.run.base_url === "string" &&
    !data.run.base_url.includes("{") &&
    !data.run.base_url.includes("}")
  ) {
    try {
      const parsedBaseUrl = new URL(data.run.base_url);
      baseUrlIsValid =
        ["http:", "https:"].includes(parsedBaseUrl.protocol) &&
        parsedBaseUrl.hostname !== "" &&
        parsedBaseUrl.username === "" &&
        parsedBaseUrl.password === "" &&
        parsedBaseUrl.hash === "";
    } catch (_error) {
      baseUrlIsValid = false;
    }
  }
  const casesAreValid =
    cases.length >= 1 &&
    cases.length <= 50 &&
    cases.every(
      (testCase) =>
        testCase &&
        typeof testCase === "object" &&
        typeof testCase.id === "string" &&
        typeof testCase.name === "string" &&
        ["GET", "POST", "PUT", "PATCH", "DELETE"].includes(testCase.method) &&
        typeof testCase.path === "string" &&
        testCase.path.startsWith("/") &&
        !testCase.path.startsWith("//") &&
        isPlainObject(testCase.headers) &&
        Object.keys(testCase.headers).length === 0 &&
        Array.isArray(testCase.depends_on) &&
        testCase.depends_on.length === 0 &&
        Array.isArray(testCase.extract) &&
        testCase.extract.length === 0 &&
        Array.isArray(testCase.assertions) &&
        testCase.assertions.length === 1 &&
        testCase.assertions.every(
          (assertion) =>
            assertion &&
            typeof assertion === "object" &&
            assertion.type === "status_code" &&
            Number.isInteger(assertion.expected) &&
            assertion.expected >= 200 &&
            assertion.expected <= 299 &&
            (assertion.path === null || assertion.path === undefined),
        ),
    );
  if (
    !data ||
    typeof data !== "object" ||
    !Number.isInteger(data.generated_count) ||
    data.generated_count < 0 ||
    !Number.isInteger(data.skipped_count) ||
    data.skipped_count < 0 ||
    !warningsAreValid ||
    !data.run ||
    typeof data.run !== "object" ||
    !baseUrlIsValid ||
    !isPlainObject(data.run.variables) ||
    Object.keys(data.run.variables).length !== 0 ||
    !Array.isArray(data.run.secret_variables) ||
    data.run.secret_variables.length !== 0 ||
    !casesAreValid ||
    data.generated_count !== cases.length
  ) {
    throw new Error("服务返回的生成结果结构不完整，未载入测试编辑器。");
  }
  return data;
}

function renderOpenApiResult(result) {
  elements.openApiGeneratedCount.textContent = String(result.generated_count);
  elements.openApiSkippedCount.textContent = String(result.skipped_count);
  renderOpenApiWarnings(result.warnings);
  setOpenApiStatus(
    `生成完成：${result.generated_count} 条可用，${result.skipped_count} 条跳过。请检查后再载入。`,
    result.generated_count > 0 ? "success" : "error",
  );
}

async function generateOpenApiCases() {
  if (runIsLoading || openApiIsLoading || aiIsLoading) {
    setOpenApiStatus("当前有任务正在处理，请稍候。", "error");
    return;
  }
  let request;
  try {
    request = readOpenApiRequest();
  } catch (error) {
    invalidateGeneratedOpenApi();
    setOpenApiStatus(error.message, "error");
    elements.openApiEditor.focus();
    return;
  }

  invalidateGeneratedOpenApi();
  if (openApiAbortController) {
    openApiAbortController.abort();
  }
  const requestSequence = ++openApiRequestSequence;
  const controller = new AbortController();
  openApiAbortController = controller;
  setOpenApiLoading(true);
  setOpenApiStatus("正在同源生成基础用例，不会执行测试…", "idle");
  try {
    const response = await fetch("/api/v1/openapi/generate", {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        Accept: "application/json",
      },
      body: JSON.stringify(request),
      signal: controller.signal,
    });
    let data;
    try {
      data = await response.json();
    } catch (_error) {
      throw new Error(`服务返回 HTTP ${response.status}，但响应不是有效 JSON。`);
    }
    if (requestSequence !== openApiRequestSequence) {
      return;
    }
    if (!response.ok) {
      throw new Error(describeApiError(data, response.status));
    }
    const result = validateGeneratedResponse(data);
    generatedRun = result.run;
    renderOpenApiResult(result);
    registerReviewSource("openapi", result.run, []);
  } catch (error) {
    if (
      requestSequence !== openApiRequestSequence ||
      (error && error.name === "AbortError")
    ) {
      return;
    }
    invalidateGeneratedOpenApi();
    setOpenApiStatus(
      error instanceof Error
        ? `无法生成用例：${error.message}`
        : "无法生成用例，请确认服务仍在启动。",
      "error",
    );
  } finally {
    if (requestSequence === openApiRequestSequence) {
      openApiAbortController = null;
      setOpenApiLoading(false);
    }
  }
}

function applyGeneratedRun() {
  if (runIsLoading || openApiIsLoading || aiIsLoading) {
    setOpenApiStatus("当前有任务正在处理，暂时不能载入。", "error");
    return;
  }
  if (!generatedRun) {
    setOpenApiStatus("当前没有可载入的生成结果。", "error");
    return;
  }
  setOpenApiStatus("基础草稿已进入审核工作台。", "success");
  document.querySelector("#candidate-review-title").scrollIntoView({
    behavior: "smooth",
    block: "start",
  });
}

function setAiStatus(message, type) {
  elements.aiStatus.className = "status-message";
  if (type === "error") {
    elements.aiStatus.classList.add("status-error");
  } else if (type === "success") {
    elements.aiStatus.classList.add("status-success");
  }
  elements.aiStatus.textContent = message;
}

function invalidateGeneratedAi(message) {
  generatedAiRun = null;
  removeReviewSource("ai");
  elements.applyAiRun.disabled = true;
  replaceChildren(elements.aiInsights, [
    makeElement("li", "openapi-empty-warning", "尚未生成候选用例。"),
  ]);
  if (message) {
    setAiStatus(message, "idle");
  }
}

function setAiLoading(isLoading) {
  aiIsLoading = isLoading;
  syncMutatingControlState();
  elements.generateAi.textContent = isLoading ? "正在生成…" : "生成 AI 候选";
  elements.generateAi.setAttribute("aria-busy", String(isLoading));
}

function readAiRequest() {
  const request = readOpenApiRequest();
  const maxCases = Number(elements.aiMaxCases.value);
  const objective = elements.aiObjective.value.trim();
  if (!objective) {
    throw new Error("请填写测试目标。");
  }
  if (objective.length > 500) {
    throw new Error("测试目标不能超过500个字符。");
  }
  if (!Number.isInteger(maxCases) || maxCases < 1 || maxCases > 10) {
    throw new Error("最大候选数必须是1到10之间的整数。");
  }
  request.max_cases = maxCases;
  request.objective = objective;
  return request;
}

function validateAiResponse(data) {
  const isPlainObject = (value) =>
    value !== null &&
    typeof value === "object" &&
    !Array.isArray(value) &&
    Object.getPrototypeOf(value) === Object.prototype;
  const run = data && data.run;
  const cases = run && Array.isArray(run.cases) ? run.cases : [];
  const insights = data && Array.isArray(data.insights) ? data.insights : [];
  let baseUrlIsValid = false;
  if (run && typeof run.base_url === "string") {
    try {
      const parsed = new URL(run.base_url);
      baseUrlIsValid =
        ["http:", "https:"].includes(parsed.protocol) &&
        parsed.hostname !== "" &&
        parsed.username === "" &&
        parsed.password === "" &&
        parsed.hash === "" &&
        !run.base_url.includes("{") &&
        !run.base_url.includes("}");
    } catch (_error) {
      baseUrlIsValid = false;
    }
  }
  const casesAreValid =
    cases.length >= 1 &&
    cases.length <= 10 &&
    new Set(cases.map((testCase) => testCase && testCase.id)).size ===
      cases.length &&
    cases.every(
      (testCase) =>
        isPlainObject(testCase) &&
        typeof testCase.id === "string" &&
        typeof testCase.name === "string" &&
        ["GET", "POST", "PUT", "PATCH", "DELETE"].includes(testCase.method) &&
        typeof testCase.path === "string" &&
        testCase.path.startsWith("/") &&
        !testCase.path.startsWith("//") &&
        !testCase.path.includes("://") &&
        isPlainObject(testCase.headers) &&
        Object.keys(testCase.headers).length === 0 &&
        isPlainObject(testCase.query) &&
        Array.isArray(testCase.depends_on) &&
        testCase.depends_on.length === 0 &&
        Array.isArray(testCase.extract) &&
        testCase.extract.length === 0 &&
        Array.isArray(testCase.assertions) &&
        testCase.assertions.length === 1 &&
        testCase.assertions.every(
          (assertion) =>
            isPlainObject(assertion) &&
            assertion.type === "status_code" &&
            Number.isInteger(assertion.expected) &&
            assertion.expected >= 100 &&
            assertion.expected <= 599 &&
            (assertion.path === null || assertion.path === undefined),
        ),
    );
  const caseIds = new Set(cases.map((testCase) => testCase.id));
  const insightsAreValid =
    insights.length === cases.length &&
    insights.every(
      (insight) =>
        isPlainObject(insight) &&
        caseIds.has(insight.case_id) &&
        ["boundary", "negative", "robustness"].includes(insight.category) &&
        typeof insight.rationale === "string" &&
        insight.rationale.length >= 1 &&
        insight.rationale.length <= 500,
    );
  const warningsAreValid =
    Array.isArray(data && data.warnings) &&
    data.warnings.every(
      (warning) =>
        isPlainObject(warning) &&
        typeof warning.location === "string" &&
        typeof warning.code === "string" &&
        typeof warning.message === "string",
    );
  if (
    !isPlainObject(data) ||
    typeof data.provider !== "string" ||
    (data.model !== null &&
      data.model !== undefined &&
      typeof data.model !== "string") ||
    data.requires_human_review !== true ||
    !Number.isInteger(data.generated_count) ||
    data.generated_count !== cases.length ||
    !isPlainObject(run) ||
    !isPlainObject(run.variables) ||
    Object.keys(run.variables).length !== 0 ||
    !Array.isArray(run.secret_variables) ||
    run.secret_variables.length !== 0 ||
    !baseUrlIsValid ||
    !casesAreValid ||
    !insightsAreValid ||
    !warningsAreValid
  ) {
    throw new Error("AI 服务返回了不符合安全契约的响应，结果未载入。");
  }
  return data;
}

function renderAiResult(result) {
  const categoryNames = {
    boundary: "边界",
    negative: "异常",
    robustness: "健壮性",
  };
  const items = result.insights.map((insight) => {
    const item = makeElement("li", "ai-insight");
    item.append(
      makeElement("span", "", categoryNames[insight.category]),
      makeElement("strong", "", insight.case_id),
      makeElement("p", "", insight.rationale),
    );
    return item;
  });
  replaceChildren(elements.aiInsights, items);
  const modelText = result.model ? ` · ${result.model}` : "";
  elements.aiProviderDetail.textContent =
    `Provider：${result.provider}${modelText} · ${result.generated_count} 条候选`;
  setAiStatus(
    `已生成 ${result.generated_count} 条候选，请人工检查后再载入。`,
    "success",
  );
}

async function generateAiCases() {
  if (runIsLoading || openApiIsLoading || aiIsLoading) {
    setAiStatus("当前有任务正在处理，请稍候。", "error");
    return;
  }
  let request;
  try {
    request = readAiRequest();
  } catch (error) {
    invalidateGeneratedAi();
    setAiStatus(error.message, "error");
    return;
  }
  invalidateGeneratedAi();
  if (aiAbortController) {
    aiAbortController.abort();
  }
  const requestSequence = ++aiRequestSequence;
  const controller = new AbortController();
  aiAbortController = controller;
  setAiLoading(true);
  setAiStatus("正在生成候选；真实 Provider 可能产生 API 费用…", "idle");
  try {
    const response = await fetch("/api/v1/ai/cases/generate", {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        Accept: "application/json",
      },
      body: JSON.stringify(request),
      signal: controller.signal,
    });
    let data;
    try {
      data = await response.json();
    } catch (_error) {
      throw new Error(`服务返回 HTTP ${response.status}，但响应不是有效 JSON。`);
    }
    if (requestSequence !== aiRequestSequence) {
      return;
    }
    if (!response.ok) {
      throw new Error(describeApiError(data, response.status));
    }
    const result = validateAiResponse(data);
    generatedAiRun = result.run;
    renderAiResult(result);
    registerReviewSource("ai", result.run, result.insights);
  } catch (error) {
    if (
      requestSequence !== aiRequestSequence ||
      (error && error.name === "AbortError")
    ) {
      return;
    }
    invalidateGeneratedAi();
    setAiStatus(
      error instanceof Error
        ? `无法生成 AI 候选：${error.message}`
        : "无法生成 AI 候选，请确认服务仍在启动。",
      "error",
    );
  } finally {
    if (requestSequence === aiRequestSequence) {
      aiAbortController = null;
      setAiLoading(false);
    }
  }
}

function applyAiRun() {
  if (runIsLoading || openApiIsLoading || aiIsLoading) {
    setAiStatus("当前有任务正在处理，暂时不能载入。", "error");
    return;
  }
  if (!generatedAiRun) {
    setAiStatus("当前没有可载入的 AI 候选。", "error");
    return;
  }
  setAiStatus("AI 候选已进入审核工作台。", "success");
  document.querySelector("#candidate-review-title").scrollIntoView({
    behavior: "smooth",
    block: "start",
  });
}

async function loadAiStatus() {
  try {
    const response = await fetch("/api/v1/ai/status", {
      method: "GET",
      credentials: "same-origin",
      headers: { Accept: "application/json" },
    });
    const data = await response.json();
    if (!response.ok || !data || typeof data.provider !== "string") {
      throw new Error("Provider 状态不可用");
    }
    elements.aiProviderBadge.textContent =
      data.provider === "mock" ? "Mock · 本地" : "OpenAI";
    elements.aiProviderDetail.textContent = data.configured
      ? `Provider 已就绪${data.model ? ` · ${data.model}` : ""}`
      : "Provider 尚未配置 API Key";
    if (!data.configured) {
      setAiStatus("OpenAI Provider 尚未配置，请在后端设置 API Key。", "error");
    }
  } catch (_error) {
    elements.aiProviderBadge.textContent = "状态未知";
    elements.aiProviderDetail.textContent = "无法读取 Provider 状态。";
  }
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
  runIsLoading = isLoading;
  syncMutatingControlState();
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
  if (openApiIsLoading || aiIsLoading || runIsLoading) {
    setRunStatus("当前有任务正在处理，请稍候。", "error");
    return;
  }
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

function isCanonicalUuidV4(runId) {
  return typeof runId === "string" && UUID_V4_PATTERN.test(runId);
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
  if (isCanonicalUuidV4(safeItem.run_id)) {
    const encodedRunId = encodeURIComponent(safeItem.run_id);
    const reportLink = makeElement(
      "a",
      "history-report-link",
      "下载报告",
    );
    reportLink.href = `/api/v1/runs/${encodedRunId}/report`;
    reportLink.setAttribute("download", "");
    reportLink.setAttribute(
      "aria-label",
      `下载 ${formatDate(safeItem.created_at)} 的 HTML 测试报告`,
    );
    listItem.append(reportLink);
  }
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
elements.openApiFile.addEventListener("change", (event) => {
  void handleOpenApiFile(event);
});
elements.loadOpenApiDemo.addEventListener("click", loadOpenApiDemo);
elements.generateOpenApi.addEventListener("click", () => {
  void generateOpenApiCases();
});
elements.applyGeneratedRun.addEventListener("click", applyGeneratedRun);
elements.generateAi.addEventListener("click", () => {
  void generateAiCases();
});
elements.applyAiRun.addEventListener("click", applyAiRun);
elements.selectAllCandidates.addEventListener("click", () => {
  reviewCandidates.forEach((candidate) => {
    candidate.selected = true;
  });
  renderCandidateReview();
  setReviewStatus("已选择全部候选。", "idle");
});
elements.clearCandidateSelection.addEventListener("click", () => {
  reviewCandidates.forEach((candidate) => {
    candidate.selected = false;
  });
  renderCandidateReview();
  setReviewStatus("已取消全部候选选择。", "idle");
});
elements.replaceEditorCandidates.addEventListener("click", () => {
  applyReviewedCandidates("replace");
});
elements.appendEditorCandidates.addEventListener("click", () => {
  applyReviewedCandidates("append");
});

function handleOpenApiInputChange() {
  if (openApiAbortController) {
    openApiAbortController.abort();
    openApiAbortController = null;
    openApiRequestSequence += 1;
    setOpenApiLoading(false);
  }
}

[
  elements.openApiEditor,
  elements.openApiBaseUrl,
  elements.openApiMaxCases,
].forEach((control) => {
  control.addEventListener("input", () => {
    handleOpenApiInputChange();
    invalidateGeneratedOpenApi("OpenAPI 输入已变化，请重新生成。");
    if (aiAbortController) {
      aiAbortController.abort();
      aiAbortController = null;
      aiRequestSequence += 1;
      setAiLoading(false);
    }
    invalidateGeneratedAi("OpenAPI 输入已变化，请重新生成 AI 候选。");
  });
});

[elements.aiObjective, elements.aiMaxCases].forEach((control) => {
  control.addEventListener("input", () => {
    if (aiAbortController) {
      aiAbortController.abort();
      aiAbortController = null;
      aiRequestSequence += 1;
      setAiLoading(false);
    }
    invalidateGeneratedAi("AI 生成条件已变化，请重新生成。");
  });
});

restoreDemo();
loadOpenApiDemo();
renderCandidateReview();
syncMutatingControlState();
void loadHistory();
void loadAiStatus();
