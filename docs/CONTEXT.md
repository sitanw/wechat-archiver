# 微信投研归档助手 — 项目上下文

## 项目目标
搭建一个微信 → 企微 → 本地知识库的自动归档机器人:个人微信看到研究材料(公众号文章 / PDF / Word / 图片 / 文本传闻),转发给企微智能机器人,机器人下载、解密、落盘到指定目录,**由用户已有的下游脚本继续处理**(本项目不负责正文提取 / 洗数据 / 元数据补全)。

## 角色边界(很重要)
**本项目只做**:
- 接住企微推过来的消息
- 解密多媒体 / 抓取链接正文
- 把得到的 PDF / Word / 图片这三类文件**原封不动落到 `ARCHIVE_DIR`**

**不做**:
- PDF / Word 的正文提取
- 图片 OCR
- 元数据(公司、类型、period)的自动归类
- 写到任何索引 / 数据库

下游的 invest-kb / MeetingMinutes pipeline 会从 `ARCHIVE_DIR` 读文件继续处理。

## 当前状态(滚动更新)

### ✅ 已完成
- **长连接 + 消息 router 框架**:`wss://openws.work.weixin.qq.com` 鉴权 + 30 秒心跳 + 自动重连;`handle_frame` 按 `msgtype` 分流到各 handler;未知 msgtype 走 `handle_unknown` 兜底,所有消息 body 落盘到 `samples/{msgtype}/{msgid}.json`
- **text handler 含 URL 子分流**:文本内含 URL 时按域名识别公众号 / 有道云笔记 / 腾讯文档 / 微信文档 / 普通网页,抠出【...】前缀作为标题
- **file / image handler 含解密 + 落盘**:从 COS 下载密文 → AES-256-CBC + 宽容 PKCS#7 unpad → magic bytes 识别扩展名 → 落盘到 `ARCHIVE_DIR`(默认 `inbox/`),文件名 `{YYYYMMDD}_{HHMMSS}.{ext}`
- **`ARCHIVE_DIR` 通过 `.env` 可配**,默认 `inbox/`,实测目标:`D:\VibeCodingProjects\2.1_Project_MeetingMinutes\data\02b_external_notes`

### ✅ 最近完成
- **公众号链接抓取 → DOCX**:async Playwright 抓 `mp.weixin.qq.com` 文章正文 → DOCX 落盘。默认不带图(`INCLUDE_IMAGES=true` 可开),元信息含公众号名 / 发布时间 / 原文 URL
- **长文本笔记落盘**:无 URL 的纯文本超过 `NOTE_MIN_LENGTH`(默认 200 字符)自动落 DOCX
- **Tag 配对**:任何归档落盘后 `TAG_WINDOW_SECONDS`(默认 5 分钟)内发的短文字自动追加到文件名,实现"文件 + 后续 tag → 完整命名"工作流

### 后续规划
- 腾讯文档 / 有道云笔记 / 网盘类:**半自动或手动**(用户已表态可接受),不强求自动化
- always-on 部署(VPS / 本机服务化)— 等用户验证 POC 价值后再做

## 凭证位置
`.env` 文件(已配置,不进 git):
- `WECOM_BOT_ID` / `WECOM_BOT_SECRET` —— 企微智能机器人长连接凭证
- `ARCHIVE_DIR` —— 多媒体落盘目录(留空则用项目根的 `inbox/`)

## 技术栈
- Python 3.9+(注意:`from __future__ import annotations` 已加,避免 3.10 才支持的 `X | None` 写法炸)
- `websockets` 长连接,`httpx` HTTP,`python-dotenv` 配置
- `pycryptodome` AES 解密
- `playwright` + `python-docx` (公众号抓取,待加)

**不引入 Node.js**——企微官方 SDK 是 Node 的,但我们用 websockets 库自己直连更干净,和上下游 Python 生态对齐。

