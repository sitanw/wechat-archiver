# 微信投研归档助手 — 项目上下文

## 项目目标
搭建一个微信→企微→本地知识库的自动归档机器人:个人微信看到研究材料(公众号文章/PDF/Word/图片/文本传闻),转发给企微智能机器人,机器人自动按 tag 标签解析、改名、归档到本地。

## 当前状态(2026-05-15)
- ✅ 企微智能机器人创建完毕,API 长连接模式
- ✅ `wecom_bot_hello.py` 已验证以下链路:
  - WebSocket 连接 `wss://openws.work.weixin.qq.com`
  - bot_id + secret 鉴权
  - 30 秒心跳保活
  - 接收 `aibot_msg_callback` 消息
  - 通过 `response_url` HTTP POST 回复(必须用 markdown msgtype,text 不支持)
- ✅ 收到的消息结构已知,包含 msgid / aibotid / chattype / from.userid / msgtype / response_url

## 凭证位置
`.env` 文件(已配置)
- `WECOM_BOT_ID` / `WECOM_BOT_SECRET`

## 技术栈
- Python 3.x + websockets + httpx + python-dotenv
- 不引入 Node.js(企微官方 SDK 是 Node.js 的,但我们 invest-kb 整套都是 Python,自己用 websockets 库直连更干净)

## 关联项目
本项目是**独立项目**,不与 invest-kb 共享代码。但归档目标可能落到 invest-kb 的 raw 目录下。
invest-kb 项目里有个 `02b_format_external.py` 脚本,负责把外部材料洗成 wiki 结构。本项目是该 pipeline 的"前端"——把微信转发的素材送进 02b 的 input 目录。

## 设计决策(已定稿)

### 内容类型(types)
现有 6 类(invest-kb tags_dict.yml 已有,本项目复用) + 3 类新增:
- 新增「新闻」:公开媒体报道、公众号文章
- 新增「传闻」:未经证实的市场传言,**必须额外标注 reliability(high/medium/low)**
- 新增「Alpine」:作者本人(Stan)的点评、判断,source 同步为 Alpine

### Tag 协议
转发时附一句话标签,**词序不限**,例如:
> "Q1 26 公司交流 Callback 阿里巴巴"

解析规则:
- **company**:从公司白名单匹配(支持别名/英文/ticker)
- **type**:9 类之一
- **period**(可选):格式 NQYY 或 FYYY,如 `26Q1` / `FY25`
- 其余自由词进 topic

### 文件命名
`{date}_{company}_{source}_{type}_[{period}]_{topic}.{ext}`
- date:消息接收当日
- source:公众号文章 → "公众号";Alpine → "Alpine";其他通道留空或手动指定

### sentiment 规则差异化
- 「新闻」:评估事件对公司基本面的方向,而非作者态度
- 「Alpine」:作者本人的判断方向
- 「传闻」:如果传闻成真对公司基本面的方向;reliability 低的倾向 Neutral

## 支持的输入类型
- 文件:Word / PDF / 图片(JPG/PNG/WebP)
- 文本:直接发送的文字消息
- 公众号链接:通过 [WeSpy](https://github.com/tianchangNorth/WeSpy)(Python CLI 工具)抓取正文 → Markdown + JSON 元数据
- 腾讯文档/有道云链接:暂仅记录链接占位,待人工补抓
- 其他网页:同上

## 已知限制与决策
- 长连接模式下,**脚本必须一直在线**,离线消息会丢失(不能"攒一波再批处理")
- POC 阶段:本地笔记本运行,白天工作时段开机即可
- 长期方案:国内云 VPS(50 元/月)+ 坚果云挂载实现 24×7,延后再上
- 代理环境下 wss 连接会失败,需在代理软件里配置 `work.weixin.qq.com` 走直连

## 下一步:消息 Router 框架

基于 `wecom_bot_hello.py` 扩展:
1. `handle_frame` 收到 `aibot_msg_callback` 时按 `msgtype` 分流
2. 每种 msgtype 一个 handler:`handle_text` / `handle_image` / `handle_file` / `handle_link`
3. 暂时每个 handler 只打印消息字段 + 通过 response_url 回执"已识别为 X 类型"
4. **先不实现实际归档逻辑**,先把分流骨架走通,验证不同 msgtype 收到的字段长什么样

## 项目结构(将逐步演化为)
```
wechat-archiver/
├── .env / .env.example / .gitignore
├── requirements.txt
├── README.md
├── docs/
│   └── CONTEXT.md          # 本文件
├── wecom_bot_hello.py      # 已验证的最小版本,保留作 reference
├── main.py                 # 新主入口(下一步)
├── handlers/               # 各 msgtype handler
│   ├── __init__.py
│   ├── text.py
│   ├── image.py
│   ├── file.py
│   └── link.py
└── lib/                    # 共享工具(下一步)
    ├── wecom_client.py     # 长连接客户端
    └── reply.py            # response_url 回复 helper
```
