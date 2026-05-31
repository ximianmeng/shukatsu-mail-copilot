from shukatsu_mail_copilot.pipeline import build_mail_text, build_prompt


def test_prompt_contains_mail_content():
    mail_text = build_mail_text("Interview Invitation", "hr@example.com", "Please reply by Friday.")
    prompt = build_prompt(mail_text)
    assert "Interview Invitation" in prompt
    assert "Please reply by Friday." in prompt
