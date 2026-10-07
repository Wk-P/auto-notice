from __future__ import annotations

import json
from datetime import datetime, timedelta

from .config import Settings
from .db import Database
from .http import HttpClient
from .timeutil import SEOUL, iso_utc, now_utc, parse_datetime

CATEGORIES = ["academic", "course", "graduation", "thesis", "scholarship", "visa", "immigration",
              "employment", "dormitory", "insurance", "research", "competition", "event",
              "administration", "international", "safety", "other"]
AUDIENCES = ["undergraduate", "master", "phd", "graduate", "international_student",
             "prospective_student", "graduating_student", "chinese_student", "specific_nationality",
             "all_students"]

DEADLINE_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "type": {"type": "string"},
        "datetime": {"type": ["string", "null"]},
        "timezone": {"type": "string", "enum": ["Asia/Seoul"]},
        "original_text": {"type": "string"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": ["type", "datetime", "timezone", "original_text", "confidence"],
}

ANALYSIS_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "title_zh": {"type": "string"},
        "summary_zh": {"type": "string"},
        "categories": {"type": "array", "items": {"type": "string", "enum": CATEGORIES}},
        "audience": {"type": "array", "items": {"type": "string", "enum": AUDIENCES}},
        "action_required": {"type": "boolean"},
        "actions": {"type": "array", "items": {"type": "string"}},
        "deadlines": {"type": "array", "items": DEADLINE_SCHEMA},
        "eligibility": {"type": "array", "items": {"type": "string"}},
        "required_documents": {"type": "array", "items": {"type": "string"}},
        "importance": {"type": "string", "enum": ["critical", "high", "normal", "low"]},
        "delivery_priority": {"type": "string", "enum": ["immediate", "daily", "weekly"]},
        "warnings": {"type": "array", "items": {"type": "string"}},
        "status": {"type": "string", "enum": ["active", "upcoming", "expired", "closed", "informational"]},
        "attachment_requires_review": {"type": "boolean"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": ["title_zh", "summary_zh", "categories", "audience", "action_required", "actions",
                 "deadlines", "eligibility", "required_documents", "importance", "delivery_priority",
                 "warnings", "status", "attachment_requires_review", "confidence"],
}

SYSTEM_PROMPT = """你是釜山大学公告信息提取器。只使用所提供的官方公告正文和附件文本。
禁止依靠常识补全签证政策、毕业条件、奖学金资格或所需材料。不确定时使用空数组、null 或保守标签。
公告发布日期不是截止日期。日期必须保留 original_text，并将可确定的时间转换为带时区的 ISO 8601。
若出现 마감、종료 或 Closed，结合当前时间判断状态。未解析附件可能含要求时 attachment_requires_review=true。
摘要使用准确中文，约100至300字。输出是辅助分析，所有结论必须能回到原文核实。"""


def validate_analysis(value: object) -> dict:
    if not isinstance(value, dict) or set(value) != set(ANALYSIS_SCHEMA["required"]):
        raise ValueError("analysis fields do not match schema")
    for key in ("title_zh", "summary_zh", "importance", "delivery_priority", "status"):
        if not isinstance(value[key], str):
            raise ValueError(f"{key} must be a string")
    for key in ("categories", "audience", "actions", "deadlines", "eligibility", "required_documents", "warnings"):
        if not isinstance(value[key], list):
            raise ValueError(f"{key} must be an array")
    if any(item not in CATEGORIES for item in value["categories"]):
        raise ValueError("invalid category")
    if any(item not in AUDIENCES for item in value["audience"]):
        raise ValueError("invalid audience")
    if value["importance"] not in ("critical", "high", "normal", "low"):
        raise ValueError("invalid importance")
    if value["delivery_priority"] not in ("immediate", "daily", "weekly"):
        raise ValueError("invalid delivery_priority")
    if value["status"] not in ("active", "upcoming", "expired", "closed", "informational"):
        raise ValueError("invalid status")
    if not isinstance(value["action_required"], bool) or not isinstance(value["attachment_requires_review"], bool):
        raise ValueError("boolean field has wrong type")
    if not isinstance(value["confidence"], (int, float)) or not 0 <= value["confidence"] <= 1:
        raise ValueError("confidence out of range")
    for deadline in value["deadlines"]:
        if not isinstance(deadline, dict) or set(deadline) != set(DEADLINE_SCHEMA["required"]):
            raise ValueError("invalid deadline object")
        if deadline["timezone"] != "Asia/Seoul" or not isinstance(deadline["original_text"], str):
            raise ValueError("invalid deadline timezone or original_text")
        if deadline["datetime"] is not None and parse_datetime(deadline["datetime"]) is None:
            # An unparseable date only loses its machine-readable time; the original wording is kept for readers
            # and no reminder is scheduled from a guess.
            deadline["datetime"] = None
    return value


