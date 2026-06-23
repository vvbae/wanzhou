"""发信（Brevo 事务邮件）。没配 BREVO_API_KEY 时静默跳过，不影响主流程。"""

from __future__ import annotations

import os

import httpx

SENDER = {"name": "万轴", "email": "hello@wanzhoubooks.org"}


def send_email(to_list: list[str], subject: str, html: str) -> bool:
    key = os.environ.get("BREVO_API_KEY", "").strip()
    if not key or not to_list:
        return False
    try:
        r = httpx.post(
            "https://api.brevo.com/v3/smtp/email",
            headers={"api-key": key, "content-type": "application/json"},
            json={"sender": SENDER, "to": [{"email": e} for e in to_list],
                  "subject": subject, "htmlContent": html},
            timeout=15,
        )
        return r.status_code in (200, 201)
    except httpx.HTTPError:
        return False
