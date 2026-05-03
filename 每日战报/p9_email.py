"""P9 email helpers."""

from __future__ import annotations

import smtplib
import html as html_lib
from datetime import datetime
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr


ALLOWED_EMAIL_TAGS = frozenset({
    'a',
    'blockquote',
    'br',
    'code',
    'em',
    'h1',
    'h2',
    'h3',
    'h4',
    'hr',
    'li',
    'ol',
    'p',
    'pre',
    'strong',
    'table',
    'tbody',
    'td',
    'th',
    'thead',
    'tr',
    'ul',
})
ALLOWED_EMAIL_ATTRIBUTES = {
    'a': ['href', 'title'],
    'td': ['colspan', 'rowspan'],
    'th': ['colspan', 'rowspan'],
}
ALLOWED_EMAIL_PROTOCOLS = ['http', 'https', 'mailto']


def _render_safe_report_html(content: str) -> str:
    import bleach
    import markdown

    rendered = markdown.markdown(content, extensions=['tables', 'fenced_code'])
    return bleach.clean(
        rendered,
        tags=ALLOWED_EMAIL_TAGS,
        attributes=ALLOWED_EMAIL_ATTRIBUTES,
        protocols=ALLOWED_EMAIL_PROTOCOLS,
        strip=True,
    )


def build_report_email_message(content: str, *, sender_email: str, receiver_email: str, now: datetime | None = None) -> MIMEText:
    now = now or datetime.now()
    try:
        html = _render_safe_report_html(content)
        today_fmt = now.strftime('%Y年%m月%d日 %H:%M')
        html_full = f'''<html><head><meta charset="utf-8"><style>
            * {{margin:0;padding:0;box-sizing:border-box}}
            body {{font-family:-apple-system,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif;line-height:1.9;color:#1a1a2e;padding:0;background:#f5f6fa;font-size:15px}}
            .wrap {{max-width:960px;margin:0;padding:24px}}
            h1 {{font-size:22px;color:#0f3460;border-bottom:3px solid #e94560;padding-bottom:8px;margin:28px 0 16px}}
            h2 {{font-size:19px;color:#16213e;border-left:4px solid #e94560;padding-left:12px;margin:24px 0 14px}}
            h3 {{font-size:16px;color:#533483;margin:18px 0 10px}}
            p {{margin:10px 0;font-size:15px}}
            ul,ol {{padding-left:24px;margin:10px 0}}
            li {{margin:5px 0;font-size:15px}}
            table {{border-collapse:collapse;width:100%;font-size:13px;margin:14px 0;background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.06)}}
            th {{background:linear-gradient(135deg,#0f3460,#16213e);color:#fff;font-weight:600;padding:10px 10px;text-align:left;font-size:12px;white-space:nowrap}}
            td {{padding:9px 10px;border-bottom:1px solid #eef2f7;font-size:13px}}
            tr:nth-child(even) {{background:#f8fafc}}
            tr:hover {{background:#e8f4fd}}
            blockquote {{border-left:4px solid #e94560;padding:14px 18px;background:linear-gradient(135deg,#fff5f5,#fff);margin:14px 0;border-radius:0 8px 8px 0;font-size:15px}}
            strong {{color:#e94560}}
            code {{background:#f0f2f5;padding:2px 6px;border-radius:4px;font-size:14px;color:#e94560}}
            hr {{border:none;height:1px;background:linear-gradient(90deg,transparent,#dee2e6,transparent);margin:18px 0}}
            .hdr {{background:linear-gradient(135deg,#0f3460,#533483);color:#fff;padding:24px;border-radius:12px;margin-bottom:24px;text-align:center}}
            .hdr h1 {{color:#fff;border:none;margin:0;font-size:24px;letter-spacing:1px}}
            .hdr p {{color:rgba(255,255,255,0.8);margin:6px 0 0;font-size:14px;border:none}}
            .ftr {{text-align:center;color:#999;font-size:12px;margin-top:28px;padding:16px 0;border-top:1px solid #eee}}
        </style></head><body>
        <div class="wrap">
        <div class="hdr"><h1>🎯 VCP Hunter AI 产业链战报</h1><p>{today_fmt} · 北美产业链情报 · 自动生成</p></div>
        {html}
        <div class="ftr">VCP Hunter v4.1 · 数据来源: Grok / RSS / Gemini / Opus / 通达信 · 仅供研究参考，不构成投资建议</div>
        </div>
        </body></html>'''
        msg = MIMEText(html_full, 'html', 'utf-8')
    except ImportError:
        msg = MIMEText(f"<pre>{html_lib.escape(content)}</pre>", 'html', 'utf-8')

    msg['From'] = formataddr(('VCP Hunter', sender_email))
    msg['To'] = receiver_email
    msg['Subject'] = Header(f"【AI产业链战报】{now.strftime('%m月%d日 %H:%M')}", 'utf-8')
    return msg


def send_report_via_email(content: str, *, sender_email: str, sender_password: str, receiver_email: str, now: datetime | None = None) -> None:
    msg = build_report_email_message(
        content,
        sender_email=sender_email,
        receiver_email=receiver_email,
        now=now,
    )
    with smtplib.SMTP_SSL('smtp.qq.com', 465) as s:
        s.set_debuglevel(0)
        s.login(sender_email, sender_password)
        s.sendmail(sender_email, [e.strip() for e in receiver_email.split(',')], msg.as_string())
