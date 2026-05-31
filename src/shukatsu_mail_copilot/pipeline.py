"""Core pipeline for Shukatsu Mail Copilot.

This module contains the Apple Mail ingestion flow, LLM extraction,
normalization, routing decisions, CSV persistence, and optional Notion sync.
The implementation is intentionally self-contained so contributors can inspect
the end-to-end workflow from one entrypoint.
"""

from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from textwrap import dedent
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from openai import OpenAI
import pandas as pd
import requests


PACKAGE_DIR = Path(__file__).resolve().parent
ROOT_DIR = PACKAGE_DIR.parents[1]
DATA_DIR = ROOT_DIR / "data"
ENV_FILE = ROOT_DIR / ".env"
CSV_FILE = DATA_DIR / "shukatsu_table.csv"
MOVE_LOG_FILE = DATA_DIR / "move_log.jsonl"
SAFE_MOVE_REPORT_FILE = DATA_DIR / "safe_move_report.md"
JAPAN_TZ = ZoneInfo("Asia/Tokyo")
NOTION_API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2026-03-11"
MAIL_SEPARATOR_SENDER = "\n---SENDER---\n"
MAIL_SEPARATOR_BODY = "\n---BODY---\n"
MAIL_SEPARATOR_DATE = "\n---DATE---\n"
MAIL_SEPARATOR_MESSAGE_ID = "\n---MESSAGE_ID---\n"
MAIL_SEPARATOR_MAILBOX = "\n---MAILBOX---\n"
MAIL_SEPARATOR_ACCOUNT = "\n---ACCOUNT---\n"
MAIL_RECORD_SEPARATOR = "\n---MAIL_RECORD---\n"
DEFAULT_STATUS = "未対応"
LEGACY_CATEGORY_OPTIONS = ("説明会", "インターン", "本選考", "ES提出", "面談", "その他")
TRIAGE_CATEGORY_OPTIONS = (
    "important",
    "action_required",
    "deadline_related",
    "expired",
    "ignore",
    "info",
)
SAFE_MOVE_TARGETS = {
    "important": "AI_review",
    "action_required": "AI_review",
    "deadline_related": "AI_deadline",
    "expired": "AI_expired",
    "ignore": "AI_lowpriority",
}
PROTECTION_KEYWORDS = (
    "学校",
    "university",
    "大学",
    "銀行",
    "bank",
    "visa",
    "ビザ",
    "签证",
    "支払",
    "支払い",
    "payment",
    "lab",
    "研究室",
    "実験室",
    "实验室",
    "导师",
    "教員",
    "先生",
    "教授",
    "supervisor",
    "advisor",
)
PROTECTED_SENDERS = (
    "ac.jp",
    ".edu",
    "university",
    "bank",
    "visa",
    "prof",
    "lab",
)
NOTION_REQUIRED_PROPERTIES = {
    "company_name": "title",
    "position": "rich_text",
    "summary_zh": "rich_text",
    "sender": "rich_text",
    "category": "rich_text",
    "status": "rich_text",
    "deadline": "rich_text",
    "mail_id": "rich_text",
    "importance": "rich_text",
    "extracted_at": "rich_text",
    "action": "rich_text",
    "mail_category": "rich_text",
    "reason": "rich_text",
}
OPTIONAL_NOTION_FIELDS = (
    "summary",
    "triage_category",
    "priority",
    "action_needed",
    "next_action",
    "confidence",
    "job_category",
    "mail_subject",
)


class UserFacingError(Exception):
    pass


def ensure_data_dir():
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def now_jst():
    return datetime.now(JAPAN_TZ)


