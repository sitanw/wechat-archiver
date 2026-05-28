# 飞书妙记自动抓取 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 bot 收到飞书妙记 URL 时,自动通过 Feishu Open Platform API 拉转写文本,落 DOCX 到 ARCHIVE_DIR,纳入现有 pending tag 流程。

**Architecture:** 三新模块 `lib/feishu_{token_store,auth_cli,minutes_fetcher}.py` + 三处小改 `lib/url_detect.py` `handlers/text.py` `lib/tag_parser.py`。OAuth 走独立 CLI(`python -m feishu_auth_cli`),token 存项目根 JSON。运行时 fetcher 调 `get_valid_token()` → 自动 refresh → 拉转写 → 拼 DOCX。

**Tech Stack:** Python 3.9+, `httpx`(已有)、`python-docx`(已有)、`http.server`/`webbrowser`(标准库)。**无新 deps**。

**Verification 方式:** 项目无 pytest 基础,沿用 CONTEXT.md 的手测路径。每个任务结束跑明确的 smoke 命令,核对预期输出再 commit。

**参考 spec:** [`docs/superpowers/specs/2026-05-28-feishu-minutes-fetcher-design.md`](../specs/2026-05-28-feishu-minutes-fetcher-design.md)

**分三阶段执行,每个阶段独立可验证**:
- **Phase A**(Task 1-3):CLI 走通 OAuth,拿到 token 文件
- **Phase B**(Task 4-6):fetcher 隔离测试,能拿到转写并落 DOCX
- **Phase C**(Task 7-9):接入 bot,微信端 E2E

---

## Phase A — CLI OAuth 走通

### Task 1:配置项 + gitignore

**Files:**
- Modify: `.env.example`
- Modify: `.gitignore`

- [ ] **Step 1:** `.env.example` 末尾追加飞书三项配置说明

打开 `.env.example`,在末尾追加:

```env

# 飞书开放平台应用凭证(用于抓取飞书妙记转写)
# 从 https://open.feishu.cn 开发者后台拿。留空则飞书功能不可用(只是回执提示,不影响其他功能)
FEISHU_APP_ID=
FEISHU_APP_SECRET=

# 飞书 OAuth token 存储路径,留空则用项目根 ./feishu_token.json
# 跨项目复用可以指 ~/.feishu-archiver/token.json
FEISHU_TOKEN_PATH=
```

- [ ] **Step 2:** `.gitignore` 加上 `feishu_token.json`

打开 `.gitignore`,在 `inbox/` 之后追加:

```gitignore

# 飞书 OAuth token,含 refresh_token,不入版本控制
feishu_token.json
```

- [ ] **Step 3:** Commit

```bash
git add .env.example .gitignore
git commit -m "Feishu Minutes: add .env vars + gitignore token file"
```

---

### Task 2:`lib/feishu_token_store.py` — token 生命周期

**Files:**
- Create: `lib/feishu_token_store.py`

- [ ] **Step 1:** 创建文件,写入下面完整内容

```python
"""
飞书 OAuth token 生命周期管理。

被 feishu_auth_cli(写入)和 feishu_minutes_fetcher(读 + 自动 refresh)共享。
设计上零 IO 副作用以外的状态;每次 get_valid_token 都自查过期 + 必要时 refresh + 落盘。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import httpx


# Feishu OAuth v2 endpoint
TOKEN_URL = "https://open.feishu.cn/open-apis/authen/v2/oauth/token"

# 提前刷新缓冲:距过期 ≤ 这么多秒就主动 refresh,避免"调到一半 token 失效"
_REFRESH_BUFFER_SECONDS = 300


class TokenMissingError(Exception):
    """token 文件不存在或损坏 — 需要跑 CLI 完成首次授权"""


class TokenExpiredError(Exception):
    """refresh_token 也过期 — 30 天窗口已过,需要重新 CLI 授权"""


def _token_path() -> Path:
    """读 .env 里的 FEISHU_TOKEN_PATH,缺省用项目根 ./feishu_token.json"""
    val = os.getenv("FEISHU_TOKEN_PATH", "").strip()
    if val:
        return Path(val).expanduser()
    return Path(__file__).resolve().parent.parent / "feishu_token.json"


def _read_token_file() -> dict:
    path = _token_path()
    if not path.exists():
        raise TokenMissingError(f"token 文件不存在: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        raise TokenMissingError(f"token 文件无法解析: {path} — {type(e).__name__}: {e}") from e


def write_token_file(token_response: dict) -> Path:
    """
    把 OAuth /token endpoint 返回的 dict 落盘成稳定 schema。

    token_response 期望字段(飞书 v2 返回):
      access_token / refresh_token / expires_in / refresh_token_expires_in / scope
    """
    now = int(time.time())
    expires_in = int(token_response.get("expires_in", 7200))
    refresh_expires_in = int(token_response.get("refresh_token_expires_in", 2592000))  # 30 天
    persisted = {
        "access_token":         token_response["access_token"],
        "refresh_token":        token_response["refresh_token"],
        "expires_at":           now + expires_in,
        "refresh_expires_at":   now + refresh_expires_in,
        "obtained_at":          now,
        "scope":                token_response.get("scope", ""),
    }
    path = _token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(persisted, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _refresh(refresh_token: str) -> dict:
    """用 refresh_token 换新 access_token。失败抛 TokenExpiredError。"""
    app_id = os.getenv("FEISHU_APP_ID", "").strip()
    app_secret = os.getenv("FEISHU_APP_SECRET", "").strip()
    if not app_id or not app_secret:
        raise TokenExpiredError(".env 缺 FEISHU_APP_ID / FEISHU_APP_SECRET,无法 refresh")
    payload = {
        "grant_type":    "refresh_token",
        "client_id":     app_id,
        "client_secret": app_secret,
        "refresh_token": refresh_token,
    }
    try:
        resp = httpx.post(TOKEN_URL, json=payload, timeout=15)
        resp.raise_for_status()
    except httpx.HTTPError as e:
        raise TokenExpiredError(f"refresh 请求失败: {type(e).__name__}: {e}") from e
    data = resp.json()
    # 飞书 v2 在 body 里返业务错误码,即使 HTTP 200
    if data.get("code", 0) != 0:
        raise TokenExpiredError(f"refresh 业务错误: code={data.get('code')} msg={data.get('msg')}")
    return data


def get_valid_token() -> str:
    """
    返回当前可用的 access_token。
    必要时自动 refresh 并写回文件。
    """
    token = _read_token_file()
    now = int(time.time())

    # 距 access 过期 > buffer → 直接用
    if token["expires_at"] - now > _REFRESH_BUFFER_SECONDS:
        return token["access_token"]

    # 已过期,且 refresh_token 也过期 → 用户必须重跑 CLI
    if token["refresh_expires_at"] <= now:
        raise TokenExpiredError(
            f"refresh_token 已过期(refresh_expires_at={token['refresh_expires_at']}, now={now})"
        )

    # 走 refresh
    new_data = _refresh(token["refresh_token"])
    write_token_file(new_data)
    return new_data["access_token"]
```