## 关联项目
- **invest-kb**(独立项目):有 `02b_format_external.py` 把外部材料洗成 wiki 结构。本项目是它的"前端"——把微信转发的素材送进 02b 的 input 目录
- **MeetingMinutes**(实测 `ARCHIVE_DIR` 指向的项目):`D:\VibeCodingProjects\2.1_Project_MeetingMinutes\data\02b_external_notes` 下游接管处理
- **AceCamp scraper**(同用户的另一项目):Playwright 抓取页面 → DOCX 的模板代码,公众号 link 抓取**借鉴其异步 Playwright + DOCX 构造模式**

## 企微 AI bot 平台已踩过的坑(给后来者)

### 富 share 卡片完全收不到
**所有富 share 卡片在企微平台层就被过滤**,包括:
- 公众号文章卡片(标题 + 缩略图 + 来源)
- 腾讯文档 / 微信文档卡片
- 微信"笔记"卡片
- 估计所有自带预览缩略图的卡片都一样

它们不会通过 `aibot_msg_callback` 推到我们的 WebSocket。用户那侧会看到平台默认的"抱歉,目前不支持理解此类型消息"回复——**那不是我们代码发的**。

绕过方法:
- 链接类:**复制链接 → 粘贴发送**,以 `msgtype: text` 到达
- 笔记 / 纪要类:**复制全文 → 粘贴发送**,长文本超过 NOTE_MIN_LENGTH 自动落盘为 DOCX

### body 不带原始文件名(但 COS 响应头带)
file / image 消息 body 只有 `url` 和 `aeskey`,**没有 filename / mimetype / size**。

**但**:COS 下载响应里的 **HTTP `Content-Disposition` header 带了原始文件名**(2026-05-16 实测确认)。格式:
```
attachment; filename=2026-05-07_Acecamp_%E4%B8%93%E5%AE%B6...docx
```
注意是 **URL percent-encoded UTF-8**(`%XX`),且**空格会被编码成 `+`**(application/x-www-form-urlencoded 风格,**不是** RFC 5987 的 `filename*=UTF-8''xxx` 格式)。

所以 `urllib.parse.unquote_plus(value, encoding="utf-8")` 同时处理两种编码,完美还原中文文件名。

实现:`lib/downloader.py::_extract_filename_from_disposition`,提取后作为 auto_title 拼到 `{date}_{time}_{original_stem}.{ext}`。下游 tag handler 重命名时若用户没打 title,fallback 链自然用上原文件名。

### aeskey 缺 padding
`aeskey` 是 base64 但**只有 43 字符**(标准 base64 编码 32 字节内容应该 44 字符含 1 个 `=`)。`base64.b64decode` 严格会报 "Incorrect padding"。修法:`aeskey + "=" * (-len(aeskey) % 4)` 自己补齐。

### PKCS#7 padding 倍数不固定
官方文档说"填充到 32 字节倍数",但实测看 PDF 和图片有时是 16 字节倍数填充(否则它们应该全 work,但 docx 翻车说明确实有 32 字节情况)。pycryptodome 的 `unpad(block_size=16)` 拒绝 padding count > 16,`unpad(block_size=32)` 又强制要求密文长度是 32 的倍数,两边都不能 cover。

修法:**自己写宽容 unpad**,只看末尾字节 `n`、验证 `n ∈ [1, 32]` 且末尾 n 字节都是 0x{n},不做 `len % block_size` 检查。见 `lib/aes.py::_lenient_pkcs7_unpad`。

### URL 5 分钟过期
file / image 的 COS URL 里 `q-sign-time` 显示有效期 5 分钟。handler 必须 **inline 同步下载**,不能放后台 task 拖到几分钟后。