MAX_AI_ATTEMPTS = 4
QUOTA_PAUSE = timedelta(hours=1)
FALLBACK_MODEL = "fallback-no-ai"


def fallback_analysis(notice: dict, attachments: list[dict]) -> dict:
    """Used when AI is unavailable: only the official title and an excerpt of the body, nothing inferred."""
    excerpt = " ".join(notice["clean_text"].split())[:300]
    summary = f"AI 摘要暂不可用，以下为原文开头：{excerpt}" if excerpt else "AI 摘要暂不可用，正文可能为图片，请查看学校原公告。"
    return validate_analysis({
        "title_zh": notice["original_title"], "summary_zh": summary, "categories": [], "audience": [],
        "action_required": False, "actions": [], "deadlines": [], "eligibility": [], "required_documents": [],
        "importance": "normal", "delivery_priority": "daily",
        "warnings": ["本条未经 AI 整理，请直接查看学校原公告。"], "status": "informational",
        "attachment_requires_review": bool(attachments), "confidence": 0.0,
    })


class OpenAIAnalyzer:
    def __init__(self, settings: Settings, http: HttpClient | None = None):
        self.settings = settings
        self.http = http or HttpClient(settings.http_timeout_seconds, 10)

    def analyze(self, notice: dict, attachments: list[dict]) -> dict:
        if not self.settings.openai_api_key:
            raise RuntimeError("OPENAI_API_KEY is not configured")
        attachment_context = []
        for item in attachments:
            attachment_context.append({"filename": item["filename"], "extension": item["extension"],
                                       "text": item.get("extracted_text")})
        user_input = json.dumps({
            "current_time": datetime.now(SEOUL).isoformat(),
            "official_url": notice["original_url"],
            "published_at": notice["published_at"],
            "original_title": notice["original_title"],
            "category_original": notice["category_original"],
            "body": notice["clean_text"],
            "attachments": attachment_context,
        }, ensure_ascii=False)
        payload = {
            "model": self.settings.openai_model,
            "instructions": SYSTEM_PROMPT,
            "input": user_input,
            "text": {"format": {"type": "json_schema", "name": "pnu_notice_analysis", "strict": True,
                                "schema": ANALYSIS_SCHEMA}},
        }
        response = self.http.request(
            "https://api.openai.com/v1/responses", method="POST", json_body=payload,
            headers={"Authorization": f"Bearer {self.settings.openai_api_key}"}, retries=2,
        )
        result = json.loads(response.text)
        if result.get("status") != "completed":
            raise RuntimeError(f"AI response status was {result.get('status')}")
        output_text = result.get("output_text")
        if not output_text:
            for output in result.get("output", []):
                for content in output.get("content", []):
                    if content.get("type") == "output_text":
                        output_text = content.get("text")
                        break
        if not output_text:
            raise RuntimeError("AI response did not contain output text")
        analysis = validate_analysis(json.loads(output_text))
        if any(not item.get("text") for item in attachment_context):
            analysis["attachment_requires_review"] = True
        return analysis