def notify(title, message):
    if os.getenv("SHUKATSU_NOTIFICATIONS", "0") != "1":
        return
    safe_title = title.replace('"', '\\"')
    safe_message = message.replace('"', '\\"')
    subprocess.run(
        [
            "osascript",
            "-e",
            f'display notification "{safe_message}" with title "{safe_title}"',
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def run_osascript(script):
    return subprocess.run(
        ["osascript", "-e", script],
        capture_output=True,
        text=True,
        check=False,
    )


def escape_applescript(value):
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def build_mail_text(subject, sender, content):
    return f"{subject}{MAIL_SEPARATOR_SENDER}{sender}{MAIL_SEPARATOR_BODY}{content}"


def build_mail_record(subject, sender, body, received_at="", message_id="", mailbox_name="", account_name=""):
    subject = subject.strip()
    sender = sender.strip()
    body = body.strip()
    received_at = received_at.strip()
    message_id = message_id.strip()
    mailbox_name = mailbox_name.strip()
    account_name = account_name.strip()
    return {
        "subject": subject,
        "sender": sender,
        "body": body,
        "date": received_at,
        "message_id": message_id,
        "apple_mail_id": message_id,
        "mailbox_name": mailbox_name,
        "account_name": account_name,
        "mail_text": build_mail_text(subject, sender, body),
    }


def parse_mail_record_text(raw_item):
    required_separators = (
        MAIL_SEPARATOR_SENDER,
        MAIL_SEPARATOR_DATE,
        MAIL_SEPARATOR_MESSAGE_ID,
        MAIL_SEPARATOR_MAILBOX,
        MAIL_SEPARATOR_ACCOUNT,
        MAIL_SEPARATOR_BODY,
    )
    if not all(separator in raw_item for separator in required_separators):
        raise UserFacingError("Selected mail could not be parsed.")

    subject, rest = raw_item.split(MAIL_SEPARATOR_SENDER, 1)
    sender, rest = rest.split(MAIL_SEPARATOR_DATE, 1)
    received_at, rest = rest.split(MAIL_SEPARATOR_MESSAGE_ID, 1)
    message_id, rest = rest.split(MAIL_SEPARATOR_MAILBOX, 1)
    mailbox_name, rest = rest.split(MAIL_SEPARATOR_ACCOUNT, 1)
    account_name, body = rest.split(MAIL_SEPARATOR_BODY, 1)
    return build_mail_record(subject, sender, body, received_at, message_id, mailbox_name, account_name)


def get_selected_mail_text():
    record = get_selected_mail_record()
    return record["mail_text"]


def get_file_mail_record(path_text):
    path = Path(path_text).expanduser().resolve()
    if not path.exists():
        raise UserFacingError(f"Sample mail file was not found: {path}")
    body = path.read_text(encoding="utf-8")
    subject = path.stem.replace("_", " ").strip() or "Sample Mail"
    return build_mail_record(subject, "sample@example.com", body, mailbox_name="local_file", account_name="local_demo")


def get_selected_mail_record():
    script = '''
tell application "Mail"
    set selectedMessages to selection

    if selectedMessages is not {} then
        set theMessage to item 1 of selectedMessages
        set theSubject to subject of theMessage
        set theSender to sender of theMessage
        set theDate to date received of theMessage as string
        set theContent to content of theMessage
        set theMailboxName to ""
        set theAccountName to ""
        try
            set theMailboxName to name of mailbox of theMessage
        end try
        try
            set theAccountName to name of account of mailbox of theMessage
        end try
        try
            set theMessageId to message id of theMessage
        on error
            set theMessageId to ""
        end try

        return theSubject & "\\n---SENDER---\\n" & theSender & "\\n---DATE---\\n" & theDate & "\\n---MESSAGE_ID---\\n" & theMessageId & "\\n---MAILBOX---\\n" & theMailboxName & "\\n---ACCOUNT---\\n" & theAccountName & "\\n---BODY---\\n" & theContent
    else
        return "No selected message"
    end if
end tell
'''

    result = run_osascript(script)
    if result.returncode != 0:
        raise UserFacingError(
            "Could not read the selected Apple Mail message. "
            "Please make sure Mail is open and automation permission is allowed."
        )

    raw_item = result.stdout.strip()
    if not raw_item or raw_item == "No selected message":
        raise UserFacingError("No selected mail. Please select one mail in Apple Mail first.")

    return parse_mail_record_text(raw_item)


def get_mailbox_mail_records(mailbox_name):
    safe_mailbox = escape_applescript(mailbox_name)
    script = f'''
tell application "Mail"
    set mailboxName to "{safe_mailbox}"
    set targetMailbox to missing value

    repeat with acc in every account
        try
            set targetMailbox to first mailbox of acc whose name is mailboxName
            exit repeat
        end try
    end repeat

    if targetMailbox is missing value then
        repeat with mbox in every mailbox
            try
                if name of mbox is mailboxName then
                    set targetMailbox to mbox
                    exit repeat
                end if
            end try
        end repeat
    end if

    if targetMailbox is missing value then
        return "MAILBOX_NOT_FOUND"
    end if

    set outputText to ""
    repeat with theMessage in every message of targetMailbox
        set theSubject to subject of theMessage
        set theSender to sender of theMessage
        set theDate to date received of theMessage as string
        set theContent to content of theMessage
        set theMailboxName to ""
        set theAccountName to ""
        try
            set theMailboxName to name of mailbox of theMessage
        end try
        try
            set theAccountName to name of account of mailbox of theMessage
        end try
        try
            set theMessageId to message id of theMessage
        on error
            set theMessageId to ""
        end try

        set itemText to theSubject & "\\n---SENDER---\\n" & theSender & "\\n---DATE---\\n" & theDate & "\\n---MESSAGE_ID---\\n" & theMessageId & "\\n---MAILBOX---\\n" & theMailboxName & "\\n---ACCOUNT---\\n" & theAccountName & "\\n---BODY---\\n" & theContent
        if outputText is "" then
            set outputText to itemText
        else
            set outputText to outputText & "\\n---MAIL_RECORD---\\n" & itemText
        end if
    end repeat

    return outputText
end tell
'''

    result = run_osascript(script)
    if result.returncode != 0:
        raise UserFacingError(
            "Could not read the configured Apple Mail mailbox. "
            "Please make sure Mail is open and automation permission is allowed."
        )

    output = result.stdout.strip()
    if output == "MAILBOX_NOT_FOUND":
        raise UserFacingError(
            f'Apple Mail mailbox "{mailbox_name}" was not found. '
            "Please check APPLE_MAIL_SOURCE_MAILBOX in .env."
        )

    if not output:
        return []

    raw_items = [item.strip() for item in output.split(MAIL_RECORD_SEPARATOR) if item.strip()]
    records = []
    for raw_item in raw_items:
        try:
            records.append(parse_mail_record_text(raw_item))
        except UserFacingError:
            print("Skipped one mailbox message because it could not be parsed.")

    return records


def build_prompt(mail_text):
    now = now_jst()
    today = now.strftime("%Y-%m-%d")
    current_time = now.strftime("%Y-%m-%d %H:%M")
    return f"""
あなたは日本の就職活動メールを整理するAIです。

以下のメールが就職活動に関係あるか判断し、
関係ある場合のみ情報を抽出してください。

現在時刻は日本標準時 JST (UTC+9) の {current_time} です。
本日の日付は {today} です。
必ずJSON形式だけで出力してください。
```json などの記号や説明文は不要です。

JSON項目:
- is_shukatsu: true/false
- company_name: 企業名。不明なら空文字
- position: 職種・イベント名。不明なら空文字
- category: 説明会 / インターン / 本選考 / ES提出 / 面談 / その他
- mail_category: important / action_required / deadline_related / expired / ignore / info
- reason: 分類理由を中国語で一文
- summary: 中国語で80字以内の簡潔な要約
- summary_zh: summary と同じ内容
- priority: 1-5 の整数
- importance: high / middle / low
- action_needed: true/false
- next_action: ユーザーが次に取るべき行動を中国語で具体的に。不明なら空文字
- action: next_action と同じ内容
- deadline: YYYY-MM-DD または YYYY-MM-DD HH:MM。明確でなければ空文字
- confidence: 0 から 1 の数値

分類ルール:
- important: 重要度が高く、慎重に対応すべきメール。面接、合格後の必須対応、重要な案内、参加予定イベントの重要詳細など
- action_required: 返信、提出、予約、入力、支払い、確認など明確な行動が必要
- deadline_related: 締切や開催時刻の情報が中心で、把握しておく価値が高い
- expired: 締切や開催時刻が本日以前で、すでに過ぎている、または期限切れと明記されている
- ignore: 広告、無関係、重複通知、ノイズ
- info: 参考情報だが今すぐの対応は不要

補足:
- action_needed が false の場合、next_action と action は空文字にしてください
- 就活と無関係な場合は {{"is_shukatsu": false}} だけを返してください
- 中国語要約では、受信者の姓名から性別を推定しないでください
- 「様」を自動的に「先生」「小姐」へ変換しないでください
- 要約は可能な限り中性的で客観的な文体にしてください
- どうしても受信者への呼称が必要な場合のみ、「女士」を使用してください
- 「先生」は使用しないでください

メール:
{mail_text}
"""


def strip_json_fence(text):
    return text.replace("```json", "").replace("```", "").strip()


def extract_json(client, mail_text):
    response = client.chat.completions.create(
        model=get_model_name(),
        messages=[{"role": "user", "content": build_prompt(mail_text)}],
    )

    json_text = strip_json_fence(response.choices[0].message.content.strip())
    try:
        return json.loads(json_text)
    except json.JSONDecodeError as exc:
        raise UserFacingError("The model returned invalid JSON. Please try again.") from exc


def normalize_text(value, default=""):
    if value is None:
        return default
    text = str(value).strip()
    if not text or text.lower() in {"none", "null", "nan", "n/a", "unknown", "不明"}:
        return default
    return text


def normalize_bool(value, default=False):
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)

    text = str(value).strip().lower()
    if text in {"true", "1", "yes", "y", "required", "needed", "need"}:
        return True
    if text in {"false", "0", "no", "n", "not needed", "none"}:
        return False
    return default


def normalize_job_category(value):
    text = normalize_text(value)
    mapping = {
        "説明会": "説明会",
        "セミナー": "説明会",
        "イベント": "説明会",
        "ワークショップ": "説明会",
        "インターン": "インターン",
        "サマーインターン": "インターン",
        "本選考": "本選考",
        "選考": "本選考",
        "ES提出": "ES提出",
        "es提出": "ES提出",
        "エントリーシート": "ES提出",
        "面談": "面談",
        "面接": "面談",
    }
    if text in LEGACY_CATEGORY_OPTIONS:
        return text
    return mapping.get(text, "その他")


def normalize_deadline(value):
    text = normalize_text(value)
    if not text:
        return ""

    parsed = parse_deadline_datetime(text)
    if not parsed:
        return text

    if parsed.hour == 0 and parsed.minute == 0 and parsed.second == 0:
        return parsed.strftime("%Y-%m-%d")
    return parsed.strftime("%Y-%m-%d %H:%M")


def parse_deadline_datetime(text):
    normalized = normalize_text(text)
    if not normalized:
        return None

    formats = (
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%dT%H:%M:%S",
        "%Y/%m/%d",
        "%Y/%m/%d %H:%M",
        "%Y/%m/%d %H:%M:%S",
        "%Y-%m-%d",
    )
    for fmt in formats:
        try:
            return datetime.strptime(normalized, fmt)
        except ValueError:
            pass

    match = re.search(
        r"(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日(?:\([^)]+\))?(?:\s*(\d{1,2})[:時]\s*(\d{1,2})(?:[:分]\s*(\d{1,2}))?)?",
        normalized,
    )
    if match:
        year, month, day, hour, minute, second = match.groups()
        return datetime(
            int(year),
            int(month),
            int(day),
            int(hour or 0),
            int(minute or 0),
            int(second or 0),
        )
    return None


def is_past_deadline(deadline_text):
    parsed = parse_deadline_datetime(deadline_text)
    if not parsed:
        return False
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", deadline_text):
        return parsed.date() < now_jst().date()
    return parsed < now_jst().replace(tzinfo=None)


def infer_triage_category(action_needed, deadline, priority):
    if deadline and is_past_deadline(deadline):
        return "expired"
    if action_needed:
        return "important" if priority >= 5 else "action_required"
    if deadline:
        return "deadline_related"
    if priority <= 1:
        return "ignore"
    return "info"


def normalize_mail_category(value, action_needed, deadline, priority):
    if deadline and is_past_deadline(deadline):
        return "expired"
    text = normalize_text(value).lower().replace("-", "_").replace(" ", "_")
    mapping = {
        "important": "important",
        "urgent": "important",
        "action_required": "action_required",
        "needs_action": "action_required",
        "reply_required": "action_required",
        "deadline_related": "deadline_related",
        "deadline": "deadline_related",
        "schedule": "deadline_related",
        "expired": "expired",
        "closed": "expired",
        "ignore": "ignore",
        "spam": "ignore",
        "promotion": "ignore",
        "promotional": "ignore",
        "info": "info",
        "information": "info",
    }
    if text in mapping:
        return mapping[text]
    return infer_triage_category(action_needed, deadline, priority)


def normalize_priority(value, importance="", triage_category="info"):
    if isinstance(value, (int, float)):
        return max(1, min(5, int(round(value))))
    text = normalize_text(value).lower()
    if text.isdigit():
        return max(1, min(5, int(text)))

    importance_text = normalize_text(importance).lower()
    if importance_text == "high":
        return 5 if triage_category == "important" else 4
    if importance_text == "middle":
        return 3
    if importance_text == "low":
        return 1 if triage_category in {"ignore", "expired"} else 2

    fallback = {
        "important": 5,
        "action_required": 4,
        "deadline_related": 3,
        "info": 2,
        "ignore": 1,
        "expired": 1,
    }
    return fallback.get(triage_category, 3)


def normalize_importance(value, priority):
    text = normalize_text(value).lower()
    if text in {"high", "middle", "low"}:
        return text
    if priority >= 4:
        return "high"
    if priority == 3:
        return "middle"
    return "low"


def normalize_confidence(value):
    if isinstance(value, (int, float)):
        confidence = float(value)
    else:
        text = normalize_text(value)
        if not text:
            return 0.75
        if text.endswith("%"):
            try:
                confidence = float(text[:-1]) / 100
            except ValueError:
                return 0.75
        else:
            try:
                confidence = float(text)
            except ValueError:
                return 0.75

    if confidence > 1:
        confidence = confidence / 100 if confidence <= 100 else 1
    return round(max(0.0, min(1.0, confidence)), 2)


def normalize_extracted_data(raw_data, mail_record):
    if normalize_bool(raw_data.get("is_shukatsu"), default=False) is not True:
        return {"is_shukatsu": False}

    summary = normalize_text(raw_data.get("summary")) or normalize_text(raw_data.get("summary_zh"))
    summary_zh = normalize_text(raw_data.get("summary_zh")) or summary
    deadline = normalize_deadline(raw_data.get("deadline"))
    next_action = normalize_text(raw_data.get("next_action")) or normalize_text(raw_data.get("action"))
    inferred_action_needed = bool(next_action)

    provisional_priority = normalize_priority(
        raw_data.get("priority"),
        importance=raw_data.get("importance", ""),
        triage_category=normalize_text(raw_data.get("mail_category")).lower(),
    )
    action_needed = normalize_bool(raw_data.get("action_needed"), default=inferred_action_needed)
    triage_category = normalize_mail_category(
        raw_data.get("mail_category"),
        action_needed=action_needed,
        deadline=deadline,
        priority=provisional_priority,
    )
    priority = normalize_priority(
        raw_data.get("priority"),
        importance=raw_data.get("importance", ""),
        triage_category=triage_category,
    )
    importance = normalize_importance(raw_data.get("importance"), priority)

    if triage_category in {"important", "action_required"} and not action_needed:
        action_needed = True
    if triage_category in {"ignore", "expired"}:
        action_needed = False
        next_action = ""
    if not action_needed:
        next_action = ""

    job_category = normalize_job_category(raw_data.get("category"))
    reason = normalize_text(raw_data.get("reason"))
    if not reason:
        reason = f"分類結果為 {triage_category}，請結合摘要判斷後續是否需要處理。"

    extracted_at = now_jst().strftime("%Y-%m-%d %H:%M")
    subject = mail_record["subject"]
    sender = mail_record["sender"]
    apple_mail_id = normalize_text(mail_record.get("apple_mail_id") or mail_record.get("message_id"))
    mailbox_name = normalize_text(mail_record.get("mailbox_name"))
    account_name = normalize_text(mail_record.get("account_name"))
    mail_id = generate_mail_id(
        subject,
        sender,
        mail_record.get("date", ""),
        mail_record.get("message_id", ""),
    )

    return {
        "is_shukatsu": True,
        "company_name": normalize_text(raw_data.get("company_name")),
        "position": normalize_text(raw_data.get("position")),
        "summary": summary,
        "summary_zh": summary_zh,
        "category": job_category,
        "job_category": job_category,
        "triage_category": triage_category,
        "mail_category": triage_category,
        "priority": priority,
        "importance": importance,
        "action_needed": action_needed,
        "next_action": next_action,
        "action": next_action,
        "deadline": deadline,
        "confidence": normalize_confidence(raw_data.get("confidence")),
        "reason": reason,
        "mail_subject": subject,
        "sender": sender,
        "status": DEFAULT_STATUS,
        "extracted_at": extracted_at,
        "mail_id": mail_id,
        "apple_mail_id": apple_mail_id,
        "old_mailbox": mailbox_name,
        "account_name": account_name,
    }


def generate_mail_id(subject, sender, received_at="", message_id=""):
    if message_id:
        return message_id.strip()
    base = f"{sender.strip()}|{subject.strip()}|{received_at.strip()}"
    return hashlib.md5(base.encode("utf-8")).hexdigest()


def append_to_csv(data, mail_id):
    ensure_data_dir()
    new_df = pd.DataFrame([data])
    if CSV_FILE.exists():
        old_df = pd.read_csv(CSV_FILE)
        if "mail_id" in old_df.columns:
            existing_ids = old_df["mail_id"].dropna().astype(str)
            if mail_id in set(existing_ids):
                print("This mail already exists. Skipped.")
                return False
        combined_df = pd.concat([old_df, new_df], ignore_index=True, sort=False)
    else:
        combined_df = new_df
    combined_df.to_csv(CSV_FILE, index=False, encoding="utf-8-sig")
    return True


def notion_headers(api_key):
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Notion-Version": NOTION_VERSION,
    }