- [ ] **Step 2:** Syntax check

```powershell
python -m py_compile lib/feishu_token_store.py
```
Expected: 无输出,exit 0。

- [ ] **Step 3:** Smoke test:无文件时报 TokenMissingError

```powershell
python -c "from lib.feishu_token_store import get_valid_token, TokenMissingError; import sys; try: get_valid_token(); print('UNEXPECTED OK')
except TokenMissingError as e: print(f'OK: {e}')"
```
Expected: `OK: token 文件不存在: ...feishu_token.json`

(如果你机器上已有 token 文件,这一步会拿到真 token 或走 refresh — 不算失败,只要不报别的异常都 OK)

- [ ] **Step 4:** Commit

```bash
git add lib/feishu_token_store.py
git commit -m "Feishu Minutes: token store with auto-refresh + 5min buffer"
```

---

### Task 3:`lib/feishu_auth_cli.py` — 一次性 OAuth CLI

**Files:**
- Create: `lib/feishu_auth_cli.py`

- [ ] **Step 1:** 创建文件,写入下面完整内容

```python
"""
飞书 OAuth 授权码流程 CLI。
跑法:python -m lib.feishu_auth_cli

流程:
  1. 起 http.server 监听 127.0.0.1:8765
  2. 浏览器打开飞书授权页
  3. 用户点同意 → 浏览器跳 /callback?code=...&state=...
  4. server 收到 code → POST /oauth/token 换 access_token + refresh_token
  5. token_store 落盘 → server.shutdown() → 退出

注意:Feishu 应用后台必须把重定向 URL 配成 http://localhost:8765/callback。
"""
from __future__ import annotations

import http.server
import os
import secrets
import sys
import urllib.parse
import webbrowser
from threading import Event

import httpx
from dotenv import load_dotenv

from lib.feishu_token_store import write_token_file, TOKEN_URL


# Feishu OAuth v2 authorize endpoint
# 实现时若文档有更新,改这里
_AUTHORIZE_URL = "https://accounts.feishu.cn/open-apis/authen/v1/authorize"

_REDIRECT_URI = "http://localhost:8765/callback"
_LISTEN_HOST = "127.0.0.1"
_LISTEN_PORT = 8765

# 用户报的 scope
_SCOPES = [
    "minutes:minutes.basic:read",
    "minutes:minutes.transcript:export",
]


class _AuthState:
    """跨 server 实例 + 主线程共享授权结果"""
    code: str | None = None
    state_expected: str = ""
    error: str | None = None
    done = Event()


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if not self.path.startswith("/callback"):
            self.send_response(404)
            self.end_headers()
            return
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        code = (qs.get("code") or [""])[0]
        state = (qs.get("state") or [""])[0]
        error = (qs.get("error") or [""])[0]
        if error:
            _AuthState.error = f"飞书返回 error: {error}"
        elif not code:
            _AuthState.error = "回调缺 code 参数"
        elif state != _AuthState.state_expected:
            _AuthState.error = f"state 不匹配(可能 CSRF):got={state} want={_AuthState.state_expected}"
        else:
            _AuthState.code = code

        # 给浏览器一个友好提示
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        if _AuthState.error:
            html = f"<h2>❌ 授权失败</h2><pre>{_AuthState.error}</pre>可关闭本页"
        else:
            html = "<h2>✅ 授权成功</h2><p>可关闭本页,回到终端看后续日志。</p>"
        self.wfile.write(html.encode("utf-8"))
        _AuthState.done.set()

    def log_message(self, fmt: str, *args) -> None:  # 静默 http.server 自带日志
        return


def main() -> int:
    load_dotenv()
    app_id = os.getenv("FEISHU_APP_ID", "").strip()
    app_secret = os.getenv("FEISHU_APP_SECRET", "").strip()
    if not app_id or not app_secret:
        print("ERROR: .env 缺 FEISHU_APP_ID / FEISHU_APP_SECRET", file=sys.stderr)
        return 2

    _AuthState.state_expected = secrets.token_urlsafe(16)

    # 拼授权 URL
    params = {
        "client_id":     app_id,
        "redirect_uri":  _REDIRECT_URI,
        "response_type": "code",
        "state":         _AuthState.state_expected,
        "scope":         " ".join(_SCOPES),
    }
    authorize_url = f"{_AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"

    # 起 server
    server = http.server.HTTPServer((_LISTEN_HOST, _LISTEN_PORT), _CallbackHandler)
    print(f"[Feishu Auth] 起本地回调监听 http://{_LISTEN_HOST}:{_LISTEN_PORT}/callback")
    print(f"[Feishu Auth] 打开浏览器 → 飞书登录页 ...")
    print(f"[Feishu Auth] 若浏览器没自动打开,手动访问: {authorize_url}")
    webbrowser.open(authorize_url)

    # serve until callback received(单线程同步即可)
    while not _AuthState.done.is_set():
        server.handle_request()

    server.server_close()

    if _AuthState.error or not _AuthState.code:
        print(f"[Feishu Auth] ❌ {_AuthState.error or '未拿到 code'}", file=sys.stderr)
        return 1

    print(f"[Feishu Auth] 收到 code,换取 access_token ...")

    # code → token
    payload = {
        "grant_type":    "authorization_code",
        "client_id":     app_id,
        "client_secret": app_secret,
        "code":          _AuthState.code,
        "redirect_uri":  _REDIRECT_URI,
    }
    try:
        resp = httpx.post(TOKEN_URL, json=payload, timeout=15)
        resp.raise_for_status()
    except httpx.HTTPError as e:
        print(f"[Feishu Auth] ❌ token 请求失败: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    data = resp.json()
    if data.get("code", 0) != 0:
        print(f"[Feishu Auth] ❌ 业务错误: code={data.get('code')} msg={data.get('msg')}", file=sys.stderr)
        return 1

    path = write_token_file(data)
    print(f"[Feishu Auth] ✅ token 已保存到 {path}")
    print(f"[Feishu Auth] access_token 有效期 ~ {data.get('expires_in', 7200)}s,"
          f"refresh_token ~ {data.get('refresh_token_expires_in', 2592000)}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2:** Syntax check

```powershell
python -m py_compile lib/feishu_auth_cli.py
```
Expected: 无输出。

- [ ] **Step 3:** Smoke test:.env 缺凭证时报错退出

```powershell
# 临时屏蔽 .env 让它走"凭证缺"分支
$env:FEISHU_APP_ID=""; $env:FEISHU_APP_SECRET=""; python -m lib.feishu_auth_cli
```
Expected: 输出 `ERROR: .env 缺 FEISHU_APP_ID / FEISHU_APP_SECRET`,exit code 2。

- [ ] **Step 4:** **手动 E2E 走一次完整授权**(需要你 user 提前在飞书后台:
  1. 创建/选定应用,记下 App ID 和 App Secret
  2. **重定向 URI** 配 `http://localhost:8765/callback`
  3. 权限里加 `minutes:minutes.basic:read` 和 `minutes:minutes.transcript:export`
  4. 应用发版 + 自审批
  5. 把凭证写进 `.env` 的 `FEISHU_APP_ID` / `FEISHU_APP_SECRET`)