### text msgtype 包揽 URL 分享
微信 / 企微把"复制链接 → 粘贴"统一打包成 `msgtype: text`,URL 出现在 `text.content` 里,可能带【有道云笔记】/【腾讯文档】/【微信文档】这种自动前缀。所以**没有独立的 link msgtype**(`handlers/link.py` 在 HANDLERS dict 里给"万一企微哪天发 link" 占位,但实测从没触发)。

### 回复必须 markdown msgtype
通过 `response_url` POST 回复时,`msgtype` 必须填 `markdown`,`text` 不支持。

### `response_url` 是一次性的
对同一个 msgid 的 `response_url`,只能 POST 一次有效回执——**第二次 POST 服务端会接受但企微对话里看不到**。表现:如果先发"⏳ 正在处理...",再发"✅ 处理完成",用户只会看到第一条。

含义:**长耗时操作(比如公众号抓取 10-30 秒)只能选一种姿势**:
- 简单方案:**默默处理,等做完了一次性回最终结果**(成功 / 失败)——当前 wechat_mp_fetcher 采用这种
- 复杂方案:用官方"流式消息回复机制"(`msgtype: stream` + `finish: false/true` 多次 POST 更新同一条消息),代码量翻倍,以后真有 UX 痛点再考虑

### 长连接模式特有限制
- 同一时刻每个 bot 只能保持一个连接,新连接踢掉旧连接
- 离线消息会丢——本机睡眠 / VPN 抽风期间的消息**没有补发**
- 代理环境下 wss 可能挂,需在代理软件里把 `work.weixin.qq.com` 走直连

### Clash / mihomo fake-ip DNS 劫持的坑(实测踩过)
Clash Verge / mihomo 默认开启 **fake-ip 模式**(把所有域名 DNS 返回 `198.18.x.x` 假 IP,然后 TUN 拦截转发)。这套机制对 **HTTP/HTTPS 短连接没问题**,但对**长连接 + WSS** 会让 TLS 握手莫名 `ConnectionResetError`,即便 Clash 已切到规则模式 + qq.com 命中 DIRECT。

**症状**:`Test-NetConnection openws.work.weixin.qq.com -Port 443` 返回的 `RemoteAddress` 是 `198.18.0.x`,`InterfaceAlias=Meta`(Clash 虚拟网卡)。

**解药**:覆写配置里加 `dns.fake-ip-filter`,把这几个域名排除 fake-ip,让 Clash 返回真实 IP,Python TCP 直连。

```yaml
# Clash Verge → 全局扩展覆写配置
mode: rule

prepend-rules:
  - DOMAIN-SUFFIX,work.weixin.qq.com,DIRECT
  - DOMAIN-SUFFIX,weixin.qq.com,DIRECT
  - DOMAIN-SUFFIX,myqcloud.com,DIRECT
  - DOMAIN-SUFFIX,qq.com,DIRECT

dns:
  fake-ip-filter:
    - '+.work.weixin.qq.com'
    - '+.weixin.qq.com'
    - '+.qq.com'
    - '+.myqcloud.com'
```

**别只加 prepend-rules 不加 fake-ip-filter**——前者只让 Clash 知道"该 DIRECT",但 DNS 阶段域名仍被解析成假 IP,TLS 在那一步就挂了。两者都要加。

## 设计决策

### 文件落盘命名
**当前实现**:`{YYYYMMDD}_{HHMMSS}.{ext}`(例 `20260515_143215.pdf`)
- 用消息到达时刻的本地时间
- 不带 userid / msgid hash(用户决定:类型 / 公司 / source 由下游或人工归类)
- 同一秒内冲突极少,真撞了用 `.1.ext` 后缀避免覆盖

**长期目标**(待 tag 解析器实现):`{date}_{company}_{source}_{type}_[{period}]_{topic}.{ext}`,由"file + 紧随的 tag text 配对"机制驱动