def notion_text(value):
    text = "" if value is None else str(value)
    return {"rich_text": [{"text": {"content": text[:2000]}}]} if text else {"rich_text": []}


def notion_title(value):
    text = "" if value is None else str(value)
    return {"title": [{"text": {"content": text[:2000]}}]} if text else {"title": []}


def notion_select(value):
    text = normalize_text(value)
    return {"select": {"name": text}} if text else {"select": None}


def notion_date(value):
    text = normalize_text(value)
    if not text:
        return {"date": None}
    parsed = parse_deadline_datetime(text)
    if not parsed:
        return {"date": None}
    return {"date": {"start": parsed.isoformat()}}


def notion_number(value):
    if value in (None, ""):
        return {"number": None}
    try:
        return {"number": float(value)}
    except (TypeError, ValueError):
        return {"number": None}


def notion_checkbox(value):
    return {"checkbox": normalize_bool(value)}


def notion_property_value(prop_type, value):
    if prop_type == "title":
        return notion_title(value)
    if prop_type == "rich_text":
        return notion_text(value)
    if prop_type == "date":
        return notion_date(value)
    if prop_type == "select":
        return notion_select(value)
    if prop_type == "number":
        return notion_number(value)
    if prop_type == "checkbox":
        return notion_checkbox(value)
    raise UserFacingError(f"Unsupported Notion property type: {prop_type}")


