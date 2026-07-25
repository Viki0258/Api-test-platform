from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app


client = TestClient(app)
repository_root = Path(__file__).resolve().parent.parent
frontend_directory = repository_root / "frontend"


def test_root_serves_the_chinese_visual_console() -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")

    html = response.text
    assert '<html lang="zh-CN">' in html
    assert "智能接口自动化测试" in html
    assert "运行测试" in html
    assert "加载演示" in html
    assert "总数" in html
    assert "通过" in html
    assert "失败" in html
    assert "跳过" in html


def test_root_references_only_the_local_static_assets() -> None:
    response = client.get("/")

    assert response.status_code == 200
    html = response.text
    assert 'href="/static/styles.css"' in html
    assert 'src="/static/app.js"' in html

    asset_urls = re.findall(
        r"""(?:src|href)\s*=\s*["']([^"']+)["']""",
        html,
        flags=re.IGNORECASE,
    )
    assert asset_urls
    assert all(
        value.startswith(("/", "#")) and not value.startswith("//")
        for value in asset_urls
    )


def test_static_css_and_javascript_are_served() -> None:
    css_response = client.get("/static/styles.css")
    javascript_response = client.get("/static/app.js")

    assert css_response.status_code == 200
    assert css_response.headers["content-type"].startswith("text/css")
    assert ":focus-visible" in css_response.text
    assert "@media" in css_response.text

    assert javascript_response.status_code == 200
    assert "javascript" in javascript_response.headers["content-type"]
    assert "/api/v1/runs" in javascript_response.text


def test_console_exposes_keyboard_and_status_dom_contracts() -> None:
    html = client.get("/").text

    assert re.search(
        r"""<button\b[^>]*\bid=["']run-tests["'][^>]*>"""
        r"(?:(?!</button>).)*运行测试(?:(?!</button>).)*</button>",
        html,
        re.DOTALL,
    )
    assert re.search(
        r"""<button\b[^>]*\bid=["']restore-demo["'][^>]*>"""
        r"\s*加载演示\s*</button>",
        html,
    )
    assert re.search(r"""aria-live\s*=\s*["'](?:polite|assertive)["']""", html)
    assert "<textarea" in html
    assert re.search(r"""<label\b[^>]*\bfor\s*=\s*["'][^"']+["']""", html)


def test_frontend_security_contract_uses_same_origin_and_safe_text_rendering() -> None:
    javascript = (frontend_directory / "app.js").read_text(encoding="utf-8")

    assert re.search(
        r"""fetch\s*\(\s*["']/api/v1/runs["']""",
        javascript,
    )
    assert "textContent" in javascript
    assert "innerHTML" not in javascript
    assert "localStorage" not in javascript
    assert "sessionStorage" not in javascript
    assert "insertAdjacentHTML" not in javascript
    assert "document.write" not in javascript
    assert re.search(r"""credentials\s*:\s*["']same-origin["']""", javascript)
    assert re.search(r"\b(?:window\.)?location\.origin\b", javascript)


def test_default_demo_exposes_the_two_case_dependency_workflow() -> None:
    javascript = (frontend_directory / "app.js").read_text(encoding="utf-8")

    assert "createDemoPayload" in javascript
    assert 'id: "fetch_user"' in javascript
    assert 'id: "verify_extracted_user"' in javascript
    assert 'depends_on: ["fetch_user"]' in javascript
    assert 'name: "user_id"' in javascript
    assert 'name: "user_name"' in javascript
    assert "正在运行" in javascript
    assert "请求失败" in javascript
    assert "空结果" in javascript


def test_frontend_has_no_external_resource_or_network_reference() -> None:
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (
            frontend_directory / "index.html",
            frontend_directory / "styles.css",
            frontend_directory / "app.js",
        )
    )

    assert not re.search(r"""(?:https?:)?//[A-Za-z0-9]""", source)
    assert "@import" not in source
