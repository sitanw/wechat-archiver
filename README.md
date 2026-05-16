# 微信投研归档助手

把微信里看到的研究材料(公众号文章 / PDF / Word / 图片 / 笔记)转发给企微机器人 → bot 自动解密、抓取、命名、落到指定目录 → 喂下游 pipeline 处理。

## 一分钟跑起来

```powershell
cd D:\AI-Project\wechat-archiver
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -i https://pypi.tuna.tsinghua.edu.cn/simple "greenlet==3.1.1" -r requirements.txt
playwright install chromium

copy .env.example .env
# 编辑 .env 填入 WECOM_BOT_ID / WECOM_BOT_SECRET / ARCHIVE_DIR

python main.py
```

看到 `[订阅响应] {"errcode":0,...}` 就成了。

## 日常怎么用

### 1. 文件 / 图片转发 → 自动解密落盘

| 你在微信干啥 | bot 干啥 |
|---|---|
| 转发 PDF / Word / Excel / PPT | 解密 + 落到 `ARCHIVE_DIR` |
| 转发图片 | 同上,识别成 .jpg/.png/.webp/.gif |
| 转发公众号链接(**复制链接发,不要转发卡片**) | Playwright 抓正文 → DOCX 落盘 |
| 复制粘贴长篇笔记(≥200 字) | 落 DOCX,第一行做标题 |

**重要**:**富 share 卡片(公众号卡片 / 笔记卡片 / 文档卡片)发不进来**——企微平台层挡掉了。要么复制链接发,要么复制全文发。

### 2. 跟一条 tag 文字 → 自动重命名

文件保存后 5 分钟内,**发一句包含 type 关键词的短文字**,bot 会重命名:

```
你: [转发一个 PDF]
bot: ✅ 已保存: 2026-05-16_211020.pdf

你: 公司交流 阿里巴巴 GS Q1 callback
bot: ✅ 已重命名: 2026-05-16_公司交流_阿里巴巴_GS_Q1_callback.pdf
```

**tag 字段顺序无所谓**,bot 自动识别:
- **type**(必需,白名单 10 类):专家访谈 / 付费专家 / 公司交流 / 卖方汇报 / 媒体新闻 / Alpine周度汇报 / 同行交流 / 新闻 / 传闻 / Alpine
- **source**(可选,白名单 18 个):Acecamp / Thirdbridge / AlphaEngine / 公众号 / Citi / UBS / GS / MS / JPM / CICC / CLSA / Macquarie / Barclays / BofA / HSBC / Nomura / Jefferies / Deutsche / Bernstein / Daiwa
- **company**(必需):2+ 汉字(阿里巴巴) 或 3+ 全大写(BABA) 或 3+ 字母(minimax)
- **date**(可选):`2026-05-07` / `2026/05/07` / `20260507` 任一格式;不打则用文件实际日期
- **title**(可选):其他自由词;不打则用原文件名

最终文件名:`{date}_{type}_{company}[_{source}]_{title}.{ext}`

### 3. 多文件按顺序排队(FIFO)

```
你: [文件 A]    你: [文件 B]    你: tag for A    你: tag for B
        ↓             ↓               ↓                ↓
   pending[A]   pending[A,B]   rename A,pop      rename B,pop
```

你发文件的顺序就是 tag 配对的顺序,**不会串味**。

### 4. 引用回执修正错 tag

打错了 tag?**长按 bot 的回执消息 → 引用 → 发新 tag**:

```
你: 公司交流 阿里巴巴   ← typo,应该是腾讯
bot: ✅ 重命名: 2026-05-16_公司交流_阿里巴巴_xxx.pdf

[长按上面那条回执 → 引用]
你: 腾讯              ← 只打要改的字段
bot: ✅ 重命名(引用 + 部分更新):
     2026-05-16_公司交流_腾讯_xxx.pdf  (其他字段从老文件名继承)
```

部分更新只在引用已经 tag 过的文件时生效。引用初次保存的文件还是要打完整 tag。

## 文件最终长什么样

落到 `ARCHIVE_DIR` 里:

```
2026-05-07_专家访谈_minimax_Acecamp_头部北美CSP一季度业绩解读.docx
2026-03-31_公司交流_腾讯_GS_Q4_2025_Callback.pdf
2026-05-16_新闻_快手_公众号_快手迎来核心人事调整.docx
2026-05-15_媒体新闻_比亚迪.pdf
```

下游脚本按 `_` 切 token,前 3 段(date / type / company)100% 可预测。

## .env 配置项一览

```env
# 必填
WECOM_BOT_ID=...
WECOM_BOT_SECRET=...
ARCHIVE_DIR=D:\path\to\archive\dir   # 解密后的文件落这里

# 选填(都有合理默认)
INCLUDE_IMAGES=                       # 公众号抓取是否带图,默认 false(图多是广告)
NOTE_MIN_LENGTH=                      # 长文本自动落盘阈值,默认 200 字符
TAG_WINDOW_SECONDS=                   # tag 配对窗口,默认 300 秒(5 分钟)
```

## 故障排查

### 启动报 `ModuleNotFoundError: No module named 'Crypto'`
没装 pycryptodome。`pip install pycryptodome` 即可。

### 启动报 Python 3.10 union 类型语法错误(`unsupported operand type(s) for |`)
你装的 Python <3.10。代码已 `from __future__ import annotations` 兼容,理论上不会报。如还报,贴行号给我。

### `[异常] ConnectionResetError`,连不上企微长连接
**最常见**:Clash/mihomo 的 fake-ip DNS 劫持把 `*.qq.com` 解析成 `198.18.x.x` 假 IP,TLS 握手挂掉。

修复:Clash Verge **全局扩展覆写配置**里加 `dns.fake-ip-filter` 排除 qq.com / myqcloud.com。完整配置见 `docs/CONTEXT.md` "Clash / mihomo fake-ip DNS 劫持的坑"段。

### 公众号抓取报"未找到正文容器 #js_content"
公众号 HTML 结构变了,或这条链接不是文章页。把 URL 贴给我,我调选择器。

### 文件解密报 "PKCS#7 unpad 失败"
理论上已通过宽容 unpad 解决。如还出现,可能是企微改了加密参数,贴 `samples/file/{msgid}.json` 给我看。

### tag 发了 bot 不响应
检查:
- tag 是不是包含**白名单 type 关键词**(没 type 关键词不会进 tag 模式)
- pending 是不是过期了(默认 5 分钟)
- company 部分是不是 2+ 汉字 或 3+ 字母(`callback`、`Q1` 不算 company)

## 进阶

- 设计决策、平台坑、技术架构 → `docs/CONTEXT.md`
- 调试用的消息样本 → `samples/{msgtype}/{msgid}.json`(运行时自动落,git ignored)
- 当前**未实现**的功能:腾讯文档 / 有道云 / 网盘自动抓取(走半自动 / 手动)、URL 反查映射、pending 持久化

## 项目结构

```
main.py                       # 入口:长连接 + cmd/msgtype 分流
wecom_bot_hello.py            # 最小验证 demo,保留作 baseline,不再维护
handlers/                     # 各 msgtype handler
  ├── text.py                 #  含 URL 子分流 + tag 配对 + 引用模式
  ├── file.py / image.py      #  下载 + 解密 + 落盘
  └── link.py                 #  企微实测从不发的 msgtype,占位
lib/
  ├── reply.py                # reply_markdown + dump_body
  ├── url_detect.py           # 域名分类(公众号 / 有道云 / 腾讯文档 / 网页)
  ├── aes.py                  # AES-256-CBC + 宽容 PKCS#7 unpad
  ├── filetype.py             # magic bytes → 扩展名
  ├── downloader.py           # 下载 + 解密 + Content-Disposition 抠原文件名
  ├── wechat_mp_fetcher.py    # Playwright 抓公众号 → DOCX
  ├── text_note_saver.py      # 长文本 → DOCX
  ├── pending_tag.py          # FIFO 队列等待 tag
  └── tag_parser.py           # 白名单 + 启发式解析 tag → 结构化字段
samples/                      # 运行时消息样本,git ignored
inbox/                        # ARCHIVE_DIR 未设时的 fallback,git ignored
docs/CONTEXT.md               # 完整设计文档
```