```powershell
python -m lib.feishu_auth_cli
```
Expected:
- 浏览器自动开飞书登录页
- 你登录 + 点同意
- 浏览器跳"✅ 授权成功"页
- 终端输出"token 已保存到 ...feishu_token.json"
- `feishu_token.json` 文件出现在项目根

如果浏览器报授权 error:对照终端打印的 authorize_url,常见原因是 scope 名不对 / 重定向 URI 没在后台配 / 应用未发版。

- [ ] **Step 5:** Commit

```bash
git add lib/feishu_auth_cli.py
git commit -m "Feishu Minutes: CLI OAuth (python -m lib.feishu_auth_cli)"
```

---

**Phase A 完成判定**:`feishu_token.json` 文件存在且 JSON 结构含 `access_token` / `refresh_token` / `expires_at` / `refresh_expires_at` 四个字段。可以进 Phase B。

---

## Phase B — Fetcher 隔离测试

### Task 4:`lib/feishu_minutes_fetcher.py` 骨架 + URL 解析

**Files:**
- Create: `lib/feishu_minutes_fetcher.py`

- [ ] **Step 1:** 创建文件,写入下面骨架(API 调用先 stub,留下回填位置)

```python
"""
飞书妙记 URL → 转写文本 DOCX → 落 ARCHIVE_DIR

API 端点(实现时若文档有更新,改 _API_HOST 和下面常量):
  - GET  /open-apis/minutes/v1/minutes/{token}             : 基本信息
  - POST /open-apis/minutes/v1/minutes/{token}/transcript/export : 导出转写
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import re
from pathlib import Path

import httpx
from docx import Document

from lib.downloader import get_archive_dir
from lib.feishu_token_store import get_valid_token, TokenExpiredError, TokenMissingError


# Feishu 开放平台 API base
_API_HOST = "https://open.feishu.cn"

# 单次抓取总预算(从拿 token 到 DOCX 落盘),超过算失败
_TOTAL_TIMEOUT_SECONDS = 60

# 异步 export job 轮询参数
_EXPORT_POLL_INTERVAL = 1.0   # 秒
_EXPORT_POLL_MAX_TRIES = 30   # 最长 30 秒


class LinkFetchError(Exception):
    """飞书妙记抓取统一异常(handler 侧用 except 捕获)"""


# URL 形态:
#   https://siq00ly0hzc.feishu.cn/minutes/obcnhh8v75l36eze44fbx84y
#   https://*.feishu.cn/minutes/<token>[?query]
#   国际版: https://*.larksuite.com/minutes/<token>
_MINUTES_URL_RE = re.compile(
    r"https?://[^/]+/minutes/([A-Za-z0-9_-]+)", re.IGNORECASE
)


def extract_obj_token(url: str) -> str:
    """从妙记 URL 抠 obj_token。抠不到抛 LinkFetchError。"""
    m = _MINUTES_URL_RE.search(url or "")
    if not m:
        raise LinkFetchError(f"URL 不像妙记链接(缺 /minutes/<token>): {url}")
    return m.group(1)


def _sanitize_title(title: str, max_len: int = 60) -> str:
    """文件名安全化,与 wechat_mp_fetcher 一致"""
    if not title:
        return ""
    title = re.sub(r'[\\/*?:"<>|\n\r\t]', "", title)
    title = title.strip().replace(" ", "_")
    return title[:max_len]


def _build_filename(title: str) -> str:
    date_str = _dt.datetime.now().strftime("%Y-%m-%d")
    time_str = _dt.datetime.now().strftime("%H%M%S")
    safe = _sanitize_title(title)
    return f"{date_str}_{time_str}_{safe}.docx" if safe else f"{date_str}_{time_str}.docx"


def _format_ts(ms: int | None) -> str:
    """毫秒时间戳 → HH:MM:SS"""
    if not ms or ms < 0:
        return "00:00:00"
    s = ms // 1000
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


async def _api_get(client: httpx.AsyncClient, path: str, token: str) -> dict:
    """GET 飞书 API,统一错误处理。"""
    try:
        resp = await client.get(
            f"{_API_HOST}{path}",
            headers={"Authorization": f"Bearer {token}"},
        )
    except httpx.HTTPError as e:
        raise LinkFetchError(f"网络异常 GET {path}: {type(e).__name__}: {e}") from e
    if resp.status_code == 403:
        raise LinkFetchError("无权访问该妙记,请确认你是该会议参与者")
    if resp.status_code == 404:
        raise LinkFetchError("妙记不存在或已删除")
    if resp.status_code >= 400:
        raise LinkFetchError(f"API 错误 {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    if data.get("code", 0) != 0:
        raise LinkFetchError(f"业务错误: code={data.get('code')} msg={data.get('msg')}")
    return data.get("data") or {}


async def _api_post(client: httpx.AsyncClient, path: str, token: str, json_body: dict | None = None) -> dict:
    """POST 飞书 API,统一错误处理。"""
    try:
        resp = await client.post(
            f"{_API_HOST}{path}",
            headers={"Authorization": f"Bearer {token}"},
            json=json_body or {},
        )
    except httpx.HTTPError as e:
        raise LinkFetchError(f"网络异常 POST {path}: {type(e).__name__}: {e}") from e
    if resp.status_code == 403:
        raise LinkFetchError("无权访问该妙记,请确认你是该会议参与者")
    if resp.status_code == 404:
        raise LinkFetchError("妙记不存在或已删除")
    if resp.status_code >= 400:
        raise LinkFetchError(f"API 错误 {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    if data.get("code", 0) != 0:
        raise LinkFetchError(f"业务错误: code={data.get('code')} msg={data.get('msg')}")
    return data.get("data") or {}


async def _fetch_meeting_info(client: httpx.AsyncClient, token: str, obj_token: str) -> dict:
    """
    GET /open-apis/minutes/v1/minutes/{obj_token}
    返回 {title, ...}。
    """
    data = await _api_get(client, f"/open-apis/minutes/v1/minutes/{obj_token}", token)
    # 飞书 v1 API 通常嵌套在 .minute 下,具体字段以实现时文档为准
    minute = data.get("minute") or data
    return {
        "title": minute.get("topic") or minute.get("title") or "妙记",
        "raw":   minute,
    }


async def _fetch_transcript_paragraphs(client: httpx.AsyncClient, token: str, obj_token: str) -> list[dict]:
    """
    拿转写文本。返回 [{ts_ms, speaker, text}, ...]。

    飞书 transcript export 接口典型是异步 job:
      1. POST .../transcript/export → 拿到 task_id 或 file_token
      2. GET 轮询 → 拿到 file_url 或 file_token
      3. GET 文件内容(可能是纯文本 / JSON 字幕格式)

    实现要点:
    - 先 POST 看返回结构
    - 如果直接返了 transcript 内容,跳过 poll
    - 如果返 task_id,起 poll 循环,最长 _EXPORT_POLL_MAX_TRIES * _EXPORT_POLL_INTERVAL 秒
    - 如果返 file_url(下载 URL),httpx GET 拿内容
    - 解析成 [{ts_ms, speaker, text}, ...] 统一格式
    """
    # 第一次 POST,拿任务起手数据
    init = await _api_post(
        client,
        f"/open-apis/minutes/v1/minutes/{obj_token}/transcript/export",
        token,
        json_body={"format": "json"},  # 实现时按文档调整参数
    )

    # ── 情况 A:直接返了 paragraph 数组 ──
    if isinstance(init.get("paragraphs"), list):
        return [_normalize_paragraph(p) for p in init["paragraphs"]]

    # ── 情况 B:返了 task_id,需要 poll ──
    task_id = init.get("task_id") or init.get("export_task_id")
    if task_id:
        result = await _poll_export_task(client, token, obj_token, task_id)
        if isinstance(result.get("paragraphs"), list):
            return [_normalize_paragraph(p) for p in result["paragraphs"]]
        file_url = result.get("file_url") or result.get("download_url")
        if file_url:
            return await _download_and_parse_transcript(client, token, file_url)
        raise LinkFetchError(f"export 完成但未拿到 paragraphs 或 file_url: {result}")

    # ── 情况 C:直接返了 file_url ──
    file_url = init.get("file_url") or init.get("download_url")
    if file_url:
        return await _download_and_parse_transcript(client, token, file_url)

    raise LinkFetchError(f"export 接口返回结构非预期: {list(init.keys())}")


def _normalize_paragraph(p: dict) -> dict:
    """
    把飞书返回的单条记录归一为 {ts_ms, speaker, text}。
    字段名可能是 start_time / start_time_ms / time / speaker_name / content / text,
    实现时按真返回 schema 调整。
    """
    ts_ms = (
        p.get("start_time_ms")
        or p.get("start_time")
        or p.get("ts_ms")
        or p.get("time")
        or 0
    )
    speaker = p.get("speaker_name") or p.get("speaker") or p.get("user_name") or ""
    text = p.get("text") or p.get("content") or ""
    return {"ts_ms": int(ts_ms), "speaker": str(speaker).strip(), "text": str(text).strip()}


async def _poll_export_task(client: httpx.AsyncClient, token: str, obj_token: str, task_id: str) -> dict:
    """轮询导出任务,直到 status 为完成或超 max tries"""
    path = f"/open-apis/minutes/v1/minutes/{obj_token}/transcript/export/{task_id}"
    for i in range(_EXPORT_POLL_MAX_TRIES):
        data = await _api_get(client, path, token)
        status = (data.get("status") or "").lower()
        if status in ("done", "success", "finished", "completed"):
            return data
        if status in ("failed", "error", "cancelled"):
            raise LinkFetchError(f"export 任务失败: {data}")
        await asyncio.sleep(_EXPORT_POLL_INTERVAL)
    raise LinkFetchError(f"export 任务超时(>{_EXPORT_POLL_MAX_TRIES * _EXPORT_POLL_INTERVAL}s)")


async def _download_and_parse_transcript(client: httpx.AsyncClient, token: str, file_url: str) -> list[dict]:
    """
    GET file_url 拿到内容并解析成 paragraph list。
    可能是 JSON 字幕格式,或 SRT/VTT 纯文本 — 实现时按真实返回调整。
    """
    try:
        resp = await client.get(file_url, headers={"Authorization": f"Bearer {token}"})
        resp.raise_for_status()
    except httpx.HTTPError as e:
        raise LinkFetchError(f"下载转写文件失败: {type(e).__name__}: {e}") from e

    ctype = resp.headers.get("Content-Type", "")
    if "json" in ctype.lower():
        body = resp.json()
        paragraphs = body.get("paragraphs") or body.get("data") or []
        if isinstance(paragraphs, list):
            return [_normalize_paragraph(p) for p in paragraphs]
        raise LinkFetchError(f"JSON 转写格式非预期: {list(body.keys())}")

    # fallback:纯文本,每行作为一段,没 speaker / ts
    lines = [ln.strip() for ln in resp.text.splitlines() if ln.strip()]
    return [{"ts_ms": 0, "speaker": "", "text": ln} for ln in lines]


def _build_docx(meeting_title: str, paragraphs: list[dict], source_url: str, target_path: Path) -> int:
    """拼 DOCX。每段格式:[HH:MM:SS] 张三:文本"""
    doc = Document()
    if meeting_title:
        doc.add_heading(meeting_title, level=1)

    meta_lines = [
        "来源:飞书妙记",
        f"接收时间:{_dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"原始链接:{source_url}",
    ]
    doc.add_paragraph("\n".join(meta_lines))
    doc.add_paragraph("=" * 40)

    if not paragraphs:
        doc.add_paragraph("(转写为空)")
    for p in paragraphs:
        ts = _format_ts(p.get("ts_ms"))
        speaker = p.get("speaker") or ""
        text = p.get("text") or ""
        if not text:
            continue
        prefix = f"[{ts}] {speaker}:" if speaker else f"[{ts}]"
        doc.add_paragraph(f"{prefix} {text}")

    doc.save(str(target_path))
    return target_path.stat().st_size


async def fetch_and_save_as_docx(url: str, body: dict) -> tuple[Path, int, str]:
    """
    飞书妙记 URL → 转写 DOCX 落盘到 ARCHIVE_DIR。

    Args:
        url: feishu.cn/minutes/<token> 完整链接
        body: 完整 msgbody,目前未直接用,留参数对称(与 wechat_mp_fetcher 接口一致)

    Returns:
        (落盘路径, 字节数, 会议标题)

    Raises:
        TokenMissingError / TokenExpiredError: handler 侧专门捕获 → 提示用户重跑 CLI
        LinkFetchError: 其他抓取 / 解析 / 落盘失败
    """
    async def _do():
        obj_token = extract_obj_token(url)
        access_token = get_valid_token()  # 可能抛 TokenMissingError / TokenExpiredError

        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            info = await _fetch_meeting_info(client, access_token, obj_token)
            paragraphs = await _fetch_transcript_paragraphs(client, access_token, obj_token)

        title = info.get("title") or "妙记"
        archive_dir = get_archive_dir()
        archive_dir.mkdir(parents=True, exist_ok=True)
        path = archive_dir / _build_filename(title)
        if path.exists():
            for i in range(1, 100):
                cand = path.with_suffix(f".{i}{path.suffix}")
                if not cand.exists():
                    path = cand
                    break
        size = _build_docx(title, paragraphs, url, path)
        return path, size, title

    try:
        return await asyncio.wait_for(_do(), timeout=_TOTAL_TIMEOUT_SECONDS)
    except asyncio.TimeoutError as e:
        raise LinkFetchError(f"总耗时超过 {_TOTAL_TIMEOUT_SECONDS}s,放弃: {url}") from e
```