def debug_notion_schema(api_key, data_source_id):
    schema = notion_request("GET", f"/data_sources/{data_source_id}", api_key)
    properties = schema.get("properties", {})
    print("Notion data source properties:")
    for name, prop in properties.items():
        print(f"- {name}: {prop.get('type')}")
    return properties


def validate_notion_schema(schema_properties):
    missing = []
    mismatched = []
    for name, expected_type in NOTION_REQUIRED_PROPERTIES.items():
        actual_prop = schema_properties.get(name)
        if not actual_prop:
            missing.append(name)
            continue
        actual_type = actual_prop.get("type")
        if actual_type != expected_type:
            mismatched.append(f"{name}={actual_type} (expected {expected_type})")

    if missing or mismatched:
        details = []
        if missing:
            details.append(f"missing: {', '.join(missing)}")
        if mismatched:
            details.append(f"type mismatch: {', '.join(mismatched)}")
        raise UserFacingError(
            "Notion database schema does not match the current app settings: "
            + "; ".join(details)
        )


def notion_page_properties(data, schema_properties):
    field_values = {
        "company_name": data.get("company_name"),
        "position": data.get("position"),
        "category": data.get("category"),
        "mail_category": data.get("mail_category"),
        "reason": data.get("reason"),
        "deadline": data.get("deadline"),
        "importance": data.get("importance"),
        "status": data.get("status"),
        "action": data.get("action"),
        "summary_zh": data.get("summary_zh"),
        "sender": data.get("sender"),
        "extracted_at": data.get("extracted_at"),
        "mail_id": data.get("mail_id"),
    }
    for optional_name in OPTIONAL_NOTION_FIELDS:
        field_values[optional_name] = data.get(optional_name)

    properties = {}
    for name, value in field_values.items():
        schema_prop = schema_properties.get(name)
        if not schema_prop:
            continue
        try:
            properties[name] = notion_property_value(schema_prop.get("type"), value)
        except UserFacingError:
            print(f"Notion property skipped because of unsupported type: {name}")
    return properties