### Tag 协议(已实现结构化版)
任何归档(file / image / 公众号 DOCX / 长文本笔记 DOCX)落盘后,该用户在 `TAG_WINDOW_SECONDS`(默认 300 秒)内发的一条**含 type 关键词**的短文字,会被解析成 5 个字段(type / source / industry / company / title)+ date,并以 `{date}_{type}_{subject}[_{source}]_{title}.{ext}` 格式重命名该归档,其中 `subject = company or industry`。

**解析规则**(`lib/tag_parser.py`):
- **type**(必需,白名单 + 最长匹配 + 大小写不敏感):专家访谈 / 付费专家 / 公司交流 / 卖方汇报 / 媒体新闻 / Alpine周度汇报 / 同行交流 / 新闻 / 传闻 / Alpine
  - "Alpine周度汇报" 优先于 "Alpine",防止短前缀误吞
- **source**(可选,白名单):专家网络(Acecamp / Thirdbridge / AlphaEngine / 公众号)+ 卖方(Citi / UBS / GS / MS / JPM / CICC / CLSA / Macquarie / Barclays / BofA / HSBC / Nomura / Jefferies / Deutsche / Bernstein / Daiwa)
  - 公众号抓取的 DOCX 默认带 `source_hint="公众号"`,用户 tag 没指定 source 时自动填上
- **industry**(白名单,company 的"行业版"替代):AI / 互联网 / 电商 / 本地生活 / 招聘 / 半导体 / 新能源 / 创新药 / 银行 / 券商 / 快递 等约 30 项
  - 用于资料对应行业而非特定公司的场景(如 "新闻 电商 行业增速放缓")
  - 与 company 互不冲突,二者**至少一个**即可成立 tag
- **company**(启发式):非白名单 token 里第一个"像公司名"的,优先级评分:
  - ≥2 汉字 = 100(中文名)
  - ≥3 字母含大写 = 50(Minimax / BABA / ByteDance)
  - ≥3 字母全小写 = 10(minimax / deepseek)
  - 同分取最早出现
- **title**(可选):再剩余的 token 用 `_` 连接;为空则 fallback 到老文件名里的 auto_title(如公众号文章标题、笔记第一行),再 fallback 到 HHMMSS

**subject 取舍规则**:
- 有 company → subject = company
- 无 company 有 industry → subject = industry(顶 company 槽位)
- 二者都有 → subject = company,industry 自动 prepend 到 title 不丢信息

**有效性校验**:type 必有 + (company OR industry) 至少一个。二者都没就拒绝。

**示例**:

| 原文件 | 用户 tag | 重命名后 |
|---|---|---|
| `2026-05-16_143020.pdf` | `公司交流 阿里巴巴` | `2026-05-16_公司交流_阿里巴巴_143020.pdf` |
| `2026-05-16_143020.pdf` | `卖方汇报 阿里 GS 创新药点评` | `2026-05-16_卖方汇报_阿里_GS_创新药点评.pdf` |
| `2026-05-16_143020.pdf` | `新闻 电商 行业增速放缓` | `2026-05-16_新闻_电商_行业增速放缓.pdf`(industry 占 subject) |
| `2026-05-16_143020.pdf` | `新闻 AI OpenAI 新模型` | `2026-05-16_新闻_OpenAI_AI_新模型.pdf`(industry prepend 到 title) |
| `2026-05-16_143020_阿里健康2HFY26_callback.docx` | `新闻 阿里` | `2026-05-16_新闻_阿里_公众号_阿里健康2HFY26_callback.docx` |
| `2026-05-16_143020.pdf` | `Alpine周度汇报 阿里 2026W20` | `2026-05-16_Alpine周度汇报_阿里_2026W20.pdf` |

**触发条件**:
- 无 URL
- 文本含 type 白名单关键词(`has_type_keyword`)→ **才视为 tag 意图**;否则当普通文本处理(避免闲聊被误消费)
- 文本长度 ∈ [3, NOTE_MIN_LENGTH-1)(默认 3~199 字符)
- 该 userid 在窗口内有未消费的 pending 归档(队列非空)

