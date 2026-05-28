# 飞书妙记自动抓取 — 设计文档

**日期**:2026-05-28
**状态**:Draft → 待 user 审核 → 进入 writing-plans

## 目标

用户在微信里转发(复制粘贴)一条飞书妙记 URL(例 `https://*.feishu.cn/minutes/<obj_token>`)进 bot,bot 调用飞书开放平台 API 拉取**录音转写正文**,落 DOCX 到 `ARCHIVE_DIR`,然后走现有 pending tag 流程允许后续重命名。

## 非目标

- 不抓 AI 摘要 / 关键词 / 会议元信息(用户当前只需要转写)
- 不支持自动续期超过 30 天的 refresh_token(refresh_token 过期则用户重新跑授权 CLI)
- 不支持多用户(单 bot 单 user 场景)
- 不引入持久数据库(token 用 JSON 文件即可)

## 角色边界

跟项目整体角色一致:**只负责把转写文字落成 DOCX 到 ARCHIVE_DIR**,不做正文清洗、不做下游归类。下游 invest-kb / MeetingMinutes pipeline 接管。

## 用户走查(happy path)

```
[一次性,每 30 天一次]
$ python -m feishu_auth_cli
[Feishu Auth] 起本地回调监听 http://localhost:8765
[Feishu Auth] 打开浏览器 → 飞书登录页 ...
                  ↓
[用户在浏览器里登录 + 授权 Solis 应用]
                  ↓
[Feishu Auth] 收到 code → 换取 user_access_token ... OK
[Feishu Auth] 保存到 ./feishu_token.json
[Feishu Auth] 完成 ✅

[日常使用]
微信里复制粘贴飞书妙记 URL → bot
bot:✅ 已抓取并保存 妙记
     文件: `2026-05-28_143020_Q1业绩复盘会议.docx`
     💡 5 分钟内发 tag(含 type 关键词)可重命名

[token 过期 30 天后]
bot:⚠️ 飞书授权过期,请运行 `python -m feishu_auth_cli` 重新授权
```

## 组件拆分

| 文件 | 职责 | 依赖 |
|---|---|---|
| `lib/feishu_token_store.py` | 读 / 写 / 自动 refresh token JSON;暴露 `get_valid_token()` | httpx, .env |
| `lib/feishu_auth_cli.py` | CLI 入口(`python -m feishu_auth_cli`):本地 HTTP server + 浏览器 → 拿 code → 换 token → 存盘 | http.server(标准库), webbrowser(标准库), httpx |
| `lib/feishu_minutes_fetcher.py` | URL → 调 Minutes API → 拼 DOCX → 落盘,返回 (path, size, title) | httpx, python-docx, feishu_token_store |
| `lib/url_detect.py`(修改) | `DOMAIN_MAP` 加 `feishu.cn` / `larksuite.com` → `feishu_minutes`;`SOURCE_LABEL` 加 `"feishu_minutes": "飞书妙记"` | — |
| `handlers/text.py`(修改) | 新 `_handle_feishu_minutes` 分支,照搬 `_handle_wechat_mp` 模式 | feishu_minutes_fetcher |
| `lib/tag_parser.py`(修改) | `SOURCE_WHITELIST` 加 `"飞书妙记"` | — |
| `.env.example`(修改) | 加 `FEISHU_APP_ID` / `FEISHU_APP_SECRET` / `FEISHU_TOKEN_PATH` 三行注释说明 | — |
| `.gitignore`(修改) | 加 `feishu_token.json` | — |

不引入新 deps:httpx / python-docx 已有。OAuth 回调用标准库 `http.server`。

## 数据流

### A. 一次性授权(CLI)

