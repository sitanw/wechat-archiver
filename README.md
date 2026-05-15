# 微信投研归档助手

企微智能机器人长连接接入 — 自动归档微信转发的研究材料。

## 快速开始

```bash
# 1. 建虚拟环境并装依赖
python -m venv .venv
source .venv/bin/activate    # Windows PowerShell: .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# 2. 配置凭证(每台新机器都要做)
cp .env.example .env         # Windows: copy .env.example .env
# 然后编辑 .env,填入真实的 WECOM_BOT_ID 和 WECOM_BOT_SECRET
# .env 已在 .gitignore 中,不会被提交到 git

# 3. 启动
python main.py               # 主入口,带 msgtype 路由
```

> `wecom_bot_hello.py` 是已验证的最小连接 demo,保留作为出问题时的 baseline 回退,日常运行用 `main.py`。

## 项目结构

```
main.py                   # 主入口:WebSocket 长连接 + cmd/msgtype 分流
wecom_bot_hello.py        # 已验证的最小 demo,reference,不再维护
handlers/                 # 各 msgtype handler(text/image/file/link + unknown 兜底)
lib/reply.py              # 共享 helper: reply_markdown + dump_body
samples/                  # 运行时落盘的消息样本,git ignored
docs/CONTEXT.md           # 项目背景、设计决策、下一步路线
```

每条进来的消息会自动写一份到 `samples/{msgtype}/{msgid}.json`,作为后续写解析器时的真实测试数据。

## 状态

- [x] Hello world: 长连接打通 + 消息接收 + 简单回复
- [x] 消息 router 框架: msgtype 分流 + 样本落盘 + 未知类型兜底
- [ ] 文件下载（image/file/video AES 解密）
- [ ] Tag 解析器
- [ ] WeSpy 公众号抓取集成
- [ ] 文件改名与归档