**校验失败**:
- 含 type 关键词但 parse 后 type 或 company 为空 → 不重命名,回执提示缺什么
- 含 type 关键词但无 pending 归档 → 提示"请先发文件再发 tag"

**并发处理**(FIFO 队列):
- pending 从单值 dict 改为 deque,**多个文件未 tag 时按发文件顺序排队**
- 用户发的 tag 消费**队列头(最老的)**:文件 A → 文件 B → tag X → tag Y 会得到 (X→A, Y→B)
- 单 userid 队列上限 20,防异常情况下内存失控

**状态**:`lib/pending_tag.py` 内存 dict[userid → deque],进程重启丢失(可接受,窗口本来就 5 分钟)。

### 引用模式(Phase 3,已实现)
用户在企微里**长按消息 → 引用 → 加一句 tag** 发送时,企微会在 body 里带 `quote` 字段(实测确认):

```json
"quote": {
    "msgtype": "text",          // 或 "markdown"
    "text":     {"content": "..."},
    // 或 "markdown": {"content": "..."}
}
```

注意:**quote 只带内容,不带原消息 msgid**。所以无法用 msgid 直接定位,只能从 quote.content 里挖文件名。

**实现**(`handlers/text.py::_handle_quoted_tag`):
1. 用户引用 bot 之前的回执(里面有 backtick-wrapped 文件名,如 `` `20260516_143020.pdf` ``)
2. 提取最后一个文件名匹配(rename 回执里"新名"在后,这是当前最新状态)
3. 在 ARCHIVE_DIR 找该文件,存在则**直接 rename(不走 pending 队列)**
4. 提取不到 → fall through 走常规 pending 流程

**主要用途**:
- **修正错 tag**:bot 之前给 X 打了 wrong tag → 用户引用那条回执 + 重发正确 tag → 文件被改正
- **给已过期 pending 加 tag**:文件保存超过 5 分钟,pending 已清 → 用户引用 bot 当时的"已保存"回执 + 发 tag → 仍能 rename
- **跨越 pending 队列指定文件**:有多个未 tag 文件时,精准 tag 中间某一个

**限制**:
- 只能引用 **bot 的回执**(因为只有那里有 backtick 包裹的文件名)
- 引用用户自己的文件 / URL 消息 → quote 里没有可解析的文件名,fall through 走常规流程
- 引用模式不消费 pending 队列(已落盘文件直接定位,不影响其他未 tag 文件)

**未来可补**(看痛点而定):
- 公司白名单 / 别名归一化(出现"阿里 / BABA / 阿里巴巴"被算作三个 company 的痛点时)
- 持久化 pending(sqlite),避免进程重启丢失
- 引用 URL 消息 → 通过 URL 反查文件(需建持久 `URL → file_path` 映射)

### NOTE_MIN_LENGTH 与 TAG_MIN_CHARS 的阈值关系
- 文本长度 < 3:回执"已识别为 文本消息",忽略(避免"ok"被误判为 tag)
- 3 ≤ 长度 < NOTE_MIN_LENGTH(默认 200) **且有 pending**:走 tag 重命名
- 3 ≤ 长度 < NOTE_MIN_LENGTH **无 pending**:回执"已识别为 文本消息"
- 长度 ≥ NOTE_MIN_LENGTH:落盘为长文本笔记 DOCX,然后**自己也登记 pending**,允许再发 tag 重命名

### 内容类型(9 类,未实现)
现有 6 类(invest-kb tags_dict.yml 复用) + 3 类新增:
- 新增「新闻」:公开媒体报道、公众号文章
- 新增「传闻」:未经证实的市场传言,**必须额外标注 reliability(high/medium/low)**
- 新增「Alpine」:作者本人(Stan)的点评、判断,source 同步为 Alpine