- [ ] **Step 2:** Syntax check

```powershell
python -m py_compile lib/feishu_minutes_fetcher.py
```
Expected: 无输出。

- [ ] **Step 3:** Smoke test `extract_obj_token` 纯函数

```powershell
python -c "from lib.feishu_minutes_fetcher import extract_obj_token; print(extract_obj_token('https://siq00ly0hzc.feishu.cn/minutes/obcnhh8v75l36eze44fbx84y'))"
```
Expected: `obcnhh8v75l36eze44fbx84y`

```powershell
python -c "from lib.feishu_minutes_fetcher import extract_obj_token, LinkFetchError; try: extract_obj_token('https://example.com/foo'); print('UNEXPECTED OK')
except LinkFetchError as e: print(f'OK: {e}')"
```
Expected: `OK: URL 不像妙记链接(缺 /minutes/<token>): https://example.com/foo`

- [ ] **Step 4:** Commit

```bash
git add lib/feishu_minutes_fetcher.py
git commit -m "Feishu Minutes: fetcher skeleton with URL parsing + API helpers"
```

---

### Task 5:E2E 隔离测试 fetcher(无 bot)

**Files:**
- (临时调试,无新文件 — 跑命令即可)

- [ ] **Step 1:** **手动 E2E**:用一条你确定有权访问的真实妙记 URL 跑 fetcher

