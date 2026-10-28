"""Small course adapters around Radar's public-source, card and form modules.

Baseline: e2bcb2ba917644b8a88d9981d4e3eb514be98c65 (MIT, Rui Zhou).
No AI fallback, implicit resume selection, submission, or delivery claims.
"""
from __future__ import annotations

import html
import os
import re
import smtplib
from dataclasses import replace
from datetime import timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from urllib.parse import urlsplit

from career_assistant.config import DB_PATH, _validate, load_config
from career_assistant.db import Database
from career_assistant.models import JobPosting, now_hk
from career_assistant.notify.email_client import EmailChannel
from career_assistant.notify.report import build_html_card
from career_assistant.scoring.keyword_filter import KeywordFilter
from career_assistant.scrapers import SCRAPER_REGISTRY, get_scraper
from career_assistant.scrapers.base import NeedsManualVerifyError
from career_assistant.scrapers.linkedin import LinkedInPageError
from career_assistant.scrapers.browser import BrowserManager
from career_assistant.scrapers.job_types import normalize_job_types


class IntegrationError(RuntimeError):
    """An external action failed; display this without replacing saved reports."""


class SourceBrowser(BrowserManager):
    """Keep existing profiles; never reset a profile to evade source throttling."""

    def launch(self, source_key, force_headless=None, fresh=False):
        # Indeed list/detail share the normal login profile; no per-keyword identity.
        source_key = 'indeed' if source_key.startswith('indeed_') else source_key
        return super().launch(source_key, force_headless=force_headless, fresh=False)

    def close_source(self, source_key):
        return super().close_source('indeed' if source_key.startswith('indeed_') else source_key)

    def wait_cloudflare(self, page, *args, **kwargs):
        from career_assistant.scrapers.page_content import check_access
        check_access(page, urlsplit(page.url).hostname or '招聘页面')
        return True