def notion_request(method, path, api_key, **kwargs):
    response = requests.request(
        method,
        f"{NOTION_API_BASE}{path}",
        headers=notion_headers(api_key),
        timeout=20,
        **kwargs,
    )
    if response.status_code >= 400:
        try:
            detail = response.json().get("message", response.text)
        except ValueError:
            detail = response.text
        raise UserFacingError(f"Notion API error ({response.status_code}): {detail}")
    return response.json()


def notion_mail_exists(api_key, data_source_id, mail_id):
    payload = {
        "filter": {"property": "mail_id", "rich_text": {"equals": mail_id}},
        "page_size": 1,
    }
    result = notion_request(
        "POST",
        f"/data_sources/{data_source_id}/query",
        api_key,
        json=payload,
    )
    return bool(result.get("results"))


def append_to_notion(data):
    api_key = os.getenv("NOTION_API_KEY")
    data_source_id = os.getenv("NOTION_DATA_SOURCE_ID")

    if not api_key and not data_source_id:
        print("Notion is not configured. CSV backup was updated only.")
        return False
    if not api_key or not data_source_id:
        raise UserFacingError("NOTION_API_KEY and NOTION_DATA_SOURCE_ID must both be set in .env.")

    schema_properties = debug_notion_schema(api_key, data_source_id)
    validate_notion_schema(schema_properties)
    if notion_mail_exists(api_key, data_source_id, data["mail_id"]):
        print("This mail already exists in Notion. Skipped Notion insert.")
        return False

    payload = {
        "parent": {"data_source_id": data_source_id},
        "properties": notion_page_properties(data, schema_properties),
    }
    notion_request("POST", "/pages", api_key, json=payload)
    return True


