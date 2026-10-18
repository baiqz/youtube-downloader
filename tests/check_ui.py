"""界面静态自检：不启浏览器也能抓住三类致命错。

1. 内联 JS 语法错误（用 node --check）
2. JS 引用了 HTML 里不存在的 id / class 选择器
3. 前端调的 /api 路径后端根本没实现
"""
import json
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML = os.path.join(ROOT, "static", "index.html")
APP = os.path.join(ROOT, "app.py")
NODE = r"C:\Users\Administrator\.workbuddy\binaries\node\versions\22.22.2-6\node.exe"

FAIL = []


def check(name, cond, extra=""):
    print(("  ok   " if cond else "  FAIL ") + name + ("" if cond else f"  {extra}"))
    if not cond:
        FAIL.append(name)


html = open(HTML, encoding="utf-8").read()
app_src = open(APP, encoding="utf-8").read()

scripts = re.findall(r"<script[^>]*>(.*?)</script>", html, re.S)
js = "\n".join(scripts)

# ---------- 1. JS 语法 ----------
tmp = os.path.join(tempfile.gettempdir(), "_ui_check.js")
with open(tmp, "w", encoding="utf-8") as f:
    f.write(js)
p = subprocess.run([NODE, "--check", tmp], capture_output=True, text=True)
check("内联 JS 语法正确", p.returncode == 0, p.stderr.strip()[:400])

# ---------- 2. DOM id 引用 ----------
html_ids = set(re.findall(r'\bid="([A-Za-z0-9_-]+)"', html))
js_ids = set(re.findall(r"""\$\(\s*['"]#([A-Za-z0-9_-]+)['"]""", js))
js_ids |= set(re.findall(r"""querySelector(?:All)?\(\s*['"]#([A-Za-z0-9_-]+)['"]""", js))
missing = sorted(js_ids - html_ids)
check(f"JS 引用的 {len(js_ids)} 个 id 都存在", not missing, f"缺失: {missing}")

# 动态拼接的 id（#jobList 之类）已在上面覆盖；再查 CSS 里用到的 id
css_ids = set(re.findall(r"#([A-Za-z][A-Za-z0-9_-]{2,})\s*\{", html))
ghost = sorted(c for c in css_ids - html_ids if not re.match(r"^(fff|000|[0-9a-f]{3,8})$", c))
check("CSS 里没有指向不存在 id 的样式", not ghost, f"多余: {ghost}")

# ---------- 3. 前后端接口对齐 ----------
routes = []
for m in re.finditer(r'@app\.(get|post|route)\(\s*["\']([^"\']+)["\']', app_src):
    path = m.group(2)
    tmpl = re.sub(r"<[^>]+>", "[^/]+", path)
    routes.append((path, re.compile("^" + tmpl + "/?$")))

# 前端出现的所有 /api 字面量（含 'a' + id 这种拼接，只取静态前缀）
calls = set(re.findall(r"""['"](/api/[A-Za-z0-9_/\-.]*)['"]""", js))
calls |= set(re.findall(r"""fetch\(\s*['"](/api/[^'"?]*)""", js))
unmatched = []
for c in sorted(calls):
    ok = any(rx.match(c) or c.startswith(rp.rsplit("/", 1)[0] + "/") for rp, rx in routes)
    if not ok:
        unmatched.append(c)
check(f"前端调的 {len(calls)} 个接口后端都实现了", not unmatched, f"未匹配: {unmatched}")

# 反向：后端关键接口前端有没有接上（避免做了没人用）
for must in ["/api/info", "/api/download", "/api/health"]:
    check(f"前端接上了 {must}", any(c.startswith(must) for c in calls))

# ---------- 4. 一些容易忽略的坑 ----------
check("没有残留的 <script src=", "<script src=" not in html)
check("没有 TODO 占位", "TODO" not in js and "TODO" not in app_src)
check("CSS 花括号配平", html.count("{") == html.count("}"), f"{html.count('{')} vs {html.count('}')}")
check("页面声明了 viewport", 'name="viewport"' in html)
check("缩略图带 no-referrer", html.count("referrerpolicy=\"no-referrer\"") >= 2)

# 关键交互：选中态 / 进度条 / SSE 兜底 都在
for token, name in [
    (".opt.sel", "清晰度选中态样式"),
    ("bar i", "进度条"),
    ("new EventSource", "SSE 订阅"),
    ("es.onerror", "SSE 断线兜底"),
    ("/cancel", "取消任务"),
]:
    check(f"有 {name}", token in html)

print()
if FAIL:
    print(f"FAILED {len(FAIL)}: {FAIL}")
    sys.exit(1)
print("全部通过")
