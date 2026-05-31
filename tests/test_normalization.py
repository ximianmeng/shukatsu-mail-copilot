from shukatsu_mail_copilot.pipeline import (
    infer_triage_category,
    normalize_deadline,
    normalize_mail_category,
    normalize_priority,
)


def test_normalize_deadline_japanese_date():
    assert normalize_deadline("2026年6月15日 18:00") == "2026-06-15 18:00"


def test_priority_falls_back_from_category():
    assert normalize_priority("", triage_category="important") == 5


def test_mail_category_keeps_expired_when_deadline_passed():
    category = normalize_mail_category("info", action_needed=False, deadline="2020-01-01", priority=1)
    assert category == "expired"


def test_infer_triage_category_action_required():
    assert infer_triage_category(action_needed=True, deadline="", priority=3) == "action_required"
