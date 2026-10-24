# 南方电网招标公告监控

每日定时抓取 [中国南方电网供应链统一服务平台 - 公共信息](https://www.bidding.csg.cn/ggxx/index.jhtml)，用本地 JSON 记录历史 URL 去重，命中关键词后推送到手机或群机器人，并通过 GitHub Actions 免费云端定时运行。

## 功能

- 抓取公共信息列表，默认检查前 3 页（可按需调整）
- `data/seen.json` 记录已处理的公告 URL，重启、重跑都不会重复推送
- 关键词匹配标题、发布单位，可选抓取正文提高准确率
- 支持任意/全部关键词、排除词
- 一条消息汇总多条命中公告，包含标题、单位、日期、关键词和原文链接
- 推送失败自动保留到 `pending` 队列，下次运行重试
- 支持 PushPlus、Server酱、企业微信、钉钉、飞书、Telegram、Bark、自定义 Webhook
- 首次运行只建立历史记录，不推送历史旧公告
- GitHub Actions 每天北京时间 09:00 自动运行，也可手动触发

## 目录结构

```text
.
├─ main.py                      # 抓取、去重、匹配、调度
├─ notifier.py                  # 推送渠道实现
├─ config.json                  # 站点、关键词、推送等配置
├─ requirements.txt             # Python 依赖
├─ data/seen.json               # 历史 URL 与待重试队列（由 Actions 自动提交）
├─ .github/workflows/monitor.yml# GitHub Actions 定时任务
└─ README.md
```

## 部署到 GitHub Actions

1. 新建一个 GitHub 仓库（公开仓库的 Actions 完全免费；私有仓库有免费额度）。
2. 把本项目全部文件推送到默认分支（通常是 `main`）：

   ```bash
   git init
   git add .
   git commit -m "init: 南网招标监控"
   git branch -M main
   git remote add origin https://github.com/<你的用户名>/<仓库名>.git
   git push -u origin main
   ```

3. 在仓库 **Settings → Secrets and variables → Actions → Secrets** 中添加推送渠道密钥（见下一节，至少配置一个）。
4. 打开仓库 **Actions** 页面，选择「南网招标监控」，点击 **Run workflow**：
   - 首次建议勾选 `seed`，只建立历史记录、不推送；
   - 或勾选 `dry_run` 查看会命中哪些公告（日志中会打印标题，`dry_run` 不会受「首次运行静默」影响）。
5. 之后每天北京时间 09:00 会自动运行，结果会提交到 `data/seen.json`。

> 定时任务只会在默认分支上生效。GitHub 的 cron 可能有 5～30 分钟延迟，属正常现象。

## 推送渠道配置

在仓库 Secrets 中配置任意一个或多个渠道。配置多个时，每次会向所有渠道推送。渠道选择优先级：`PUSH_CHANNELS` 变量 > `config.json` 的 `push.channels` > 自动识别已配置密钥的渠道。

| 渠道 | 所需 Secrets | 说明 |
| --- | --- | --- |
| PushPlus | `PUSHPLUS_TOKEN` | [pushplus.plus](https://www.pushplus.plus/)，微信推送，免费 |
| Server酱 | `SERVERCHAN_SENDKEY` | [sct.ftqq.com](https://sct.ftqq.com/)，微信推送 |
| 企业微信机器人 | `WECOM_WEBHOOK` | 群机器人完整 Webhook URL |
| 钉钉机器人 | `DINGTALK_WEBHOOK`、`DINGTALK_SECRET`（可选） | 群机器人 Webhook；加签机器人需填 Secret |
| 飞书机器人 | `FEISHU_WEBHOOK`、`FEISHU_SECRET`（可选） | 群机器人 Webhook；开启签名校验时需填 Secret |
| Telegram | `TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID` | 通过 @BotFather 创建 Bot，chat_id 可用 @userinfobot 查询 |
| Bark | `BARK_KEY` 或 `BARK_URL` | iOS 推送，`BARK_URL` 可以是完整设备地址 |
| 自定义 Webhook | `WEBHOOK_URL`、`WEBHOOK_TOKEN`（可选） | 向该地址 POST JSON，见下方示例 |

如果同时配置了多个渠道，只想启用其中某几个，可在 **Settings → Secrets and variables → Actions → Variables** 新建变量：

```text
PUSH_CHANNELS = wecom,dingtalk
```

支持的值：`pushplus`、`serverchan`、`wecom`、`dingtalk`、`feishu`、`telegram`、`bark`、`webhook`。

自定义 Webhook 收到的 JSON 示例：

```json
{
  "title": "【南网招标】2 条新公告 10-24",
  "markdown": "## 【南网招标】...",
  "html": "<b>...</b>",
  "text": "纯文本内容",
  "items": [
    {
      "url": "https://www.bidding.csg.cn/ggxx/1200446884.jhtml",
      "title": "公告标题",
      "org": "发布单位",
      "date": "2026-10-23",
      "keywords": ["服务器", "交换机"]
    }
  ]
}
```

`WEBHOOK_TOKEN` 会以 `Authorization: Bearer <token>` 请求头发送。

## 关键词配置

编辑 `config.json`：

```json
{
  "keywords": {
    "include": ["服务器", "交换机", "网络安全"],
    "exclude": ["中标结果", "流标"],
    "match_mode": "any",
    "fetch_detail": true,
    "detail_max_items": 40
  }
}
```

- `include`：关键词列表。留空 `[]` 表示匹配所有公告。
- `exclude`：命中任意排除词就跳过（标题或正文包含即可）。
- `match_mode`：`any` 命中任意关键词；`all` 必须同时命中全部关键词。
- `fetch_detail`：是否抓取公告正文一起匹配。正文只在公告首次出现时抓取，每次最多 `detail_max_items` 篇。
- 匹配不区分英文大小写。

也可以在 GitHub **Variables** 中覆盖，不必改代码：

| 变量 | 示例 | 说明 |
| --- | --- | --- |
| `INCLUDE_KEYWORDS` | `服务器,交换机,防火墙` | 逗号/分号分隔，覆盖 include |
| `EXCLUDE_KEYWORDS` | `中标结果,流标` | 覆盖 exclude |
| `MATCH_MODE` | `any` / `all` | 覆盖匹配模式 |
| `FETCH_DETAIL` | `true` / `false` | 是否抓正文 |
| `MAX_PAGES` | `3` | 每次最多抓取列表页数 |
| `PUSH_CHANNELS` | `wecom,telegram` | 启用的推送渠道 |

## 运行机制

1. 依次抓取列表第 1～N 页，遇到整页都是已见 URL 就提前停止。
2. 新公告按 `include` / `exclude` 做关键词匹配（可选抓正文）。
3. 所有新公告 URL 写入 `data/seen.json`，并记录标题、日期、单位、首次发现时间、是否命中。
4. 命中的公告加入待推送队列，按每 5 条一条消息汇总推送；失败的内容保留到 `pending`，下次运行重试。
5. Actions 最后提交 `data/seen.json`，实现跨运行去重。
6. 历史记录默认保留 180 天，最多 10000 条，自动清理。

状态文件示例：

```json
{
  "version": 1,
  "updated_at": "2026-10-24T09:00:00+08:00",
  "items": {
    "https://www.bidding.csg.cn/ggxx/1200446884.jhtml": {
      "title": "……",
      "org": "……",
      "date": "2026-10-23",
      "first_seen": "2026-10-24T09:00:00+08:00",
      "matched": true
    }
  },
  "pending": []
}
```

## 本地运行

需要 Python 3.10+。

```bash
pip install -r requirements.txt

# 只抓取和匹配，不推送、不写状态，适合调试关键词
python main.py --dry-run

# 只把当前列表写入历史，不推送
python main.py --seed

# 正常推送（先设置对应环境变量）
set PUSHPLUS_TOKEN=你的token       # Windows CMD
# export PUSHPLUS_TOKEN=你的token  # Linux / macOS / Git Bash
python main.py
```

常用参数：

```text
--config PATH       指定配置文件
--state PATH        指定去重记录文件
--max-pages N       覆盖最大抓取页数
--dry-run           只打印，不推送不写状态
--seed              只记录历史，不推送
-v, --verbose       输出调试日志
```

## 常见问题

**没有收到推送？**

- 查看 Actions 运行日志，确认是否命中了关键词；
- 用 `dry_run` 手动运行，日志会打印命中的公告；
- 检查对应渠道的 Secret 名称是否完全一致；
- 企业微信/钉钉/飞书机器人可能触发频率限制，适当调大 `max_items_per_message` 的间隔或减少关键词范围；
- 钉钉开启了「加签」时必须同时配置 `DINGTALK_SECRET`。

**定时任务没有运行？**

- 确认代码在默认分支；
- 仓库 60 天无任何提交时，GitHub 可能暂停定时任务，重新启用即可（本项目的自动提交通常可避免）；
- GitHub 的 cron 有延迟，且高峰期可能跳过，稍后手动运行一次即可。

**提示「列表页没有解析到公告」？**

网站改版会导致页面结构变化，需要调整 `main.py` 中的 `parse_list()`（列表）或 `parse_detail()`（正文）。

**想监控采购公告/招标公告？**

修改 `config.json`：

```json
"site": {
  "base_url": "https://www.bidding.csg.cn",
  "list_path": "/zbgg/index.jhtml",
  "page_path": "/zbgg/index_{page}.jhtml",
  "max_pages": 3
}
```

同时把 `LIST_ITEM_RE` 中的 `/ggxx/` 改成对应栏目路径（如 `/zbgg/`），或改成更通用的 `r"/[a-z]+/\d+\.jhtml$"`。

**首次运行会不会把历史公告全推一遍？**

不会。`history.first_run_silent` 默认为 `true`，首次运行只记录、不推送。

**Actions 会不会因为提交状态文件而循环触发？**

不会。工作流只监听 `schedule` 和 `workflow_dispatch`，不监听 `push`。

**提交去重记录失败（protected branch / permission denied）？**

如果默认分支开启了分支保护，`GITHUB_TOKEN` 可能无法直接 push。可以：
- 允许 GitHub Actions 直接推送到默认分支；或
- 把状态文件提交步骤改成用 Personal Access Token（PAT）认证；或
- 改用 Actions Cache / Artifact 保存 `data/seen.json`（持久性不如提交到仓库，缓存会被清理）。

## 注意事项

- 请合理设置抓取页数和请求间隔，避免对目标网站造成压力。
- 本工具仅用于公开信息的个人订阅，请遵守目标网站的服务条款及相关法律法规。
- 推送内容以网站原文为准，脚本只做转发和摘要。