```powershell
python -c "import asyncio; from lib.feishu_minutes_fetcher import fetch_and_save_as_docx; from dotenv import load_dotenv; load_dotenv(); path, size, title = asyncio.run(fetch_and_save_as_docx('https://siq00ly0hzc.feishu.cn/minutes/<你的真实 token>', {})); print(f'OK: {path} ({size} bytes) title={title}')"
```

Expected: 几秒~几十秒后输出 `OK: <ARCHIVE_DIR>/2026-05-28_HHMMSS_<标题>.docx (... bytes) title=...`,且文件实际在 ARCHIVE_DIR 里。

- [ ] **Step 2:** **如果失败,按下面 troubleshooting 表查 API 路径 / 字段名**:

| 报错 | 排查 |
|---|---|
| `API 错误 404` 或 `妙记不存在或已删除` 但实际能在网页打开 | API 路径不对:实测飞书 minutes API 路径可能跟我假设的不同。打开 [open.feishu.cn](https://open.feishu.cn) 文档 → 搜"妙记",对照 `_fetch_meeting_info` 和 `_fetch_transcript_paragraphs` 里的 path,更新 |
| `业务错误: code=99991663` 或类似 token 错误 | scope 不对 / 应用未发版,回 Feishu 后台检查 |
| `export 接口返回结构非预期: ['xxx']` | export 接口真实返回结构跟假设的三种情况都不一样。把打印加详细 `print(init)` 看实际字段,调整 `_fetch_transcript_paragraphs` 的分支 |
| DOCX 里转写文本是空 | `_normalize_paragraph` 字段名错。打印一条 paragraph 看真字段名,改 `_normalize_paragraph` |
| 一直 polling 超时 | poll endpoint 路径或 status 字段值不对,看 `_poll_export_task` |

- [ ] **Step 3:** **调试期间用 `print()` 把原始 API response 打出来确认 schema**。例:在 `_fetch_meeting_info` 第一行加 `print('[debug] meeting raw:', minute)`,在 `_fetch_transcript_paragraphs` 拿到 `init` 后加 `print('[debug] export init:', init)`。**确认 schema 后把 print 删掉再 commit**。

- [ ] **Step 4:** **改完后再跑一次 Step 1**,直到拿到正确的 DOCX。打开 DOCX 人工核对:标题对、发言段格式合理。

- [ ] **Step 5:** Commit(把过程中改对的 API path / 字段名固化下来)

```bash
git add lib/feishu_minutes_fetcher.py
git commit -m "Feishu Minutes: fix API path/schema based on real response"
```

(若 Step 1 一次就过,这步可跳过)

---

### Task 6:Refresh 路径 smoke test

**Files:** (无文件改动,纯验证)

- [ ] **Step 1:** 手动把 `feishu_token.json` 里的 `expires_at` 改成过去时间(比如减一万),保存

```powershell
$tokenPath = "feishu_token.json"
$j = Get-Content $tokenPath -Raw | ConvertFrom-Json
$j.expires_at = $j.expires_at - 10000
$j | ConvertTo-Json | Set-Content $tokenPath -Encoding UTF8
Get-Content $tokenPath
```
Expected: 文件里 `expires_at` 变成过去时间。

- [ ] **Step 2:** 跑一次拉转写,应该自动 refresh + 写回 token

```powershell
python -c "import asyncio; from lib.feishu_minutes_fetcher import fetch_and_save_as_docx; from dotenv import load_dotenv; load_dotenv(); path, size, title = asyncio.run(fetch_and_save_as_docx('https://siq00ly0hzc.feishu.cn/minutes/<你的真实 token>', {})); print(f'OK: {path}')"
```
Expected: 成功落 DOCX,且 `feishu_token.json` 的 `expires_at` 现在是未来时间(说明 refresh 跑过且写回)。

- [ ] **Step 3:** 测试"refresh_token 也过期"路径:把 `refresh_expires_at` 也改成过去,再跑

```powershell
$j = Get-Content $tokenPath -Raw | ConvertFrom-Json
$j.expires_at = $j.expires_at - 1000000
$j.refresh_expires_at = [int][double]::Parse((Get-Date -UFormat %s)) - 1000
$j | ConvertTo-Json | Set-Content $tokenPath -Encoding UTF8

python -c "import asyncio; from lib.feishu_minutes_fetcher import fetch_and_save_as_docx; from lib.feishu_token_store import TokenExpiredError; from dotenv import load_dotenv; load_dotenv(); try: asyncio.run(fetch_and_save_as_docx('https://siq00ly0hzc.feishu.cn/minutes/xxx', {})); print('UNEXPECTED OK')
except TokenExpiredError as e: print(f'OK refresh_expired: {e}')"
```
Expected: 输出 `OK refresh_expired: refresh_token 已过期...`

- [ ] **Step 4:** **重跑 CLI 拿一个新 token**,准备进 Phase C

```powershell
python -m lib.feishu_auth_cli
```
Expected: 浏览器走完授权,新 token 落盘。

(不需要 commit 这步,只是验证状态)

---

**Phase B 完成判定**:fetcher 能在隔离命令行里成功落 DOCX,refresh / refresh-expired 两条路径都按预期。可以进 Phase C。

---

## Phase C — 接入 bot

### Task 7:URL 分类 + source label + tag whitelist

**Files:**
- Modify: `lib/url_detect.py`
- Modify: `lib/tag_parser.py`

- [ ] **Step 1:** `lib/url_detect.py` 的 `DOMAIN_MAP` 在 `("doc.weixin.qq.com", "tencent_wedoc")` **之后**追加飞书条目

```python
DOMAIN_MAP = [
    ("mp.weixin.qq.com",     "wechat_mp"),       # 微信公众号文章
    ("share.note.youdao.com", "youdao_note"),    # 有道云笔记(分享链接)
    ("note.youdao.com",      "youdao_note"),     # 有道云笔记(其他形式)
    ("docs.qq.com",          "tencent_docs"),    # 腾讯文档
    ("doc.weixin.qq.com",    "tencent_wedoc"),   # 微信文档(腾讯文档内嵌微信版)
    ("feishu.cn",            "feishu_minutes"),  # 飞书妙记(国内)
    ("larksuite.com",        "feishu_minutes"),  # 飞书妙记(国际版)
]
```

注意:`("feishu.cn", ...)` 匹配规则是"host 完全等于或以 `.feishu.cn` 结尾",所以 `*.feishu.cn` 任意子域都会命中(用户自己的 org 子域如 `siq00ly0hzc.feishu.cn` 就走这条)。

- [ ] **Step 2:** 同文件的 `SOURCE_LABEL` 加 `feishu_minutes`

```python
SOURCE_LABEL = {
    "wechat_mp":      "公众号文章",
    "youdao_note":    "有道云笔记",
    "tencent_docs":   "腾讯文档",
    "tencent_wedoc":  "微信文档",
    "feishu_minutes": "飞书妙记",
    "web_other":      "网页链接",
}
```

- [ ] **Step 3:** `lib/tag_parser.py` 的 `SOURCE_WHITELIST` 末尾(`"海豚研究"` 之后)加 `"飞书妙记"`

```python
SOURCE_WHITELIST: list[str] = [
    "Acecamp",
    "Thirdbridge",
    "AlphaEngine",
    "公众号",
    "Citi", "UBS", "GS", "MS", "JPM", "CICC", "CLSA",
    "Macquarie", "Barclays", "BofA", "HSBC", "Nomura",
    "Jefferies", "Deutsche", "Bernstein", "Daiwa",
    "海豚研究",
    "飞书妙记",
]
```

- [ ] **Step 4:** Smoke test:`classify_url`

```powershell
python -c "from lib.url_detect import classify_url; print(classify_url('https://siq00ly0hzc.feishu.cn/minutes/obcnhh8v75l36eze44fbx84y'))"
```
Expected: `feishu_minutes`

```powershell
python -c "from lib.url_detect import classify_url; print(classify_url('https://example.larksuite.com/minutes/abc'))"
```
Expected: `feishu_minutes`

```powershell
python -c "from lib.tag_parser import parse_tag; print(parse_tag('卖方汇报 阿里 飞书妙记 Q1点评'))"
```
Expected: 输出含 `'source': '飞书妙记'`

- [ ] **Step 5:** Commit

```bash
git add lib/url_detect.py lib/tag_parser.py
git commit -m "Feishu Minutes: URL detection + source whitelist"
```

---

### Task 8:`handlers/text.py` 接入 feishu_minutes 分支

**Files:**
- Modify: `handlers/text.py`

- [ ] **Step 1:** 文件顶部 imports 区,在现有 `from lib.generic_web_fetcher import (...)` 之后追加

```python
from lib.feishu_minutes_fetcher import (
    LinkFetchError as FeishuLinkFetchError,
    fetch_and_save_as_docx as feishu_fetch_and_save,
)
from lib.feishu_token_store import TokenExpiredError, TokenMissingError
```

- [ ] **Step 2:** `handle()` 的 URL 分支,在 `if source_type == "wechat_mp":` 之后、`if source_type == "web_other":` 之前,插入新分支

```python
    if source_type == "wechat_mp":
        await _handle_wechat_mp(body, url, user_title_hint, label)
        return

    if source_type == "feishu_minutes":
        await _handle_feishu_minutes(body, url, user_title_hint, label)
        return

    if source_type == "web_other":
        await _handle_generic_web(body, url, user_title_hint, label)
        return
```

- [ ] **Step 3:** 在文件底部 `_handle_long_text_note` 之后追加新 handler 函数

```python
# ────────────────────────────────────────────────────────────
#  飞书妙记抓取
# ────────────────────────────────────────────────────────────
async def _handle_feishu_minutes(body: dict, url: str, user_title_hint: str | None, label: str) -> None:
    """
    飞书妙记 URL:调 Feishu API 拿转写 → DOCX 落盘 → 一次性回执。

    Token 缺 / 过期专门提示用户重跑 CLI。
    """
    print(f"[text handler / feishu_minutes] 开始抓取 {url} ...")

    try:
        path, size, fetched_title = await feishu_fetch_and_save(url, body)
    except TokenMissingError:
        await reply_markdown(
            body,
            "⚠️ **尚未授权飞书**\n\n"
            "请在 bot 所在机器上运行:\n\n"
            "`python -m lib.feishu_auth_cli`\n\n"
            "登录授权后再发该 URL。",
        )
        return
    except TokenExpiredError as e:
        await reply_markdown(
            body,
            "⚠️ **飞书授权过期**(refresh_token 30 天窗口已过)\n\n"
            "请在 bot 所在机器上重新运行:\n\n"
            "`python -m lib.feishu_auth_cli`\n\n"
            f"细节: `{e}`",
        )
        return
    except FeishuLinkFetchError as e:
        print(f"[text handler / feishu_minutes] 抓取失败: {e}")
        await reply_markdown(body, f"❌ 飞书妙记抓取失败\n\n`{e}`")
        return
    except Exception as e:
        print(f"[text handler / feishu_minutes] 未预期异常: {type(e).__name__}: {e}")
        await reply_markdown(body, f"❌ 飞书妙记抓取失败\n\n`{type(e).__name__}: {e}`")
        return

    userid = (body.get("from") or {}).get("userid", "")
    register_pending(userid, path, source_hint="飞书妙记")
    print(f"[text handler / feishu_minutes] 已保存 {path} ({size} bytes), pending tag for userid={userid}")

    window_min = get_window_seconds() // 60
    reply_lines = [f"✅ 已抓取并保存 **飞书妙记**"]
    if fetched_title:
        reply_lines.append(f"标题: {fetched_title}")
    elif user_title_hint:
        reply_lines.append(f"标题: {user_title_hint}")
    reply_lines.append(f"文件: `{path.name}` ({humanize_size(size)})")
    reply_lines.append(f"💡 {window_min} 分钟内发 tag(含 type 关键词)可重命名")
    await reply_markdown(body, "\n\n".join(reply_lines))
```

- [ ] **Step 4:** 文件顶部 docstring 的"4. 链接分享"那条括注里把"通用网页"那块加上"飞书妙记":

打开 [handlers/text.py](handlers/text.py),找到 docstring 里 `4. **链接分享**(URL 出现在 content 里,公众号 / 通用网页走抓取,其他类型回执 stub)`,改成:

```python
  4. **链接分享**(URL 出现在 content 里,公众号 / 飞书妙记 / 通用网页走抓取,其他类型回执 stub)
```

- [ ] **Step 5:** Syntax check

```powershell
python -m py_compile handlers/text.py
```
Expected: 无输出。

- [ ] **Step 6:** Commit

```bash
git add handlers/text.py
git commit -m "Feishu Minutes: wire handler branch (+ token-missing/expired hints)"
```

---

### Task 9:微信端 E2E

**Files:** (无文件改动,纯人工验证)

- [ ] **Step 1:** 启动 bot

```powershell
python main.py
```
Expected: 看到 `[订阅响应] {"errcode":0,...}`。

- [ ] **Step 2:** **微信里复制一条飞书妙记 URL 发给企微机器人**

预期 bot 回执:
```
✅ 已抓取并保存 飞书妙记
标题: <真实会议标题>
文件: `2026-05-28_HHMMSS_<标题>.docx` (N KB)
💡 5 分钟内发 tag(含 type 关键词)可重命名
```

打开 ARCHIVE_DIR 里的 DOCX 核对内容。

- [ ] **Step 3:** **测试 token 缺失提示**(可选):暂时改名 `feishu_token.json` → `feishu_token.json.bak`,bot 不重启,再发一条飞书 URL

预期 bot 回执:`⚠️ **尚未授权飞书** ...`

完事改名回来。

- [ ] **Step 4:** **测试 tag 流程衔接**:抓完一条后 5 分钟内发条短 tag,如 `公司交流 阿里 飞书妙记 Q1`

预期文件被重命名成 `2026-05-28_公司交流_阿里_飞书妙记_Q1.docx`(`飞书妙记` 既是 source_hint 也是白名单内,所以会进入 source 槽位)。

- [ ] **Step 5:** 如果 Step 2 之前没遇到问题、Step 4 也对,**无需新 commit**;有任何边界 bug 修了之后:

```bash
git add <修改的文件>
git commit -m "Feishu Minutes: E2E fixes from real WeChat test"
```

---

## 完工判定

Phase C 结束 + 微信端拉一次真实妙记成功落 DOCX + tag 重命名生效 = 全部完成。

---

## 不要做的事(YAGNI)

- 不写 pytest 测试(项目无此基础设施,与既有 wechat_mp_fetcher / generic_web_fetcher 一致用手测)
- 不引入 OAuth lib(authlib / requests-oauthlib):手写 OAuth code+refresh 流程已够,新增依赖徒增 deploy 负担
- 不做 token 加密(本机使用,文件权限够用)
- 不抓 AI 摘要 / 元信息(spec 已明确只要转写)
- 不做多用户 token 隔离(单 bot 单 user)
- 不持久化 pending(本项目其他 handler 也没做,沿用现状)
