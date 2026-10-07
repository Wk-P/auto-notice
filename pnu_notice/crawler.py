from __future__ import annotations

import hashlib
import html
import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import PurePosixPath
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup

from .config import Settings
from .db import Database
from .http import HttpClient
from .sources import Source
from .timeutil import SEOUL, iso_utc, now_utc, parse_datetime


NOTICE_ID_RE = re.compile(r"/(\d+)/artclView\.do|(?:nttId|articleNo|bbsId)=(\d+)", re.I)
DATE_RE = re.compile(r"(20\d{2})[.\-/]\s*(\d{1,2})[.\-/]\s*(\d{1,2})(?:\s+(\d{1,2}):(\d{2})(?::(\d{2}))?)?")
FILE_RE = re.compile(r"\.(pdf|docx?|xlsx?|txt|hwp|hwpx|pptx?|zip)(?:$|[?#])", re.I)
BLOCK_TAGS = ["p", "div", "li", "tr", "table", "ul", "ol", "section", "article", "blockquote", "pre",
              "h1", "h2", "h3", "h4", "h5", "h6", "dd", "dt"]
# Existing notices seen again in RSS are re-fetched for change detection at most this often.
DETAIL_RECHECK = timedelta(hours=6)
# A notice first seen by polling but published this long before backfill finished was missed by the
# backfill, not newly posted; it is stored as historical so it is never mailed as new.
HISTORICAL_MARGIN = timedelta(days=1)


@dataclass
class DiscoveredNotice:
    external_id: str
    title: str
    url: str
    published_at: datetime | None = None
    is_pinned: bool = False


@dataclass
class Attachment:
    filename: str
    url: str
    extension: str


@dataclass
class NoticeDetail:
    external_id: str
    title: str
    url: str
    author: str | None
    published_at: datetime | None
    category: str | None
    raw_html: str
    clean_text: str
    attachments: list[Attachment] = field(default_factory=list)
    external_links: list[str] = field(default_factory=list)

    @property
    def content_hash(self) -> str:
        body = re.sub(r"\s+", " ", self.clean_text).strip()
        media = "\n".join(sorted(set(re.findall(r'''(?:src|href)=["']([^"']+)["']''', self.raw_html, re.I))))
        files = "\n".join(sorted(f"{a.filename}|{a.url}" for a in self.attachments))
        payload = f"{self.title.strip()}\n{body}\n{media}\n{files}".encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


def external_id(url: str, guid: str | None = None) -> str:
    for value in (url, guid or ""):
        match = NOTICE_ID_RE.search(value)
        if match:
            return next(group for group in match.groups() if group)
    stable = guid or url
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()[:24]


def _rss_text(item: ET.Element, name: str) -> str:
    for child in item:
        if child.tag.rsplit("}", 1)[-1].lower() == name.lower():
            return (child.text or "").strip()
    return ""


def parse_rss(xml: str, base_url: str = "") -> list[DiscoveredNotice]:
    root = ET.fromstring(xml)
    result: list[DiscoveredNotice] = []
    for item in root.iter():
        if item.tag.rsplit("}", 1)[-1].lower() not in ("item", "entry"):
            continue
        title = html.unescape(_rss_text(item, "title"))
        if "{" not in title:
            title = title.rstrip("} ")  # the school RSS appends a stray "}" to every title
        url = _rss_text(item, "link")
        if not url:
            link_node = next((child for child in item if child.tag.rsplit("}", 1)[-1] == "link"), None)
            url = link_node.attrib.get("href", "") if link_node is not None else ""
        guid = _rss_text(item, "guid") or _rss_text(item, "id")
        date = _rss_text(item, "pubDate") or _rss_text(item, "published") or _rss_text(item, "date")
        if title and url:
            absolute_url = urljoin(base_url, url)
            result.append(DiscoveredNotice(external_id(absolute_url, guid), title, absolute_url, parse_datetime(date)))
    return result


def list_url(source: Source, page: int) -> str:
    parsed = urlparse(source.rss_url)
    parts = parsed.path.strip("/").split("/")
    site = parts[1]
    path = f"/bbs/{site}/{source.board_id}/artclList.do"
    return urlunparse((parsed.scheme, parsed.netloc, path, "", urlencode({"page": page}), ""))


