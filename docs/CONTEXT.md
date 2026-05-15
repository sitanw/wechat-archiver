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

### 🔜 当前推进中
- **公众号链接抓取 → DOCX**:复用用户 AceCamp scraper 的 Playwright 套路(异步、反检测、DOCX 输出),针对 `mp.weixin.qq.com` URL 抓正文 + 图片,落盘成 DOCX 喂下游

### 后续规划
- 腾讯文档 / 有道云笔记 / 网盘类:**半自动或手动**(用户已表态可接受),不强求自动化
- tag 解析器(给落盘文件加更友好的命名前缀,目前留空白由人工 / 下游补)

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

### body 不带原始文件名
file / image 消息 body 只有 `url` 和 `aeskey`,**没有 filename / mimetype / size**。微信客户端显示的"xxx.docx"在传给我们前就被剥掉。扩展名只能解密后看 magic bytes 自己判。

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

## 设计决策

### 文件落盘命名
**当前实现**:`{YYYYMMDD}_{HHMMSS}.{ext}`(例 `20260515_143215.pdf`)
- 用消息到达时刻的本地时间
- 不带 userid / msgid hash(用户决定:类型 / 公司 / source 由下游或人工归类)
- 同一秒内冲突极少,真撞了用 `.1.ext` 后缀避免覆盖

**长期目标**(待 tag 解析器实现):`{date}_{company}_{source}_{type}_[{period}]_{topic}.{ext}`,由"file + 紧随的 tag text 配对"机制驱动

### Tag 协议(未实现)
转发文件 / 链接之后立刻发一句 tag,词序不限,例如:
> "Q1 26 公司交流 Callback 阿里巴巴"

未来 tag 解析器规则:
- **company**:**MVP 阶段直接取用户输入的公司词**,不做归一化(白名单留作后续优化,等真出现"阿里巴巴 / BABA / 阿里"被聚合到不同 company 字段"的痛点时再补)
- **type**:9 类之一
- **period**(可选):格式 NQYY 或 FYYY,如 `26Q1` / `FY25`
- 其余自由词进 topic

### NOTE_MIN_LENGTH 阈值设计意图
默认 200 字符。比这个低的纯文本会有一定概率是"tag 文字"(比如"Q1 26 callback 阿里巴巴")而不是真正的笔记内容——MVP 阶段没有 tag parser 配对机制,所以阈值放高一点,避免把 tag 文字误当独立笔记落盘。**等 tag parser 上线后,这类短文字会被识别为 file/链接的 tag,**这时候阈值可以放低甚至取消。

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
| 短文字(<200 字) | text | 落盘 sample + 回执"文本消息" | ✅ |
| 长文字(≥NOTE_MIN_LENGTH,默认 200) | text | 第一行作标题 → 落盘成 DOCX 到 ARCHIVE_DIR | ✅ |
| 含 URL 的文字(公众号) | text | Playwright 抓正文 → DOCX 落到 ARCHIVE_DIR(默认不带图,INCLUDE_IMAGES 可开) | ✅ |
| 含 URL 的文字(有道云) | text | URL 检测 → 回执"暂仅记录,待人工补抓" | ✅(stub) |
| 含 URL 的文字(腾讯文档 / 微信文档) | text | 同上 | ✅(stub) |
| 含 URL 的文字(普通网页) | text | 同上 | ✅(stub) |
| 图片 | image | 下载 + AES 解密 + magic bytes → 落盘 .jpg/.png/.webp/.gif | ✅ |
| 文件(PDF / Word) | file | 下载 + AES 解密 + magic bytes → 落盘 .pdf/.docx/.xlsx/.pptx 等 | ✅ |
| 富 share 卡片(公众号 / 腾讯文档 / 笔记 等转发) | — | **平台拦截,根本到不了** | ❌(无解,需用户复制内容/链接为 text) |
| 语音 / 视频 / 位置等 | voice/video/... | 落盘样本 + handle_unknown 兜底 | ⚙️(待验证哪些 msgtype 会到) |

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
│   └── downloader.py             # download → decrypt → save 一站式
├── samples/                      # 运行时的消息 body JSON,git ignored
└── inbox/                        # ARCHIVE_DIR 未设时的 fallback,git ignored
```