```
1. 读 .env:FEISHU_APP_ID / FEISHU_APP_SECRET
   缺则报错退出
2. 起 http.server 监听 127.0.0.1:8765,handler 处理 /callback
3. 拼 auth URL,webbrowser.open() 打开
   URL: https://passport.feishu.cn/suite/passport/oauth/authorize?
        client_id={app_id}&redirect_uri=http://localhost:8765/callback&response_type=code&state={随机}&scope=...
4. 用户登录 + 授权后浏览器跳 /callback?code=XXX&state=YYY
   server 收到 → 校验 state → 拿到 code
5. POST https://open.feishu.cn/open-apis/authen/v2/oauth/token
   body: {grant_type: authorization_code, code, client_id, client_secret, redirect_uri}
   → 拿到 access_token / refresh_token / expires_in / refresh_token_expires_in
6. 计算 absolute 过期时间(now + expires_in / now + refresh_token_expires_in)
7. 写 ./feishu_token.json
8. server.shutdown() + 提示完成 → 退出
```

### B. 运行时抓取

```
微信文本里有 feishu URL
       ↓
handlers/text.py::handle (3. 无 URL 分支前的 4. 链接分支)
       ↓
classify_url() → "feishu_minutes"
       ↓
_handle_feishu_minutes(body, url, user_title_hint, label)
       ↓
fetch_and_save_as_docx(url, body):
       ├─ from_url → 抠出 obj_token (URL 倒数最后一段)
       ├─ token = feishu_token_store.get_valid_token()
       │    ├─ 文件不存在 → TokenMissingError
       │    ├─ access 5 min 内过期 + refresh 有效 → 自动 refresh,写回文件
       │    └─ refresh 已过期 → TokenExpiredError
       │
       ├─ GET /open-apis/minutes/v1/minutes/{obj_token}
       │    Authorization: Bearer {access_token}
       │    → 拿 meeting_title / created_time
       │
       ├─ POST /open-apis/minutes/v1/minutes/{obj_token}/transcript/export
       │    (确认接口路径与是否异步;若异步则 poll task_id 直到 file_url 就绪)
       │    → 拿到转写内容(JSON 结构含 speaker / start_time_ms / text)
       │
       ├─ 拼 DOCX:
       │    H1: meeting_title
       │    metadata 块:来源 / 接收时间 / 原 URL
       │    分隔符
       │    每个发言段一行:`[HH:MM:SS] 张三:文本内容`
       │
       ├─ 落盘 ARCHIVE_DIR/{date}_{time}_{meeting_title_safe}.docx
       │    title 经 _sanitize_title (复用 text_note_saver 的) 截到 60 字
       │
       └─ 返回 (path, size, meeting_title)
       ↓
register_pending(userid, path, source_hint="飞书妙记")
       ↓
回执:✅ 已抓取并保存 妙记 / 文件名 / 5 分钟内可发 tag
```

### C. Token refresh 时机

`get_valid_token()` 实现:

```python
def get_valid_token() -> str:
    token = _read_token_file()  # 缺失 → raise TokenMissingError
    now = time.time()
    # 距 access 过期 ≤ 300 秒就提前刷
    if token["expires_at"] - now > 300:
        return token["access_token"]
    # refresh_token 也过期了
    if token["refresh_expires_at"] <= now:
        raise TokenExpiredError("refresh_token 也过期,需重新跑 CLI")
    # 走 refresh
    new_token = _refresh(token["refresh_token"])
    _write_token_file(new_token)
    return new_token["access_token"]
```

5 分钟缓冲避免"调到一半 token 刚好失效"。

## 关键决策

### 1. CLI 独立 vs Bot 内集成 OAuth server
**决策**:独立 CLI(`python -m feishu_auth_cli`)。

**理由**:bot 主进程是 websockets 长连接 + asyncio,加 OAuth server 需要额外的 aiohttp/uvicorn 依赖且不复用;CLI 用标准库 `http.server` 跑完即退,职责清晰。30 天频次也不需要无感体验。

### 2. Token 存项目根 vs 用户 home
**决策**:项目根 `./feishu_token.json`(gitignored)。

**理由**:跟 `.env` 同级,排错容易;单 bot 单 user 场景不需要跨项目共享。可通过 `FEISHU_TOKEN_PATH` 覆盖(后期想挪 ~/ 也能挪)。

