from pathlib import Path

from playwright.sync_api import sync_playwright


def resolve_local_file_path(file_id: str) -> Path | None:
    """把文件标识解析为本地绝对路径。"""
    if not file_id:
        return None

    path = Path(file_id)
    if path.is_absolute():
        return path if path.exists() else None

    project_root = Path(__file__).resolve().parents[2]
    candidate = project_root / file_id
    return candidate if candidate.exists() else None


def render_chart_file_to_png(file_id: str, timeout_ms: int = 30000) -> bytes | None:
    """
    使用无头浏览器把图表 HTML 渲染成 PNG。

    当前主要用于：
    1. 分析结果 PDF 导出
    2. 后续图表下载接口复用
    """
    chart_path = resolve_local_file_path(file_id)
    if chart_path is None or chart_path.suffix.lower() != '.html':
        return None

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                context = browser.new_context(
                    viewport={'width': 1080, 'height': 720},
                    device_scale_factor=2,
                    ignore_https_errors=True,
                )
                page = context.new_page()
                page.goto(chart_path.as_uri(), wait_until='networkidle', timeout=timeout_ms)
                container = page.locator('.chart-container')
                if container.count() == 0:
                    return None
                return container.screenshot(type='png')
            finally:
                browser.close()
    except Exception:
        return None