### sentiment 规则差异化(未实现)
- 「新闻」:评估事件对公司基本面的方向,而非作者态度
- 「Alpine」:作者本人的判断方向
- 「传闻」:如果传闻成真对公司基本面的方向;reliability 低的倾向 Neutral

## 支持的输入类型(实测)

| 输入 | msgtype | 当前处理 | 状态 |
|---|---|---|---|
| 极短文字(<3 字符) | text | 落盘 sample + 回执"文本消息" | ✅ |
| 短文字(3..199 字符,**有 pending 归档**) | text | **作为 tag 重命名 pending 文件** | ✅ |
| 短文字 + **引用 bot 回执** | text + quote | **引用模式:从 quote 抠出文件名直接 rename**(不消费 pending) | ✅ |
| 短文字(3..199 字符,无 pending) | text | 回执"文本消息" | ✅ |
| 长文字(≥NOTE_MIN_LENGTH) | text | 落盘 DOCX + 登记 pending(等下一条 tag) | ✅ |
| 含 URL(公众号) | text | Playwright 抓 → DOCX + 登记 pending | ✅ |
| 含 URL(普通财经新闻 / 博客)| text | Playwright + trafilatura 通用抽取 → DOCX | ✅ |
| 含 URL(有道云 / 腾讯文档) | text | 仅回执,待手动(需登录态) | ✅(stub) |
| 图片 | image | 解密 + 落盘 + 登记 pending | ✅ |
| 文件(PDF / Word / 等) | file | 解密 + 落盘 + 登记 pending | ✅ |
| 富 share 卡片(公众号 / 笔记 / 文档转发) | — | **平台拦截,根本到不了** | ❌(无解,需复制内容/链接) |
| 语音 / 视频 / 位置等 | voice/video/... | dump + handle_unknown 兜底 | ⚙️(待验证) |

## 运行环境
- 开发 / POC 阶段:**本地 Windows 笔记本**,白天工作时段开机即可
- 长期方案:国内云 VPS(50 元/月)+ 坚果云挂载实现 24×7,延后再上
- Python 解释器:**项目自带 .venv**,启动用 `D:\AI-Project\wechat-archiver\.venv`

## 项目结构(当前)
```
wechat-archiver/
├── .env / .env.example / .gitignore
├── requirements.txt              # websockets / httpx / dotenv / pycryptodome (+ playwright/docx 待加)
├── README.md
├── docs/
│   └── CONTEXT.md                # 本文件
├── wecom_bot_hello.py            # 已验证的最小 demo,reference 保留不维护
├── main.py                       # 主入口:长连接 + cmd/msgtype 分流
├── handlers/
│   ├── __init__.py               # HANDLERS dict + handle_unknown
│   ├── text.py                   # 含 URL 子分流
│   ├── image.py                  # 解密 + 落盘
│   ├── file.py                   # 解密 + 落盘
│   └── link.py                   # 占位,实测企微从不发 link msgtype
├── lib/
│   ├── __init__.py
│   ├── reply.py                  # reply_markdown + dump_body,best-effort 不抛
│   ├── url_detect.py             # find_url / classify_url / extract_title
│   ├── aes.py                    # AES-256-CBC + 宽容 PKCS#7 unpad
│   ├── filetype.py               # magic bytes → 扩展名
│   ├── downloader.py             # download → decrypt → save 一站式
│   ├── wechat_mp_fetcher.py      # Playwright 抓公众号 → DOCX(站点定制 selectors)
│   ├── generic_web_fetcher.py    # Playwright + trafilatura 通用网页 → DOCX
│   ├── text_note_saver.py        # 长文本 → DOCX
│   ├── pending_tag.py            # userid → FIFO 队列,等待 tag 重命名
│   └── tag_parser.py             # 白名单 + 启发式解析 tag → 结构化字段
├── samples/                      # 运行时的消息 body JSON,git ignored
└── inbox/                        # ARCHIVE_DIR 未设时的 fallback,git ignored
```