### 3. 文件名带会议标题
**决策**:`{date}_{time}_{meeting_title_safe}.docx`,标题 sanitize 后截到 60 字。

**理由**:原文件名带语义信息,后续 tag fallback 链(`build_renamed_filename`)能直接用上。一致于公众号 / 网页抓取的命名风格。

### 4. source_hint = "飞书妙记" 加白名单
**决策**:加进 `SOURCE_WHITELIST`(`lib/tag_parser.py`)。

**理由**:`source_hint` 触发 `build_renamed_filename` 时填到 source 槽位需要 canonical 名(白名单内才视为有效)。加进去后用户也能在 tag 文字里直接写"飞书妙记"。

### 5. 转写格式
**决策**:每个发言段一段:`[HH:MM:SS] 张三:文本内容`。

**理由**:符合"录音转写"的直观期望;时间戳便于回放原录音对齐。如果飞书 API 没返回 speaker(纯 ASR 模式)就降级为 `[HH:MM:SS] 文本内容`。

## 错误处理

| 场景 | 异常类型 | 用户回执 |
|---|---|---|
| `.env` 缺 APP_ID/SECRET(CLI 启动时) | sys.exit + stderr | "请先在 .env 配置 FEISHU_APP_ID / FEISHU_APP_SECRET" |
| token 文件不存在(运行时) | `TokenMissingError` | "尚未授权飞书,请先运行 `python -m feishu_auth_cli`" |
| refresh_token 过期 | `TokenExpiredError` | "飞书授权过期,请运行 `python -m feishu_auth_cli` 重新授权" |
| Minutes API 403 | `LinkFetchError` | "无权访问该妙记,请确认你是该会议参与者" |
| Minutes API 404 | `LinkFetchError` | "妙记不存在或已删除" |
| Export 接口超时 / 任务失败 | `LinkFetchError` | 异常类型 + 消息 |
| 网络抖动 | `httpx.HTTPError` 上抛 | "网络异常: {类型}: {消息}" |

复用现有 `LinkFetchError`(来自 wechat_mp_fetcher 模式)以保 handler 那边的 try/except 简洁。

## 不确定项(实现时确认)

- **`/transcript/export` 接口的精确路径和同步/异步语义** — 飞书 API 的 "export" 类接口通常是异步 job(POST 返 task_id,GET 轮询直到 file_url ready)。打开开发者文档确认,如果是异步要加 poll 循环(最多 30 秒、500ms 间隔)
- **是否支持纯转写文本 endpoint 而非 export 文件** — 如果有"发言列表"风格的实时 API(直接返结构化 JSON),优先用它,避开 export job 的复杂度。这点开权限后能在文档里确认
- **scope 字符串的具体写法** — 用户报的是 `minutes:minutes.basic:read` / `minutes:minutes.transcript:export` / `minutes:minutes:readonly`。authorize URL 的 scope 参数用空格还是逗号分隔,以飞书文档为准

实现时这些一查即明,先按"最有可能"的路径写,跑通再说。

## 测试 / 验证

项目无单元测试基础设施,沿用现有的手测路径:

1. **CLI 流程**:跑 `python -m feishu_auth_cli` → 浏览器跳转 → 授权 → 看 token 文件落盘 + 字段齐全
2. **抓取主流程**:微信发一条已知妙记 URL → bot 落 DOCX → 打开核对转写内容
3. **Refresh 路径**:手动改 token 文件的 `expires_at` 为过去时间 → 触发抓取 → 看是否自动 refresh + 写回
4. **过期路径**:同上,把 `refresh_expires_at` 也改成过去时间 → 看是否拿到正确错误回执
5. **错误路径**:贴不存在的 obj_token → 看 404 回执

## Out of scope(后续可补)

- AI 摘要 / 会议元信息(用户当前不要)
- 多用户 token 隔离(本项目单 user)
- token 加密存储(本机使用,文件权限够用)
- Webhook 监听妙记新增(被动接 URL 已够)
- 国际版 `larksuite.com` 的 API endpoint 差异(主域名匹配先做,真用到再适配)