def parse_list_page(source: Source, document: str) -> list[DiscoveredNotice]:
    soup = BeautifulSoup(document, "html.parser")
    result: list[DiscoveredNotice] = []
    seen: set[str] = set()
    for row in soup.select("tr"):
        link = row.find("a", href=re.compile(r"artclView\.do"))
        if not link:
            continue
        url = urljoin(source.page_url, link.get("href", ""))
        notice_id = external_id(url)
        if notice_id in seen:
            continue
        cells = [cell.get_text(" ", strip=True) for cell in row.find_all(["td", "th"])]
        if not cells:
            continue
        number = cells[0].replace(" ", "")
        is_pinned = not number.isdigit()
        date_match = DATE_RE.search(" ".join(cells))
        published = parse_datetime(date_match.group(0)) if date_match else None
        result.append(DiscoveredNotice(notice_id, link.get_text(" ", strip=True), url, published, is_pinned))
        seen.add(notice_id)
    return result


def _metadata(soup: BeautifulSoup, labels: tuple[str, ...]) -> str | None:
    for item in soup.select(".detail li"):
        label = item.find(["span", "strong", "dt"])
        if label and any(candidate in label.get_text(" ", strip=True) for candidate in labels):
            value = re.sub(r"\s+", " ", item.get_text(" ", strip=True))
            label_text = re.sub(r"\s+", " ", label.get_text(" ", strip=True))
            value = value.removeprefix(label_text).strip(" :：")
            if value:
                return value
    for node in soup.find_all(string=True):
        text = " ".join(str(node).split())
        if any(label in text for label in labels):
            parent = node.parent
            sibling = parent.find_next_sibling() if parent else None
            if sibling:
                value = sibling.get_text(" ", strip=True)
                if value:
                    return value
            match = re.search(r"(?::|：)\s*(.+)$", text)
            if match:
                return match.group(1).strip()
    return None


def _clean_text(node) -> str:
    for br in node.find_all("br"):
        br.replace_with("\n")
    for cell in node.find_all(["td", "th"]):
        cell.append(" ")
    for block in node.find_all(BLOCK_TAGS):
        block.append("\n")
    lines = (re.sub(r"[ \t\u00a0\u200b]+", " ", line).strip() for line in node.get_text().splitlines())
    return "\n".join(line for line in lines if line)


def parse_detail(source: Source, url: str, document: str, fallback: DiscoveredNotice | None = None) -> NoticeDetail:
    soup = BeautifulSoup(document, "html.parser")
    root = soup.select_one(".board-view, .artclView, article, main") or soup
    title_node = soup.select_one(".board-view .title strong, .artclViewTitle, .view-title, .board-view-title")
    if not title_node:
        title_node = soup.find(["h1", "h2", "h3"])
    if title_node and title_node.name == "input":
        title = title_node.get("value", "").strip()
    else:
        title = title_node.get_text(" ", strip=True) if title_node else (fallback.title if fallback else "")
    title = re.sub(r"\s+", " ", title).strip()
    content = soup.select_one(".board-view .txt, .artclView .txt, .view-con, .view-content, .board-view-content, .bbs_view, .artclViewCon")
    if not content:
        content = root
    if not content:
        raise ValueError(f"DOM structure changed: notice content not found for {url}")
    raw_html = str(content)
    for unwanted in content.select("script, style, nav, button"):
        unwanted.decompose()
    attachments: list[Attachment] = []
    external_links: list[str] = []
    seen_files: set[str] = set()
    links = list(content.find_all("a", href=True))
    links.extend(root.select(".attachment a[href]"))
    for link in links:
        absolute = urljoin(url, link["href"])
        name = link.get_text(" ", strip=True) or PurePosixPath(urlparse(absolute).path).name
        if "download.do" in absolute or FILE_RE.search(absolute) or FILE_RE.search(name):
            if absolute not in seen_files:
                suffix = PurePosixPath(name.split("?", 1)[0]).suffix.lower().lstrip(".")
                if not suffix:
                    match = FILE_RE.search(absolute)
                    suffix = match.group(1).lower() if match else "unknown"
                attachments.append(Attachment(name, absolute, suffix))
                seen_files.add(absolute)
        elif urlparse(absolute).netloc and absolute != url:
            external_links.append(absolute)
    clean_text = _clean_text(content)
    if len(clean_text) < 10 and not attachments and not content.find("img"):
        raise ValueError(f"Parsed notice body is unexpectedly empty for {url}")
    full_text = soup.get_text(" ", strip=True)
    date_match = DATE_RE.search(_metadata(soup, ("작성일", "등록일", "发布时间")) or full_text)
    published = parse_datetime(date_match.group(0)) if date_match else (fallback.published_at if fallback else None)
    if (published and fallback and fallback.published_at
            and published.astimezone(SEOUL).date() == fallback.published_at.astimezone(SEOUL).date()):
        published = fallback.published_at  # the detail page shows only the date; RSS also carries the time
    category = _metadata(root, ("분류", "카테고리"))
    if not category:
        category_match = re.match(r"\s*\[([^\]]+)\]", title) or re.search(r"분류\s*[:：]?\s*([가-힣A-Za-z]+)", full_text)
        category = category_match.group(1) if category_match else None
    author = _metadata(root, ("작성자", "作者"))
    return NoticeDetail(external_id(url), title, url, author, published, category, raw_html,
                        clean_text, attachments, list(dict.fromkeys(external_links)))