class AIWorker:
    def __init__(self, db: Database, analyzer: OpenAIAnalyzer, delivery_planner=None):
        self.db = db
        self.analyzer = analyzer
        self.delivery_planner = delivery_planner

    def process_pending(self, limit: int = 25) -> dict[str, int]:
        stats = {"completed": 0, "retrying": 0, "failed": 0}
        now = iso_utc()
        with self.db.connect() as conn:
            rows = conn.execute(
                """SELECT * FROM notices WHERE ai_status IN ('pending','retrying')
                   AND (ai_next_attempt_at IS NULL OR ai_next_attempt_at<=?) ORDER BY created_at LIMIT ?""",
                (now, limit)).fetchall()
        for row in rows:
            notice = dict(row)
            with self.db.connect() as conn:
                attachments = [dict(item) for item in conn.execute(
                    "SELECT filename,extension,extracted_text FROM attachments WHERE notice_id=?", (row["id"],))]
            error = None
            analysis = None
            model = self.analyzer.settings.openai_model
            if self.analyzer.settings.openai_api_key:
                for _ in range(2):
                    try:
                        analysis = self.analyzer.analyze(notice, attachments)
                        break
                    except Exception as exc:
                        error = exc
            else:
                error = RuntimeError("OPENAI_API_KEY is not configured")
            if analysis is None and "insufficient_quota" in str(error):
                # The account is out of credit: every remaining notice would fail the same way. Pause the queue
                # without spending attempts, so notices resume with real AI analysis once credit is added.
                with self.db.transaction() as conn:
                    conn.execute("INSERT INTO job_failures(component,source_key,error_message,occurred_at) VALUES(?,?,?,?)",
                                 ("ai_quota", None, str(error)[:2000], iso_utc()))
                    conn.execute("""UPDATE notices SET ai_status='retrying',ai_next_attempt_at=?
                                    WHERE ai_status IN ('pending','retrying')""", (iso_utc(now_utc() + QUOTA_PAUSE),))
                stats["paused_for_quota"] = 1
                break
            if analysis is None:
                attempts = row["ai_attempts"] + 1
                with self.db.transaction() as conn:
                    if self.analyzer.settings.openai_api_key:
                        conn.execute("INSERT INTO job_failures(component,source_key,error_message,occurred_at) VALUES(?,?,?,?)",
                                     ("ai", row["source_key"], str(error)[:2000], iso_utc()))
                    if self.analyzer.settings.openai_api_key and attempts < MAX_AI_ATTEMPTS:
                        retry_at = iso_utc(now_utc() + timedelta(minutes=5 * 2 ** attempts))
                        conn.execute("""UPDATE notices SET ai_status='retrying',ai_attempts=?,ai_next_attempt_at=?
                                        WHERE id=?""", (attempts, retry_at, row["id"]))
                        stats["retrying"] += 1
                        continue
                stats["failed"] += 1
                analysis, model, ai_status = fallback_analysis(notice, attachments), FALLBACK_MODEL, "failed"
            else:
                attempts, ai_status = row["ai_attempts"] + 1, "completed"
                stats["completed"] += 1
            with self.db.transaction() as conn:
                conn.execute("INSERT OR REPLACE INTO ai_analyses(notice_id,content_hash,result_json,model,created_at) VALUES(?,?,?,?,?)",
                             (row["id"], row["content_hash"], json.dumps(analysis, ensure_ascii=False), model, iso_utc()))
                conn.execute("""UPDATE notices SET ai_status=?,ai_attempts=?,ai_next_attempt_at=NULL,
                                processing_status='delivery_pending' WHERE id=?""", (ai_status, attempts, row["id"]))
                conn.execute("UPDATE deadlines SET active=0 WHERE notice_id=?", (row["id"],))
                for deadline in analysis["deadlines"]:
                    when = parse_datetime(deadline["datetime"])
                    if when:
                        conn.execute("""INSERT INTO deadlines(notice_id,kind,deadline_at,timezone,original_text,confidence)
                                        VALUES(?,?,?,?,?,?) ON CONFLICT(notice_id,kind,deadline_at) DO UPDATE SET
                                        active=1,original_text=excluded.original_text,confidence=excluded.confidence""",
                                     (row["id"], deadline["type"], iso_utc(when), "Asia/Seoul",
                                      deadline["original_text"], deadline["confidence"]))
            if self.delivery_planner:
                try:
                    self.delivery_planner.plan_notice(row["id"], analysis)
                except Exception as exc:
                    with self.db.transaction() as conn:
                        conn.execute("INSERT INTO job_failures(component,source_key,error_message,occurred_at) VALUES(?,?,?,?)",
                                     ("delivery_planner", row["source_key"], str(exc)[:2000], iso_utc()))
        return stats