def protection_match(text, keywords):
    lowered = normalize_text(text).lower()
    return next((keyword for keyword in keywords if keyword.lower() in lowered), "")


def get_target_mailbox(data):
    category = data.get("mail_category")
    if category == "info":
        return "AI_lowpriority" if data.get("priority", 3) <= 2 else "AI_review"
    return SAFE_MOVE_TARGETS.get(category, "AI_review")


def evaluate_move_decision(data):
    subject = normalize_text(data.get("mail_subject"))
    sender = normalize_text(data.get("sender"))
    summary = normalize_text(data.get("summary"))
    next_action = normalize_text(data.get("next_action"))
    deadline = normalize_text(data.get("deadline"))
    category = data.get("mail_category", "info")
    confidence = float(data.get("confidence", 0))
    mailbox_name = normalize_text(data.get("old_mailbox"))

    reasons = []
    if confidence < 0.75:
        reasons.append(f"confidence {confidence:.2f} < 0.75")

    sender_keyword = protection_match(sender, PROTECTED_SENDERS)
    if sender_keyword:
        reasons.append(f"protected sender keyword: {sender_keyword}")

    subject_keyword = protection_match(subject, PROTECTION_KEYWORDS)
    if subject_keyword:
        reasons.append(f"protected subject keyword: {subject_keyword}")

    combined_context = " ".join(filter(None, [subject, summary, next_action]))
    context_keyword = protection_match(combined_context, PROTECTION_KEYWORDS)
    if context_keyword and f"protected subject keyword: {context_keyword}" not in reasons:
        reasons.append(f"protected content keyword: {context_keyword}")

    if not normalize_text(data.get("apple_mail_id")):
        reasons.append("missing Apple Mail message id")
    if not mailbox_name:
        reasons.append("missing source mailbox name")

    target_mailbox = get_target_mailbox(data)
    should_move = not reasons

    if should_move and mailbox_name == target_mailbox:
        should_move = False
        reasons.append("already in target mailbox")

    if should_move:
        decision_reason = f"safe auto move to {target_mailbox} based on category={category}, priority={data.get('priority')}, confidence={confidence:.2f}"
    else:
        decision_reason = "; ".join(reasons) if reasons else "kept for manual review"

    return {
        "should_move": should_move,
        "target_mailbox": target_mailbox,
        "reason": decision_reason,
        "protection_reasons": reasons,
    }


def ensure_target_mailbox(account_name, target_mailbox):
    safe_account = escape_applescript(account_name)
    safe_target = escape_applescript(target_mailbox)
    script = dedent(
        f'''
        tell application "Mail"
            set targetAccount to first account whose name is "{safe_account}"
            if not (exists mailbox "{safe_target}" of targetAccount) then
                error "TARGET_MAILBOX_NOT_FOUND"
            end if
            return "{safe_target}"
        end tell
        '''
    )
    result = run_osascript(script)
    if result.returncode != 0:
        raise UserFacingError(
            f'Could not find target mailbox "{target_mailbox}" in account "{account_name}". '
            'Please create it manually in Exchange first. Details: '
            + result.stderr.strip()
        )
    return result.stdout.strip() or target_mailbox