class Crawler:
    def __init__(self, db: Database, settings: Settings, http: HttpClient | None = None):
        self.db = db
        self.settings = settings
        self.http = http or HttpClient(settings.http_timeout_seconds, settings.requests_per_second)

    def fetch_detail(self, source: Source, item: DiscoveredNotice) -> NoticeDetail:
        response = self.http.request(item.url)
        return parse_detail(source, item.url, response.text, item)

    def save(self, source: Source, detail: NoticeDetail, historical: bool) -> tuple[str, int]:
        now = iso_utc()
        with self.db.transaction() as conn:
            existing = conn.execute(
                "SELECT * FROM notices WHERE source_key=? AND external_notice_id=?",
                (source.key, detail.external_id),
            ).fetchone()
            if existing and existing["content_hash"] == detail.content_hash:
                conn.execute("UPDATE notices SET last_checked_at=? WHERE id=?", (now, existing["id"]))
                return "unchanged", existing["id"]
            if existing:
                conn.execute(
                    "INSERT OR IGNORE INTO notice_revisions(notice_id,old_hash,new_hash,detected_at,old_raw_html) VALUES(?,?,?,?,?)",
                    (existing["id"], existing["content_hash"], detail.content_hash, now, existing["raw_html"]),
                )
                conn.execute(
                    """UPDATE notices SET original_title=?,original_url=?,author=?,published_at=?,category_original=?,
                       raw_html=?,clean_text=?,external_links_json=?,updated_at=?,last_checked_at=?,content_hash=?,
                       ai_status='pending',processing_status='ai_pending',ai_attempts=0,ai_next_attempt_at=NULL WHERE id=?""",
                    (detail.title, detail.url, detail.author, iso_utc(detail.published_at) if detail.published_at else None,
                     detail.category, detail.raw_html, detail.clean_text, json.dumps(detail.external_links), now, now,
                     detail.content_hash, existing["id"]),
                )
                notice_id, state = existing["id"], "updated"
            else:
                cursor = conn.execute(
                    """INSERT INTO notices(source_key,external_notice_id,original_title,original_url,author,published_at,
                       category_original,raw_html,clean_text,external_links_json,created_at,updated_at,last_checked_at,
                       historical_import,content_hash,ai_status,processing_status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (source.key, detail.external_id, detail.title, detail.url, detail.author,
                     iso_utc(detail.published_at) if detail.published_at else None, detail.category, detail.raw_html,
                     detail.clean_text, json.dumps(detail.external_links), now, now, now, int(historical),
                     detail.content_hash, "pending", "ai_pending"),
                )
                notice_id, state = cursor.lastrowid, "new"
            conn.execute("DELETE FROM attachments WHERE notice_id=?", (notice_id,))
            conn.executemany(
                "INSERT INTO attachments(notice_id,filename,url,extension) VALUES(?,?,?,?)",
                [(notice_id, item.filename, item.url, item.extension) for item in detail.attachments],
            )
            return state, notice_id

    def run_incremental(self, source: Source) -> dict[str, int]:
        return self._run(source, "incremental", historical=False)

    def run_backfill(self, source: Source) -> dict[str, int]:
        boundary = parse_datetime(self.settings.backfill_start)
        if not boundary:
            raise ValueError("BACKFILL_START is invalid")

        def discover() -> list[DiscoveredNotice]:
            rss_items = parse_rss(self.http.request(source.rss_url).text, source.page_url)
            collected = {item.external_id: item for item in rss_items
                         if not item.published_at or item.published_at >= boundary}
            oldest = min((item.published_at for item in rss_items if item.published_at), default=None)
            if oldest is None or oldest > boundary:
                page = 1
                while True:
                    page_items = parse_list_page(source, self.http.request(list_url(source, page)).text)
                    regular = [item for item in page_items if not item.is_pinned and item.published_at]
                    for item in page_items:
                        if item.published_at and item.published_at >= boundary:
                            collected.setdefault(item.external_id, item)
                    if regular and max(item.published_at for item in regular) < boundary:
                        break
                    if not page_items or page >= 1000:
                        break
                    page += 1
            return list(collected.values())

        return self._run(source, "backfill", historical=True, discover=discover)

    def _historical_cutoff(self, source: Source) -> datetime | None:
        with self.db.connect() as conn:
            row = conn.execute("SELECT backfill_completed_at FROM sources WHERE source_key=?", (source.key,)).fetchone()
        if not row or not row["backfill_completed_at"]:
            return None
        return datetime.fromisoformat(row["backfill_completed_at"]) - HISTORICAL_MARGIN

    def _known(self, source: Source, item: DiscoveredNotice):
        with self.db.connect() as conn:
            return conn.execute("SELECT id,last_checked_at FROM notices WHERE source_key=? AND external_notice_id=?",
                                (source.key, item.external_id)).fetchone()

    def _run(self, source: Source, run_type: str, historical: bool, discover=None) -> dict[str, int]:
        started = iso_utc()
        with self.db.transaction() as conn:
            run_id = conn.execute(
                "INSERT INTO crawl_runs(source_key,run_type,started_at,status) VALUES(?,?,?,'running')",
                (source.key, run_type, started),
            ).lastrowid
        stats = {"seen": 0, "new": 0, "updated": 0, "unchanged": 0, "skipped": 0, "errors": 0}
        try:
            if discover:
                items = discover()
            else:
                items = parse_rss(self.http.request(source.rss_url).text, source.page_url)
            cutoff = None if historical else self._historical_cutoff(source)
            for item in items:
                stats["seen"] += 1
                known = self._known(source, item)
                # Backfill never needs to refresh stored notices; polling re-checks them periodically for edits.
                if known and (historical or now_utc() - datetime.fromisoformat(known["last_checked_at"]) < DETAIL_RECHECK):
                    stats["skipped"] += 1
                    continue
                try:
                    detail = self.fetch_detail(source, item)
                    # Old posts are sometimes edited and bumped for a new term; the RSS/list date then reflects
                    # the bump while the detail page keeps the original date, so the later of the two counts.
                    dates = [value for value in (detail.published_at, item.published_at) if value]
                    as_historical = historical or cutoff is None or (bool(dates) and max(dates) < cutoff)
                    state, _ = self.save(source, detail, as_historical)
                    stats[state] += 1
                except Exception as exc:
                    stats["errors"] += 1
                    with self.db.transaction() as conn:
                        conn.execute("INSERT INTO job_failures(component,source_key,error_message,occurred_at) VALUES(?,?,?,?)",
                                     ("crawler_detail", source.key, f"{item.url}: {exc}"[:2000], iso_utc()))
            status = "completed" if not stats["errors"] else "partial"
            with self.db.transaction() as conn:
                conn.execute("""UPDATE crawl_runs SET finished_at=?,status=?,items_seen=?,new_items=?,updated_items=?,error_message=?
                                WHERE id=?""", (iso_utc(), status, stats["seen"], stats["new"], stats["updated"],
                                                f"{stats['errors']} detail failures" if stats["errors"] else None, run_id))
                # Detail failures do not block the switch to polling: those notices reappear in RSS and are
                # then stored as historical by the cutoff above.
                if historical:
                    conn.execute("UPDATE sources SET backfill_completed_at=? WHERE source_key=?", (iso_utc(), source.key))
            return stats
        except Exception as exc:
            with self.db.transaction() as conn:
                conn.execute("UPDATE crawl_runs SET finished_at=?,status='failed',error_message=? WHERE id=?",
                             (iso_utc(), str(exc)[:2000], run_id))
                conn.execute("INSERT INTO job_failures(component,source_key,error_message,occurred_at) VALUES(?,?,?,?)",
                             ("crawler", source.key, str(exc)[:2000], iso_utc()))
            raise
