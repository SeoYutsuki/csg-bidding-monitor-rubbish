#!/usr/bin/env python3
"""推送渠道实现。

所有渠道都通过环境变量配置，便于在 GitHub Actions Secrets 中保存。
支持：PushPlus、Server酱、企业微信机器人、钉钉机器人、飞书机器人、
Telegram Bot、Bark（iOS）以及自定义 Webhook。

渠道选择优先级：
1. 环境变量 PUSH_CHANNELS（逗号分隔）；
2. config.json 中的 push.channels；
3. 自动识别当前已配置密钥的所有渠道。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html as html_lib
import logging
import re
import time
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import quote_plus

import requests

log = logging.getLogger("csg-monitor.notifier")

Message = dict[str, str]
Item = dict[str, Any]
Environ = Mapping[str, str]

ENV_REQUIREMENTS: dict[str, tuple[str, ...]] = {
    "pushplus": ("PUSHPLUS_TOKEN",),
    "serverchan": ("SERVERCHAN_SENDKEY",),
    "wecom": ("WECOM_WEBHOOK",),
    "dingtalk": ("DINGTALK_WEBHOOK",),
    "feishu": ("FEISHU_WEBHOOK",),
    "telegram": ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"),
    "bark": (),
    "webhook": ("WEBHOOK_URL",),
}


class PushError(RuntimeError):
    """推送失败。"""


# ---------------------------------------------------------------------------
# 消息格式化
# ---------------------------------------------------------------------------
def _md_escape(text: str) -> str:
    for char in ("\\", "[", "]", "*", "_", "`"):
        text = text.replace(char, "\\" + char)
    return text


def _truncate_chars(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text.rfind("\n", 0, limit - 30)
    if cut < limit // 2:
        cut = limit - 30
    return text[:cut] + "\n...(内容已截断)"


def _truncate_bytes(text: str, limit: int) -> str:
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    return raw[: max(0, limit - 40)].decode("utf-8", "ignore") + "\n...(内容已截断)"


def _split_text(text: str, limit: int) -> list[str]:
    parts: list[str] = []
    remaining = text
    while len(remaining) > limit:
        cut = remaining.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        parts.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip("\n")
    if remaining:
        parts.append(remaining)
    return parts or [text]


def build_messages(items: list[Item], heading: str, timestamp: str) -> Message:
    """同时生成 Markdown / HTML / 纯文本三种格式，供不同渠道使用。"""
    md: list[str] = [f"## {heading}", "", f"> 抓取时间：{timestamp}（北京时间）", ""]
    html: list[str] = [
        f"<b>{html_lib.escape(heading)}</b>",
        "",
        f"抓取时间：{html_lib.escape(timestamp)}（北京时间）",
        "",
    ]
    plain: list[str] = [heading, f"抓取时间：{timestamp}（北京时间）", ""]

    for index, item in enumerate(items, 1):
        title = str(item.get("title") or "(无标题)")
        org = str(item.get("org") or "-")
        date = str(item.get("date") or "-")
        url = str(item.get("url") or "")
        keywords = "、".join(str(k) for k in (item.get("keywords") or []))

        md.extend(
            [
                f"{index}. **{_md_escape(title)}**",
                f"   - 发布单位：{_md_escape(org)}",
                f"   - 发布日期：{date}",
            ]
        )
        if keywords:
            md.append(f"   - 命中关键词：{_md_escape(keywords)}")
        md.extend([f"   - [查看公告]({url})", ""])

        html.append(f"<b>{index}. {html_lib.escape(title)}</b>")
        html.append(f"发布单位：{html_lib.escape(org)}")
        html.append(f"发布日期：{html_lib.escape(date)}")
        if keywords:
            html.append(f"命中关键词：{html_lib.escape(keywords)}")
        html.append(f'<a href="{html_lib.escape(url, quote=True)}">查看公告</a>')
        html.append("")

        plain.append(f"{index}. {title}")
        plain.append(f"   发布单位：{org}")
        plain.append(f"   发布日期：{date}")
        if keywords:
            plain.append(f"   命中关键词：{keywords}")
        plain.append(f"   {url}")
        plain.append("")

    return {
        "markdown": "\n".join(md).strip() + "\n",
        "html": "\n".join(html).strip() + "\n",
        "plain": "\n".join(plain).strip() + "\n",
    }


# ---------------------------------------------------------------------------
# 渠道选择
# ---------------------------------------------------------------------------
def _channel_ready(name: str, env: Environ) -> bool:
    if name == "bark":
        return bool(env.get("BARK_KEY", "").strip() or env.get("BARK_URL", "").strip())
    return all(str(env.get(key, "")).strip() for key in ENV_REQUIREMENTS.get(name, ()))


def resolve_channels(configured: list[Any], env: Environ) -> list[str]:
    raw = str(env.get("PUSH_CHANNELS", "")).strip()
    if raw:
        names = [name.strip().lower() for name in re.split(r"[,，;；\s]+", raw) if name.strip()]
        source = "PUSH_CHANNELS"
    elif configured:
        names = [str(name).strip().lower() for name in configured if str(name).strip()]
        source = "config.json"
    else:
        names = [name for name in ENV_REQUIREMENTS if _channel_ready(name, env)]
        source = "自动识别"

    result: list[str] = []
    for name in names:
        if name not in CHANNEL_SENDERS:
            log.warning("忽略未知推送渠道: %s", name)
            continue
        if name not in result:
            result.append(name)

    if result:
        log.info("推送渠道来源: %s", source)
    return result


# ---------------------------------------------------------------------------
# 各渠道实现
# ---------------------------------------------------------------------------
def _require(env: Environ, key: str) -> str:
    value = str(env.get(key, "")).strip()
    if not value:
        raise PushError(f"缺少环境变量 {key}")
    return value


def _post(
    url: str,
    *,
    json_body: dict[str, Any] | None = None,
    data: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 20,
) -> Any:
    try:
        response = requests.post(url, json=json_body, data=data, headers=headers, timeout=timeout)
    except requests.RequestException as exc:
        raise PushError(f"请求失败: {exc}") from exc

    if not response.ok:
        raise PushError(f"HTTP {response.status_code}: {response.text[:300]}")
    try:
        return response.json()
    except ValueError:
        return {"raw": response.text}


def _send_pushplus(title: str, message: Message, items: list[Item], env: Environ) -> None:
    token = _require(env, "PUSHPLUS_TOKEN")
    result = _post(
        "https://www.pushplus.plus/send",
        json_body={
            "token": token,
            "title": title,
            "content": message["markdown"],
            "template": "markdown",
        },
    )
    if str(result.get("code")) != "200":
        raise PushError(f"PushPlus 返回异常: {result}")


def _send_serverchan(title: str, message: Message, items: list[Item], env: Environ) -> None:
    sendkey = _require(env, "SERVERCHAN_SENDKEY")
    short_title = title if len(title) <= 32 else title[:31] + "…"
    result = _post(
        f"https://sctapi.ftqq.com/{sendkey}.send",
        data={"title": short_title, "desp": message["markdown"]},
    )
    if str(result.get("code")) != "0":
        raise PushError(f"Server酱返回异常: {result}")


def _send_wecom(title: str, message: Message, items: list[Item], env: Environ) -> None:
    webhook = _require(env, "WECOM_WEBHOOK")
    result = _post(
        webhook,
        json_body={
            "msgtype": "markdown",
            "markdown": {"content": _truncate_bytes(message["markdown"], 4000)},
        },
    )
    if result.get("errcode") != 0:
        raise PushError(f"企业微信返回异常: {result}")


def _append_query(url: str, params: dict[str, str]) -> str:
    separator = "&" if "?" in url else "?"
    return url + separator + "&".join(f"{key}={value}" for key, value in params.items())


def _send_dingtalk(title: str, message: Message, items: list[Item], env: Environ) -> None:
    webhook = _require(env, "DINGTALK_WEBHOOK")
    secret = str(env.get("DINGTALK_SECRET", "")).strip()
    if secret:
        timestamp = str(round(time.time() * 1000))
        string_to_sign = f"{timestamp}\n{secret}"
        digest = hmac.new(
            secret.encode("utf-8"),
            string_to_sign.encode("utf-8"),
            digestmod=hashlib.sha256,
        ).digest()
        sign = quote_plus(base64.b64encode(digest))
        webhook = _append_query(webhook, {"timestamp": timestamp, "sign": sign})

    result = _post(
        webhook,
        json_body={
            "msgtype": "markdown",
            "markdown": {
                "title": title,
                "text": _truncate_bytes(message["markdown"], 18000),
            },
        },
    )
    if result.get("errcode") != 0:
        raise PushError(f"钉钉返回异常: {result}")


def _send_feishu(title: str, message: Message, items: list[Item], env: Environ) -> None:
    webhook = _require(env, "FEISHU_WEBHOOK")
    payload: dict[str, Any] = {
        "msg_type": "text",
        "content": {"text": _truncate_bytes(message["plain"], 25000)},
    }
    secret = str(env.get("FEISHU_SECRET", "")).strip()
    if secret:
        timestamp = str(int(time.time()))
        string_to_sign = f"{timestamp}\n{secret}"
        digest = hmac.new(
            string_to_sign.encode("utf-8"),
            b"",
            digestmod=hashlib.sha256,
        ).digest()
        payload["timestamp"] = timestamp
        payload["sign"] = base64.b64encode(digest).decode("utf-8")

    result = _post(webhook, json_body=payload)
    code = result.get("code", result.get("StatusCode"))
    if code != 0:
        raise PushError(f"飞书返回异常: {result}")


def _send_telegram(title: str, message: Message, items: list[Item], env: Environ) -> None:
    token = _require(env, "TELEGRAM_BOT_TOKEN")
    chat_id = _require(env, "TELEGRAM_CHAT_ID")
    pieces = _split_text(message["html"], 3800)
    for index, piece in enumerate(pieces, 1):
        if len(pieces) > 1:
            piece = f"<i>({index}/{len(pieces)})</i>\n{piece}"
        result = _post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json_body={
                "chat_id": chat_id,
                "text": piece,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
        )
        if not result.get("ok"):
            raise PushError(f"Telegram 返回异常: {result}")


def _send_bark(title: str, message: Message, items: list[Item], env: Environ) -> None:
    base = str(env.get("BARK_URL", "")).strip()
    key = str(env.get("BARK_KEY", "")).strip()
    if base:
        url = base.rstrip("/")
        if key and key not in url:
            url = f"{url}/{key}"
    elif key:
        url = f"https://api.day.app/{key}"
    else:
        raise PushError("缺少 BARK_KEY 或 BARK_URL")

    result = _post(
        url,
        json_body={
            "title": title,
            "body": _truncate_chars(message["plain"], 3500),
            "group": "南网招标",
            "icon": "https://www.bidding.csg.cn/r/cms/www/red/images/logo.png",
        },
    )
    if result.get("code") != 200:
        raise PushError(f"Bark 返回异常: {result}")


def _send_webhook(title: str, message: Message, items: list[Item], env: Environ) -> None:
    url = _require(env, "WEBHOOK_URL")
    headers = {"Content-Type": "application/json; charset=utf-8"}
    token = str(env.get("WEBHOOK_TOKEN", "")).strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    result = _post(
        url,
        json_body={
            "title": title,
            "markdown": message["markdown"],
            "html": message["html"],
            "text": message["plain"],
            "items": items,
        },
        headers=headers,
    )
    if isinstance(result, dict) and result.get("errcode") not in (None, 0):
        raise PushError(f"Webhook 返回异常: {result}")


CHANNEL_SENDERS: dict[str, Callable[[str, Message, list[Item], Environ], None]] = {
    "pushplus": _send_pushplus,
    "serverchan": _send_serverchan,
    "wecom": _send_wecom,
    "dingtalk": _send_dingtalk,
    "feishu": _send_feishu,
    "telegram": _send_telegram,
    "bark": _send_bark,
    "webhook": _send_webhook,
}


def send_all(
    channels: list[str],
    title: str,
    message: Message,
    items: list[Item],
    env: Environ,
) -> tuple[int, list[str]]:
    """逐个渠道推送，返回（成功渠道数，错误信息列表）。"""
    sent = 0
    errors: list[str] = []
    for name in channels:
        sender = CHANNEL_SENDERS.get(name)
        if sender is None:
            errors.append(f"{name}: 未知渠道")
            continue
        try:
            sender(title, message, items, env)
            sent += 1
            log.info("推送成功: %s", name)
        except Exception as exc:  # noqa: BLE001 - 单渠道失败不影响其他渠道
            errors.append(f"{name}: {exc}")
            log.error("推送失败 %s: %s", name, exc)
    return sent, errors