def move_message_to_mailbox(account_name, message_id, source_mailbox, target_mailbox):
    safe_account = escape_applescript(account_name)
    safe_message_id = escape_applescript(message_id)
    safe_source = escape_applescript(source_mailbox)
    safe_target = escape_applescript(target_mailbox)
    script = dedent(
        f'''
        tell application "Mail"
            set targetAccount to first account whose name is "{safe_account}"
            set sourceMailbox to mailbox "{safe_source}" of targetAccount
            set targetMailbox to mailbox "{safe_target}" of targetAccount
            set targetMessage to first message of sourceMailbox whose message id is "{safe_message_id}"
            move targetMessage to targetMailbox
            return "OK"
        end tell
        '''
    )
    result = run_osascript(script)
    if result.returncode != 0:
        raise UserFacingError(
            "Could not move Apple Mail message: "
            + result.stderr.strip()
            + f" | account={account_name} source={source_mailbox} target={target_mailbox}"
        )
    return result.stdout.strip()


def append_move_log(entry):
    ensure_data_dir()
    with MOVE_LOG_FILE.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def read_move_log_entries():
    if not MOVE_LOG_FILE.exists():
        return []
    entries = []
    for line in MOVE_LOG_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


def render_safe_move_report(results):
    ensure_data_dir()
    lines = [
        "# Safe Move Report",
        "",
        f"Generated at: {now_jst().strftime('%Y-%m-%d %H:%M:%S JST')}",
        "",
        "| Subject | Category | Confidence | Action | Target | Reason |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for result in results:
        lines.append(
            "| {subject} | {category} | {confidence:.2f} | {action} | {target} | {reason} |".format(
                subject=result["subject"].replace("|", "/"),
                category=result["category"],
                confidence=result["confidence"],
                action="moved" if result["moved"] else "kept",
                target=result["target_mailbox"] or "-",
                reason=result["reason"].replace("|", "/"),
            )
        )
    SAFE_MOVE_REPORT_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def process_mail_record(client, mail_record):
    raw_data = extract_json(client, mail_record["mail_text"])
    data = normalize_extracted_data(raw_data, mail_record)
    if data.get("is_shukatsu") is not True:
        print("This mail is not related to job hunting. Skipped.")
        return 0, None

    updated = append_to_csv(data, data["mail_id"])
    if updated:
        print("CSV UPDATED!")
        notion_updated = append_to_notion(data)
        if notion_updated:
            print("NOTION UPDATED!")
    else:
        print("CSV already had this mail. Reusing extracted data for routing/reporting.")

    print(json.dumps(data, ensure_ascii=False, indent=2))
    if data.get("importance") == "high":
        notify("重要就活メール", str(data.get("company_name", "")))
    return 1 if updated else 0, data


def classify_mailbox_records(client, mailbox_name):
    records = get_mailbox_mail_records(mailbox_name)
    print(f'Mailbox "{mailbox_name}" scan started. {len(records)} messages found.')
    processed_count = 0
    extracted_items = []
    for index, record in enumerate(records, start=1):
        print(f'Processing mailbox message {index}/{len(records)}: {record["subject"]}')
        added, data = process_mail_record(client, record)
        processed_count += added
        if data:
            extracted_items.append(data)
        print(f'Logged mailbox message {index}/{len(records)}. No deletion was performed.')
    print(f'Mailbox "{mailbox_name}" scan finished. {processed_count} messages were newly added.')
    return processed_count, extracted_items


def print_safe_move_result(result):
    print(f'Title: {result["subject"]}')
    print(f'Category: {result["category"]}')
    print(f'Confidence: {result["confidence"]:.2f}')
    print(f'Moved: {"yes" if result["moved"] else "no"}')
    print(f'Target: {result["target_mailbox"] or "-"}')
    print(f'Reason: {result["reason"]}')
    print("")


def run_classify_dry_run(client):
    mailbox_name = os.getenv("APPLE_MAIL_SOURCE_MAILBOX", "").strip()
    if not mailbox_name:
        raise UserFacingError("APPLE_MAIL_SOURCE_MAILBOX is missing in .env.")
    _, extracted_items = classify_mailbox_records(client, mailbox_name)
    results = []
    for data in extracted_items:
        decision = evaluate_move_decision(data)
        result = {
            "subject": data.get("mail_subject", ""),
            "category": data.get("mail_category", "info"),
            "confidence": float(data.get("confidence", 0)),
            "moved": False,
            "target_mailbox": decision["target_mailbox"],
            "reason": f'DRY RUN: {decision["reason"]}',
        }
        results.append(result)
        print_safe_move_result(result)
    render_safe_move_report(results)
    return 0


def run_safe_move(client):
    mailbox_name = os.getenv("APPLE_MAIL_SOURCE_MAILBOX", "").strip()
    if not mailbox_name:
        raise UserFacingError("APPLE_MAIL_SOURCE_MAILBOX is missing in .env.")

    _, extracted_items = classify_mailbox_records(client, mailbox_name)
    results = []
    for data in extracted_items:
        decision = evaluate_move_decision(data)
        moved = False
        reason = decision["reason"]
        if decision["should_move"]:
            target_mailbox = ensure_target_mailbox(data["account_name"], decision["target_mailbox"])
            move_message_to_mailbox(
                data["account_name"],
                data["apple_mail_id"],
                data["old_mailbox"],
                target_mailbox,
            )
            moved = True
            reason = decision["reason"]
            append_move_log(
                {
                    "timestamp": now_jst().strftime("%Y-%m-%d %H:%M:%S JST"),
                    "subject": data.get("mail_subject", ""),
                    "sender": data.get("sender", ""),
                    "mail_id": data.get("mail_id", ""),
                    "apple_mail_id": data.get("apple_mail_id", ""),
                    "account_name": data.get("account_name", ""),
                    "old_mailbox": data.get("old_mailbox", ""),
                    "new_mailbox": target_mailbox,
                    "category": data.get("mail_category", "info"),
                    "confidence": float(data.get("confidence", 0)),
                    "reason": reason,
                }
            )
        result = {
            "subject": data.get("mail_subject", ""),
            "category": data.get("mail_category", "info"),
            "confidence": float(data.get("confidence", 0)),
            "moved": moved,
            "target_mailbox": decision["target_mailbox"] if not moved else target_mailbox,
            "reason": reason,
        }
        results.append(result)
        print_safe_move_result(result)

    render_safe_move_report(results)
    return 0


def restore_entries(entries):
    if not entries:
        raise UserFacingError("No move log entries were found to restore.")

    restored = 0
    for entry in entries:
        old_mailbox = normalize_text(entry.get("old_mailbox"))
        new_mailbox = normalize_text(entry.get("new_mailbox"))
        account_name = normalize_text(entry.get("account_name"))
        apple_mail_id = normalize_text(entry.get("apple_mail_id"))
        if not old_mailbox or not new_mailbox or not apple_mail_id or not account_name:
            print(f'Skipped one restore entry because mailbox or apple_mail_id was missing: {entry.get("subject", "")}')
            continue
        ensure_target_mailbox(account_name, old_mailbox)
        move_message_to_mailbox(account_name, apple_mail_id, new_mailbox, old_mailbox)
        restored += 1
        print(f'Restored: {entry.get("subject", "")} -> {old_mailbox}')
    return restored


def run_undo_last_move():
    entries = read_move_log_entries()
    if not entries:
        raise UserFacingError("move_log.jsonl is empty. Nothing to undo.")
    last_entry = entries[-1]
    restored = restore_entries([last_entry])
    print(f"Undo completed. Restored {restored} message(s).")
    return 0


def run_restore_today():
    today = now_jst().strftime("%Y-%m-%d")
    entries = [
        entry
        for entry in read_move_log_entries()
        if normalize_text(entry.get("timestamp")).startswith(today)
    ]
    restored = restore_entries(entries)
    print(f"Today restore completed. Restored {restored} message(s).")
    return 0


def build_client():
    api_key = os.getenv("OPENAI_API_KEY") or os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        raise UserFacingError("OPENAI_API_KEY is missing in .env.")

    model = os.getenv("OPENAI_MODEL", "").strip()
    if not model:
        raise UserFacingError("OPENAI_MODEL is missing in .env.")

    base_url = os.getenv("OPENAI_BASE_URL", "").strip() or os.getenv("DEEPSEEK_BASE_URL", "").strip()
    if base_url:
        return OpenAI(api_key=api_key, base_url=base_url)
    return OpenAI(api_key=api_key)


def get_model_name():
    model = os.getenv("OPENAI_MODEL", "").strip()
    if not model:
        raise UserFacingError("OPENAI_MODEL is missing in .env.")
    return model


def main():
    os.chdir(ROOT_DIR)
    load_dotenv(ENV_FILE)

    mode = sys.argv[1] if len(sys.argv) > 1 else "selected"

    if mode in {"undo-last-move", "restore-today"}:
        if mode == "undo-last-move":
            return run_undo_last_move()
        return run_restore_today()

    client = build_client()

    if mode == "selected":
        processed_count, _ = process_mail_record(client, get_selected_mail_record())
        if processed_count:
            subprocess.run(["open", str(CSV_FILE)], check=False)
        return 0

    if mode == "file":
        if len(sys.argv) < 3:
            raise UserFacingError('File mode requires a path. Example: python -m shukatsu_mail_copilot file examples/sample_mail.txt')
        processed_count, _ = process_mail_record(client, get_file_mail_record(sys.argv[2]))
        if processed_count:
            print(f"Structured data written to {CSV_FILE}")
        return 0

    if mode == "mailbox":
        mailbox_name = os.getenv("APPLE_MAIL_SOURCE_MAILBOX", "").strip()
        if not mailbox_name:
            raise UserFacingError("APPLE_MAIL_SOURCE_MAILBOX is missing in .env.")
        processed_count, _ = classify_mailbox_records(client, mailbox_name)
        if processed_count:
            subprocess.run(["open", str(CSV_FILE)], check=False)
        return 0

    if mode == "classify-dry-run":
        return run_classify_dry_run(client)

    if mode == "safe-move":
        return run_safe_move(client)

    raise UserFacingError('Unsupported mode. Use "selected", "file", "mailbox", "classify-dry-run", "safe-move", "undo-last-move", or "restore-today".')


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except UserFacingError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        notify("就活メール抽出 エラー", str(exc))
        raise SystemExit(1)
    except Exception as exc:
        print(f"UNEXPECTED ERROR: {exc}", file=sys.stderr)
        notify("就活メール抽出 エラー", "Unexpected error. Check the terminal output for details.")
        raise SystemExit(1)