def search_jobs(preferences: dict, config_path=None, *, db_path=None) -> list[JobPosting]:
    """Fetch and preliminarily filter using *confirmed* preferences.

    External sources provide summaries, not verified complete JDs. The caller
    persists immutable job snapshots and marks them as summaries. Source errors
    propagate. No profile inference, LLM scoring or notification happens here.
    """
    if preferences.get("confirmed") is not True:
        raise ValueError("请先确认求职偏好。")
    for key in ("keywords", "job_types"):
        values = preferences.get(key)
        if not isinstance(values, list) or not values or not all(
            isinstance(v, str) and v.strip() for v in values
        ):
            raise ValueError(f"请确认非空 {key} 列表。")
    if not isinstance(preferences.get("location"), str) or not preferences["location"].strip():
        raise ValueError("请确认求职地区。")
    config = load_config(Path(config_path) if config_path else None, require_runnable=False)
    config.keywords = [k.strip().lower() for k in preferences["keywords"]]
    config.graduate_keywords = []  # Never add targets the user did not confirm.
    config.location = preferences["location"].strip()
    config.job_types = normalize_job_types(preferences["job_types"])
    if not set(config.job_types).issubset({"internship", "part time", "full time"}):
        raise ValueError("本期支持的岗位类型为 internship、part time、full time。")
    config.career_stage = "both"  # Stage/visa cannot be inferred from a resume.
    # Radar auto-switches graduate keywords to full-time. A confirmed course
    # preference must be respected even for those keywords.
    config.job_types_for_keyword = lambda keyword: list(config.job_types)
    if "sources" in preferences:
        sources = preferences["sources"]
        if not isinstance(sources, list) or not sources or any(s not in SCRAPER_REGISTRY for s in sources):
            raise ValueError("请选择有效岗位来源：jobsdb、linkedin、indeed。")
        config.sources = {s: s in sources for s in SCRAPER_REGISTRY}
    _validate(config)
    if not config.enabled_sources():
        raise ValueError("未启用岗位来源。")
    path = Path(db_path or DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = Database(str(path))
    browser = SourceBrowser(config)
    cutoff = now_hk() - timedelta(days=config.max_job_age_days)
    jobs: list[JobPosting] = []
    try:
        for source in config.enabled_sources():
            limit = config.daily_limits.get(source, 0)
            if limit and db.today_counts(source) >= limit:
                raise IntegrationError(f"{source} 已达到今日抓取配额，请稍后再试。")
            scraper = get_scraper(source, config, browser, db=db, keywords=config.keywords)
            try:
                # Public fetch_new_jobs catches every failure and returns [].
                # Reuse its bounded retry implementation but preserve errors.
                found = scraper._attempt_fetch(cutoff)
                if source == "linkedin":
                    page = browser._pages.get(source)
                    if page is not None and scraper._page_is_authwall(page):
                        raise NeedsManualVerifyError(source)
                jobs.extend(found)
            except Exception as exc:
                db.log_event(source, "course_scrape_failed", {"error_type": type(exc).__name__})
                if isinstance(exc, LinkedInPageError):
                    raise IntegrationError(str(exc)) from exc
                if isinstance(exc, NeedsManualVerifyError):
                    raise IntegrationError(f'{source} 需要人工登录或验证；{exc}') from exc
                if isinstance(exc, RuntimeError):
                    raise IntegrationError(f'{source} 页面加载／解析失败：{exc}') from exc
                raise IntegrationError(
                    f"{source} 网络或浏览器操作失败（{type(exc).__name__}）；请检查连接和页面加载，已有数据保留。"
                ) from exc
        candidates = [j for j in jobs if j.posted_at is None or j.posted_at >= cutoff]
        keyword_filter = KeywordFilter(config)
        jobs = keyword_filter.filter(candidates)
        # No summary means insufficient evidence for a negative keyword match.
        # Retain these source hits for manual JD completion, respecting type filters.
        kept = {j.job_uid for j in jobs}
        jobs.extend(j for j in candidates if j.source == "linkedin" and not j.description_snippet.strip()
                    and j.job_uid not in kept
                    and keyword_filter.classify_job_type(j.title.lower())
                    in config.allowed_job_type_classes())
        if "full time" not in config.job_types:
            jobs = [
                j for j in jobs
                if KeywordFilter.classify_job_type(f"{j.title} {j.description_snippet}")
                != "graduate programme"
            ]
        # Radar's DB handles persistent deduplication. Avoid repeated rows in this
        # result using the same source identity and normalized URL.
        seen_ids, seen_urls = set(), set()
        unique = []
        for job in jobs:
            if job.job_uid in seen_ids or (job.url_normalized and job.url_normalized in seen_urls):
                continue
            seen_ids.add(job.job_uid)
            if job.url_normalized:
                seen_urls.add(job.url_normalized)
            unique.append(job)
        return unique
    finally:
        try:
            browser.close()
        finally:
            db.close()


def _http_url(value: str) -> str:
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
        raise ValueError("链接必须是无嵌入凭据的 http 或 https 地址。")
    return value


def render_recommendations(jobs: list[JobPosting], report_url: str) -> str:
    """Reuse Radar's escaped HTML cards; scores are supplied from a match version."""
    _http_url(report_url)
    cards = []
    for job in jobs:
        _http_url(job.url)
        card = build_html_card(replace(job))
        card = card.replace("未打分", "证据不足，暂无确定覆盖率")
        if job.llm_score is not None:
            card = card.replace(f"{job.llm_score} 分", f"覆盖分 {job.llm_score}")
        cards.append(card)
    summary = "覆盖分由 Python 规则计算，不是录用概率；要求与证据请打开完整报告。"
    if not jobs:
        summary = "本次没有可展示的推荐；来源无结果不表示没有合适岗位。"
    return (
        '<div style="font-family:sans-serif;max-width:720px;margin:auto">'
        f"<h1>INT6181 岗位推荐</h1><p>{summary}</p>"
        f'<p><a href="{html.escape(report_url, quote=True)}">打开本机报告与推荐</a>'
        "（本机地址仅承诺在运行项目的电脑打开）</p>"
        + "".join(cards) + "</div>"
    )


def send_email(subject: str, html: str) -> dict:
    """One SMTP attempt; caller stores and retries independently from analysis.

    SMTP_HOST absent => preview. Credentials come only from environment.
    A disconnect during DATA is uncertain and must not be retried automatically.
    Local test receivers can use SMTP_SSL=0, SMTP_STARTTLS=0 and SMTP_FROM.
    """
    host = os.getenv("SMTP_HOST", "").strip()
    if not host:
        return {"status": "preview", "error": "未配置 SMTP；仅生成本地预览，未发送。"}
    user = os.getenv("SMTP_USER", "").strip()
    password = os.getenv("SMTP_PASSWORD", "")
    sender = os.getenv("SMTP_FROM", user).strip()
    recipients = [a.strip() for a in os.getenv("SMTP_TO", "").split(",") if a.strip()]
    if not sender or not recipients or (user and not password):
        return {"status": "failed", "error": "SMTP 配置不完整：检查 FROM、TO、USER、PASSWORD。"}
    ssl_enabled = os.getenv("SMTP_SSL", "1").lower() in {"1", "true", "yes"}
    tls_enabled = os.getenv("SMTP_STARTTLS", "1").lower() in {"1", "true", "yes"}
    if not ssl_enabled and not tls_enabled and host not in {"localhost", "127.0.0.1", "::1"}:
        return {"status": "failed", "error": "非本机 SMTP 连接需要 SSL 或 STARTTLS。"}
    conn = None
    sending = False
    try:
        port = int(os.getenv("SMTP_PORT", "465" if ssl_enabled else "587"))
        channel = EmailChannel(host, port, user, password, recipients, use_ssl=ssl_enabled)
        conn = channel._connect() if ssl_enabled or tls_enabled else smtplib.SMTP(host, port, timeout=30)
        if user:
            conn.login(user, password)
        msg = MIMEMultipart("alternative")
        msg["Subject"], msg["From"], msg["To"] = subject, sender, ", ".join(recipients)
        msg.attach(MIMEText(html, "html", "utf-8"))
        sending = True
        refused = conn.sendmail(sender, recipients, msg.as_string())
        # sendmail returns after DATA's positive reply; some recipients can still
        # be refused. Do not resend to accepted recipients as part of a retry.
        return {
            "status": "accepted", "error": "",
            "refused_count": len(refused),
            "detail": "SMTP 服务器已接受，未验证送达。" + (
                f" 另有 {len(refused)} 个收件人被拒绝；不要整封自动重发。" if refused else ""
            ),
        }
    except (smtplib.SMTPRecipientsRefused, smtplib.SMTPResponseException) as exc:
        return {"status": "failed", "error": f"SMTP 明确拒绝（{type(exc).__name__}）。"}
    except Exception as exc:
        return {
            "status": "uncertain" if sending else "failed",
            "error": f"SMTP {'发送结果不确定，请人工核对后决定是否重发' if sending else '连接或配置失败'}"
                     f"（{type(exc).__name__}）。",
        }
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass  # A cleanup error cannot undo a server's positive reply.


SOURCE_CAPABILITIES = {
    "linkedin": {"listing": True, "details": True, "verification": "列表及详情曾成功，最近详情失败；新正文提取仅本地验证，真实未复验"},
    "jobsdb": {"listing": True, "details": True, "verification": "列表曾真实成功；详情适配已启用，仅本地样例验证，真实未复验"},
    "indeed": {"listing": True, "details": True, "verification": "列表历史失败原因未定；列表及详情改进仅本地样例验证，真实未复验"},
    "manual": {"listing": False, "details": False, "verification": "用户提供"},
}

def fetch_full_jd(snapshot: dict) -> str:
    """One read-only attempt at a source's structured full JobPosting description.

    Never treat a meta description/search snippet as a full JD. Unsupported or
    restricted pages require a user paste. No arbitrary host navigation.
    """
    from playwright.sync_api import Error as BrowserError
    from career_assistant.scrapers.page_content import check_access, wait_description
    hosts = {'linkedin': {'www.linkedin.com', 'hk.linkedin.com'},
             'jobsdb': {'hk.jobsdb.com'}, 'indeed': {'hk.indeed.com'}}
    parsed = urlsplit(snapshot['url'])
    if parsed.scheme != 'https' or parsed.hostname not in hosts.get(snapshot['source'], set()):
        raise IntegrationError('此来源不支持此详情链接，请粘贴完整 JD。')
    if snapshot['source'] == 'indeed' and 'jk=' not in parsed.query:
        raise IntegrationError('不支持缺少岗位 ID 的 Indeed 详情链接，请粘贴完整 JD。')
    config_path = os.environ.get('RADAR_CONFIG_PATH')
    browser = SourceBrowser(load_config(**({'path': Path(config_path)} if config_path else {}), require_runnable=False))
    try:
        page = browser.launch(snapshot['source'])
        # Retry only transport/server failures; never retry login/access challenges.
        for attempt in range(2):
            if hasattr(browser, 'human_delay'): browser.human_delay()
            try:
                response = page.goto(snapshot['url'], wait_until='domcontentloaded', timeout=30000)
            except BrowserError as exc:
                if attempt == 0:
                    page.wait_for_timeout(500)  # Let Chromium finish its error-page navigation.
                    continue
                raise IntegrationError('网络连接或导航超时；请检查网络后重试或粘贴 JD。') from exc
            status = response.status if response else 0
            check_access(page, snapshot['source'], status)
            if status >= 500 or not status:
                if attempt == 0: continue
                raise IntegrationError(f'详情服务器异常 HTTP {status}，请稍后重试或粘贴 JD。')
            if status >= 400:
                raise IntegrationError(f'详情不可用 HTTP {status}，请打开原链接核对或粘贴 JD。')
            if urlsplit(page.url).hostname not in hosts[snapshot['source']]:
                raise IntegrationError('详情重定向至其他网站，停止自动读取；请打开原链接核对。')
            return wait_description(page, snapshot)
    finally:
        browser.close()
