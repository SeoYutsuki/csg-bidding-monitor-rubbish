#!/usr/bin/env python3
"""南方电网供应链统一服务平台 - 公共信息公告监控。

流程：
1. 抓取列表页（默认前 3 页）；
2. 用 data/seen.json 中记录的历史 URL 去重；
3. 按关键词匹配公告标题、发布单位，可选抓取正文；
4. 通过一个或多个推送渠道发送汇总消息；
5. 把已处理的 URL 写回 data/seen.json，由 GitHub Actions 提交保存。

本地用法：
    python main.py --dry-run     # 只抓取、匹配并打印，不推送、不写状态
    python main.py               # 正常抓取并推送
    python main.py --seed        # 只把当前公告写入历史，不推送
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from notifier import build_messages, resolve_channels, send_all

ROOT = Path(__file__).resolve().parent
CST = timezone(timedelta(hours=8))
LIST_ITEM_RE = re.compile(r"/ggxx/\d+\.jhtml$")
PAGE_COUNT_RE = re.compile(r"共\s*(\d+)\s*条记录\s*\d+\s*/\s*(\d+)\s*页")
INDEX_PAGE_RE = re.compile(r"index_(\d+)\.jhtml")

log = logging.getLogger("csg-monitor")

DEFAULT_CONFIG: dict[str, Any] = {
    "site": {
        "base_url": "https://www.bidding.csg.cn",
        "list_path": "/ggxx/index.jhtml",
        "page_path": "/ggxx/index_{page}.jhtml",
        "max_pages": 3,
    },
    "keywords": {
        # 留空表示匹配所有公告；修改为你关心的关键词
        "include": [
            "服务器",
            "交换机",
            "路由器",
            "防火墙",
            "存储",
            "数据库",
            "信息安全",
            "网络安全",
            "软件",
            "运维",
            "数字化",
            "智能化",
            "信创",
        ],
        "exclude": ["中标结果", "中标候选人", "流标", "失败公告"],
        # any = 命中任意关键词；all = 必须命中全部关键词
        "match_mode": "any",
        # 是否抓取公告正文一起匹配（更准确，但请求数更多）
        "fetch_detail": True,
        # 每次运行最多抓取多少篇正文
        "detail_max_items": 40,
    },
    "push": {
        # 留空则根据环境变量自动识别已配置的渠道
        "channels": [],
        "title_prefix": "【南网招标】",
        # 每条消息最多包含几条公告（企业微信限制较严，建议不超过 5）
        "max_items_per_message": 5,
    },
    "history": {
        # 历史 URL 保留天数
        "retention_days": 180,
        # 单次最多保留多少条历史记录
        "max_entries": 10000,
        # 首次运行只记录历史，不推送旧公告
        "first_run_silent": True,
    },
    "request": {
        "timeout": 20,
        "retries": 3,
        "delay_seconds": 0.8,
    },
}


# ---------------------------------------------------------------------------
# 配置与状态
# ---------------------------------------------------------------------------
def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def split_keywords(value: str) -> list[str]:
    parts = re.split(r"[,，;；\n]+", value)
    return dedupe([part.strip() for part in parts if part.strip()])


def dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value not in result:
            result.append(value)
    return result


def load_config(path: Path) -> dict[str, Any]:
    config = deep_merge(DEFAULT_CONFIG, {})
    if path.exists():
        try:
            user_config = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"读取配置文件失败 {path}: {exc}") from exc
        if not isinstance(user_config, dict):
            raise RuntimeError(f"配置文件必须是 JSON 对象: {path}")
        config = deep_merge(config, user_config)
    else:
        log.warning("配置文件不存在，使用内置默认配置: %s", path)

    keywords = config["keywords"]
    keywords["include"] = dedupe([str(k).strip() for k in keywords.get("include", []) if str(k).strip()])
    keywords["exclude"] = dedupe([str(k).strip() for k in keywords.get("exclude", []) if str(k).strip()])
    if keywords.get("match_mode") not in {"any", "all"}:
        raise RuntimeError("keywords.match_mode 必须是 any 或 all")
    return config


def apply_env_overrides(config: dict[str, Any]) -> None:
    """允许在 GitHub Actions 中用变量覆盖关键词等配置。"""
    include_env = os.environ.get("INCLUDE_KEYWORDS", "").strip()
    if include_env:
        config["keywords"]["include"] = split_keywords(include_env)

    exclude_env = os.environ.get("EXCLUDE_KEYWORDS", "").strip()
    if exclude_env:
        config["keywords"]["exclude"] = split_keywords(exclude_env)

    match_mode = os.environ.get("MATCH_MODE", "").strip().lower()
    if match_mode in {"any", "all"}:
        config["keywords"]["match_mode"] = match_mode

    fetch_detail = os.environ.get("FETCH_DETAIL", "").strip().lower()
    if fetch_detail in {"1", "true", "yes", "on"}:
        config["keywords"]["fetch_detail"] = True
    elif fetch_detail in {"0", "false", "no", "off"}:
        config["keywords"]["fetch_detail"] = False


def now_cst() -> datetime:
    return datetime.now(CST)


def env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": 1, "updated_at": None, "items": {}, "pending": []}
    try:
        state = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"读取去重记录失败 {path}: {exc}\n"
            "为避免重复推送旧公告，脚本不会自动清空该文件；请修复或手动删除后重试。"
        ) from exc

    if not isinstance(state, dict) or not isinstance(state.get("items"), dict):
        raise TypeError(f"去重记录格式不正确: {path}")
    state.setdefault("version", 1)
    state.setdefault("updated_at", None)
    pending = state.get("pending")
    state["pending"] = pending if isinstance(pending, list) else []
    return state


def save_state(state: dict[str, Any], path: Path, timestamp: datetime) -> None:
    state["updated_at"] = timestamp.isoformat(timespec="seconds")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    payload = json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    tmp_path.write_text(payload, encoding="utf-8")
    tmp_path.replace(path)
    log.info("已更新去重记录: %s", path)


def prune_state(state: dict[str, Any], config: dict[str, Any], now: datetime) -> int:
    retention_days = int(config["history"].get("retention_days", 180))
    max_entries = int(config["history"].get("max_entries", 10000))
    cutoff = now - timedelta(days=retention_days)

    removed = 0
    kept: dict[str, Any] = {}
    for url, record in state["items"].items():
        try:
            first_seen = datetime.fromisoformat(str(record.get("first_seen", "")))
        except ValueError:
            kept[url] = record
            continue
        if first_seen.tzinfo is None:
            first_seen = first_seen.replace(tzinfo=CST)
        if first_seen >= cutoff:
            kept[url] = record
        else:
            removed += 1

    if len(kept) > max_entries:
        ordered = sorted(
            kept.items(),
            key=lambda pair: str(pair[1].get("first_seen", "")),
            reverse=True,
        )
        removed += len(kept) - max_entries
        kept = dict(ordered[:max_entries])

    state["items"] = kept
    return removed


# ---------------------------------------------------------------------------
# 抓取与解析
# ---------------------------------------------------------------------------
def make_session(config: dict[str, Any]) -> requests.Session:
    request_cfg = config["request"]
    retry = Retry(
        total=int(request_cfg.get("retries", 3)),
        connect=int(request_cfg.get("retries", 3)),
        read=int(request_cfg.get("retries", 3)),
        backoff_factor=0.8,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session = requests.Session()
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Referer": config["site"]["base_url"].rstrip("/") + "/",
        }
    )
    return session


def fetch_html(session: requests.Session, url: str, timeout: float) -> str:
    try:
        response = session.get(url, timeout=timeout)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise RuntimeError(f"请求失败 {url}: {exc}") from exc
    # 站点声明 UTF-8，但响应头可能不带 charset，这里强制指定避免中文乱码
    response.encoding = "utf-8"
    return response.text


def canonical_url(base_url: str, href: str) -> str:
    absolute = urljoin(base_url.rstrip("/") + "/", href.strip())
    parts = urlsplit(absolute)
    path = parts.path or "/"
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def build_page_url(config: dict[str, Any], page: int) -> str:
    site = config["site"]
    path = site["list_path"] if page == 1 else site["page_path"].format(page=page)
    return urljoin(site["base_url"].rstrip("/") + "/", str(path).lstrip("/"))


def parse_list(html: str, base_url: str) -> tuple[list[dict[str, str]], int | None, int | None]:
    soup = BeautifulSoup(html, "html.parser")
    items: list[dict[str, str]] = []
    seen_urls: set[str] = set()

    for li in soup.select("div.List2 li"):
        title_anchor = None
        url = ""
        for anchor in li.find_all("a", href=True):
            candidate = canonical_url(base_url, anchor["href"])
            if LIST_ITEM_RE.search(urlsplit(candidate).path):
                title_anchor = anchor
                url = candidate
                break
        if title_anchor is None or url in seen_urls:
            continue

        date_el = li.select_one("span.Gray")
        org_el = li.select_one("a.Blue")
        title = title_anchor.get_text(" ", strip=True)
        if not title:
            continue

        seen_urls.add(url)
        items.append(
            {
                "url": url,
                "title": title,
                "org": org_el.get_text(" ", strip=True) if org_el else "",
                "date": date_el.get_text(" ", strip=True) if date_el else "",
            }
        )

    total_records: int | None = None
    total_pages: int | None = None
    text = soup.get_text(" ", strip=True)
    match = PAGE_COUNT_RE.search(text)
    if match:
        total_records = int(match.group(1))
        total_pages = int(match.group(2))
    else:
        page_numbers = [
            int(m.group(1))
            for anchor in soup.select("div.fenye a[href]")
            if (m := INDEX_PAGE_RE.search(anchor["href"]))
        ]
        if page_numbers:
            total_pages = max(page_numbers)

    return items, total_records, total_pages


def parse_detail(html: str) -> tuple[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    title_el = soup.select_one("h1.s-title")
    content_el = soup.select_one("div.Content")
    title = title_el.get_text(" ", strip=True) if title_el else ""
    content = content_el.get_text("\n", strip=True) if content_el else ""
    return title, content


def collect_new_items(
    config: dict[str, Any],
    state: dict[str, Any],
    session: requests.Session,
    max_pages: int,
) -> list[dict[str, str]]:
    seen = state["items"]
    added_urls = set(seen)
    new_items: list[dict[str, str]] = []
    timeout = float(config["request"]["timeout"])
    delay = float(config["request"].get("delay_seconds", 0.8))

    page = 1
    while page <= max_pages:
        url = build_page_url(config, page)
        log.info("抓取列表第 %d 页: %s", page, url)
        html = fetch_html(session, url, timeout)
        items, total_records, total_pages = parse_list(html, config["site"]["base_url"])

        if not items:
            if page == 1:
                raise RuntimeError("列表页没有解析到公告，页面结构可能已变化，请检查 parse_list()")
            log.info("第 %d 页没有公告，停止翻页", page)
            break

        if page == 1 and total_pages:
            log.info("列表共 %s 条记录 / %d 页，本次最多抓取 %d 页", total_records or "?", total_pages, max_pages)
            max_pages = min(max_pages, total_pages)

        page_new = 0
        for item in items:
            if item["url"] in added_urls:
                continue
            added_urls.add(item["url"])
            new_items.append(item)
            page_new += 1

        log.info("第 %d 页: %d 条公告, 其中新增 %d 条", page, len(items), page_new)
        if page_new == 0:
            # 列表按发布时间倒序，整页都见过说明已经追上进度
            log.info("第 %d 页没有新公告，停止翻页", page)
            break

        page += 1
        if page <= max_pages:
            time.sleep(delay)

    return new_items


def match_keywords(text: str, include: list[str], exclude: list[str], mode: str) -> list[str]:
    lowered = text.casefold()
    for keyword in exclude:
        if keyword.casefold() in lowered:
            return []
    if not include:
        return ["全部"]
    hits = [keyword for keyword in include if keyword.casefold() in lowered]
    if mode == "all" and len(hits) != len(include):
        return []
    return hits


def enrich_and_match(
    item: dict[str, Any],
    config: dict[str, Any],
    session: requests.Session,
    detail_budget: list[int],
) -> bool:
    keywords_cfg = config["keywords"]
    text_parts = [item["title"], item.get("org", "")]

    if keywords_cfg.get("fetch_detail") and detail_budget[0] > 0:
        detail_budget[0] -= 1
        try:
            html = fetch_html(session, item["url"], float(config["request"]["timeout"]))
            detail_title, detail_content = parse_detail(html)
            if detail_title:
                item["title"] = detail_title
            if detail_content:
                text_parts.append(detail_content[:20000])
        except RuntimeError as exc:
            log.warning("抓取正文失败，仅用标题匹配: %s", exc)
        time.sleep(float(config["request"].get("delay_seconds", 0.8)))

    hits = match_keywords(
        "\n".join(text_parts),
        keywords_cfg["include"],
        keywords_cfg["exclude"],
        keywords_cfg["match_mode"],
    )
    item["keywords"] = hits
    return bool(hits)


def mark_seen(state: dict[str, Any], item: dict[str, Any], matched: bool, first_seen: str) -> None:
    state["items"][item["url"]] = {
        "title": item.get("title", ""),
        "org": item.get("org", ""),
        "date": item.get("date", ""),
        "first_seen": first_seen,
        "matched": matched,
    }


def merge_batch(pending: list[Any], matched: list[dict[str, Any]]) -> list[dict[str, Any]]:
    batch: list[dict[str, Any]] = []
    urls: set[str] = set()
    for item in [*pending, *matched]:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url", "")).strip()
        if not url or url in urls:
            continue
        urls.add(url)
        batch.append(
            {
                "url": url,
                "title": str(item.get("title", "")),
                "org": str(item.get("org", "")),
                "date": str(item.get("date", "")),
                "keywords": [str(k) for k in (item.get("keywords") or [])],
            }
        )
    return batch


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="南方电网公共信息公告监控")
    parser.add_argument("--config", type=Path, default=ROOT / "config.json", help="配置文件路径")
    parser.add_argument("--state", type=Path, default=ROOT / "data" / "seen.json", help="去重记录路径")
    parser.add_argument("--max-pages", type=int, default=None, help="覆盖配置中的最大抓取页数")
    parser.add_argument("--dry-run", action="store_true", help="只打印结果，不推送也不写状态")
    parser.add_argument("--seed", action="store_true", help="只记录当前公告为已见，不推送")
    parser.add_argument("--test-push", action="store_true", help="完整运行一次监控，并额外发送一条测试消息")
    parser.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")
    return parser.parse_args(argv)


def send_test_push(config: dict[str, Any], now: datetime) -> int:
    """向所有已配置渠道发送一条测试消息。"""
    channels = resolve_channels(config["push"].get("channels") or [], os.environ)
    if not channels:
        log.error(
            "没有可用的推送渠道。请先配置对应密钥（例如 PUSHPLUS_TOKEN、WECOM_WEBHOOK），"
            "或用 PUSH_CHANNELS 指定渠道。"
        )
        return 1

    timestamp = now.strftime("%Y-%m-%d %H:%M")
    prefix = str(config["push"].get("title_prefix", ""))
    title = f"{prefix}测试推送 {now.strftime('%m-%d %H:%M')}".strip()
    item = {
        "url": config["site"]["base_url"].rstrip("/") + config["site"]["list_path"],
        "title": "这是一条测试公告，收到即表示推送配置正常",
        "org": "南网招标监控",
        "date": now.strftime("%Y-%m-%d"),
        "keywords": ["测试"],
    }
    message = build_messages([item], title, timestamp)

    log.info("使用推送渠道: %s", ", ".join(channels))
    sent, errors = send_all(channels, title, message, [item], os.environ)
    if sent:
        print(f"测试消息已发送：成功 {sent}/{len(channels)} 个渠道")
        if errors:
            print("部分渠道失败：" + "; ".join(errors))
        return 0 if not errors else 1
    log.error("测试消息发送失败: %s", "; ".join(errors))
    return 1


def run(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    apply_env_overrides(config)

    test_mode = args.test_push or env_flag("TEST_PUSH")
    if test_mode:
        log.info("测试模式：先完整运行一次监控程序，结束后再发送一条测试消息")

    max_pages = args.max_pages or int(os.environ.get("MAX_PAGES") or config["site"].get("max_pages", 3))
    max_pages = max(1, max_pages)
    state_path = args.state
    state = load_state(state_path)
    state_was_empty = not state["items"]

    now = now_cst()
    timestamp = now.strftime("%Y-%m-%d %H:%M")
    iso_now = now.isoformat(timespec="seconds")

    removed = prune_state(state, config, now)
    if removed:
        log.info("清理过期历史记录 %d 条", removed)

    session = make_session(config)
    new_items = collect_new_items(config, state, session, max_pages)

    seed = args.seed or env_flag("SEED")
    dry_run = args.dry_run or env_flag("DRY_RUN")
    first_run_silent = (
        bool(config["history"].get("first_run_silent", True))
        and state_was_empty
        and not test_mode
    )

    if seed or (first_run_silent and not state["pending"] and not dry_run):
        log.info(
            "%s：把 %d 条现有公告写入历史记录，本次不推送",
            "初始化模式" if seed else "首次运行",
            len(new_items),
        )
        for item in new_items:
            mark_seen(state, item, False, iso_now)
        if dry_run:
            for item in new_items:
                log.info("[dry-run] %s | %s", item["date"], item["title"])
            return 0
        save_state(state, state_path, now)
        return 0

    detail_budget = [int(config["keywords"].get("detail_max_items", 40))]
    matched: list[dict[str, Any]] = []
    for index, item in enumerate(new_items, 1):
        log.info("处理新公告 %d/%d: %s", index, len(new_items), item["title"])
        is_match = enrich_and_match(item, config, session, detail_budget)
        mark_seen(state, item, is_match, iso_now)
        if is_match:
            matched.append(item)
            log.info("命中关键词: %s", "、".join(item["keywords"]))

    batch = merge_batch(state["pending"], matched)
    if not new_items and not batch:
        log.info("没有新公告，也没有待补推送内容")
        if removed:
            save_state(state, state_path, now)
        return 0

    log.info("本次新增 %d 条公告，命中 %d 条，待推送 %d 条", len(new_items), len(matched), len(batch))
    if dry_run:
        print("\n========== DRY RUN ==========")
        print(f"新增 {len(new_items)} 条，命中 {len(matched)} 条")
        for item in batch:
            print(f"- [{item['date']}] {item['title']} ({item['org']})")
            print(f"  关键词: {'、'.join(item['keywords']) or '-'}")
            print(f"  {item['url']}")
        print("=============================\n")
        return 0

    if not batch:
        log.info("没有命中关键词的公告，无需推送")
        # 即使没有命中，也要把新公告写入历史，避免下次重复抓取和匹配
        state["pending"] = []
        save_state(state, state_path, now)
        return 0

    channels = resolve_channels(config["push"].get("channels") or [], os.environ)

    # 先落盘待推送队列，防止推送过程中异常导致内容丢失
    state["pending"] = batch
    save_state(state, state_path, now)

    if not channels:
        log.error(
            "没有可用的推送渠道。请配置 PUSH_CHANNELS 及对应密钥，"
            "或设置 config.json 中的 push.channels。"
        )
        return 1

    log.info("使用推送渠道: %s", ", ".join(channels))
    chunk_size = max(1, int(config["push"].get("max_items_per_message", 5)))
    chunks = [batch[i : i + chunk_size] for i in range(0, len(batch), chunk_size)]
    prefix = str(config["push"].get("title_prefix", ""))
    if test_mode:
        prefix = f"{prefix}测试运行 "
    failed: list[dict[str, Any]] = []

    for index, chunk in enumerate(chunks, 1):
        suffix = f" ({index}/{len(chunks)})" if len(chunks) > 1 else ""
        title = f"{prefix}{len(chunk)} 条新公告{suffix} {now.strftime('%m-%d')}".strip()
        message = build_messages(chunk, title, timestamp)
        sent, errors = send_all(channels, title, message, chunk, os.environ)
        if sent == 0:
            failed.extend(chunk)
            log.error("第 %d/%d 条消息推送失败: %s", index, len(chunks), "; ".join(errors))
        elif errors:
            log.warning("第 %d/%d 条消息部分渠道失败: %s", index, len(chunks), "; ".join(errors))
        else:
            log.info("第 %d/%d 条消息推送完成", index, len(chunks))

    state["pending"] = failed
    save_state(state, state_path, now)
    if failed:
        log.error("有 %d 条公告推送失败，已保留在 pending 队列，下次运行会重试", len(failed))
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    test_mode = args.test_push or env_flag("TEST_PUSH")
    try:
        exit_code = run(args)
    except KeyboardInterrupt:
        log.warning("用户中断")
        return 130
    except Exception:
        log.exception("运行失败")
        exit_code = 1

    if test_mode:
        if args.dry_run or env_flag("DRY_RUN"):
            log.info("dry-run 模式不发送测试消息")
            return exit_code
        try:
            config = load_config(args.config)
            apply_env_overrides(config)
            test_code = send_test_push(config, now_cst())
        except Exception:
            log.exception("测试消息发送失败")
            test_code = 1
        return exit_code or test_code

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
