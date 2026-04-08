import json
import tempfile
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


def render_chart_spec_to_png(chart_spec: dict, timeout_ms: int = 30000) -> bytes | None:
    """
    使用无头浏览器把 ECharts 配置直接渲染为 PNG。

    这条能力用于当前的结构化图表导出链，避免调用方必须先把图表落成 HTML 文件。
    """
    if not isinstance(chart_spec, dict) or not chart_spec:
        return None

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>chart-export</title>
  <script src="https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js"></script>
  <style>
    html, body {{
      margin: 0;
      padding: 0;
      width: 100%;
      height: 100%;
      background: #ffffff;
    }}
    .chart-container {{
      width: 1080px;
      height: 720px;
      background: #ffffff;
    }}
  </style>
</head>
<body>
  <div id="chart" class="chart-container"></div>
  <script>
    const option = {json.dumps(chart_spec, ensure_ascii=False)};
    const chart = echarts.init(document.getElementById('chart'), null, {{renderer: 'canvas'}});
    chart.setOption(option, true);
  </script>
</body>
</html>"""

    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile('w', suffix='.html', delete=False, encoding='utf-8') as temp_file:
            temp_file.write(html)
            temp_path = Path(temp_file.name)

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                context = browser.new_context(
                    viewport={'width': 1080, 'height': 720},
                    device_scale_factor=2,
                    ignore_https_errors=True,
                )
                page = context.new_page()
                page.goto(temp_path.as_uri(), wait_until='networkidle', timeout=timeout_ms)
                container = page.locator('.chart-container')
                if container.count() == 0:
                    return None
                return container.screenshot(type='png')
            finally:
                browser.close()
    except Exception:
        return None
    finally:
        if temp_path and temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass
