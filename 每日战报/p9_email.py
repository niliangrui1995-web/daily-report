"""P9 email helpers."""

from __future__ import annotations

import smtplib
import html as html_lib
import json
import logging
import socket
import ssl
import urllib.request
from contextlib import contextmanager
from datetime import datetime
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr
from ipaddress import ip_address, ip_network


SMTP_HOST = 'smtp.qq.com'
SMTP_PORT = 465
SMTP_TIMEOUT_SECONDS = 80
FAKE_IP_NETWORKS = (
    ip_network('198.18.0.0/15'),
    ip_network('172.19.0.0/16'),
)
REAL_DNS_ENDPOINTS = (
    'https://cloudflare-dns.com/dns-query?name=smtp.qq.com&type=A',
    'https://dns.google/resolve?name=smtp.qq.com&type=A',
    'https://dns.alidns.com/resolve?name=smtp.qq.com&type=A',
)


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
        <div class="ftr">VCP Hunter v4.1 · 数据来源: Grok / RSS / Gemini / GPT-5.5 XHigh / 通达信 · 仅供研究参考，不构成投资建议</div>
        </div>
        </body></html>'''
        msg = MIMEText(html_full, 'html', 'utf-8')
    except ImportError:
        msg = MIMEText(f"<pre>{html_lib.escape(content)}</pre>", 'html', 'utf-8')

    msg['From'] = formataddr(('VCP Hunter', sender_email))
    msg['To'] = receiver_email
    msg['Subject'] = Header(f"【AI产业链战报】{now.strftime('%m月%d日 %H:%M')}", 'utf-8')
    return msg


def _split_receivers(receiver_email: str) -> list[str]:
    return [email.strip() for email in receiver_email.split(',') if email.strip()]


def _is_fake_ip(value: str) -> bool:
    try:
        addr = ip_address(value)
    except ValueError:
        return False
    return any(addr in network for network in FAKE_IP_NETWORKS)


def _is_usable_source_ip(value: str) -> bool:
    try:
        addr = ip_address(value)
    except ValueError:
        return False
    if addr.version != 4:
        return False
    if addr.is_loopback or addr.is_link_local or addr.is_multicast or addr.is_unspecified:
        return False
    if _is_fake_ip(value):
        return False
    return addr.is_private


def _candidate_source_ips() -> list[str]:
    candidates: list[str] = []
    seen: set[str] = set()

    def add(value: str) -> None:
        if value in seen or not _is_usable_source_ip(value):
            return
        seen.add(value)
        candidates.append(value)

    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, family=socket.AF_INET):
            add(info[4][0])
    except OSError:
        pass

    try:
        for value in socket.gethostbyname_ex(socket.gethostname())[2]:
            add(value)
    except OSError:
        pass

    return candidates


def _resolve_real_smtp_ips() -> list[str]:
    resolved: list[str] = []
    seen: set[str] = set()

    def add(value: str) -> None:
        if value in seen:
            return
        try:
            addr = ip_address(value)
        except ValueError:
            return
        if addr.version != 4 or _is_fake_ip(value):
            return
        seen.add(value)
        resolved.append(value)

    try:
        for info in socket.getaddrinfo(SMTP_HOST, SMTP_PORT, family=socket.AF_INET, proto=socket.IPPROTO_TCP):
            add(info[4][0])
    except OSError:
        pass

    headers = {'accept': 'application/dns-json', 'user-agent': 'VCPHunter-Mailer/1.0'}
    for endpoint in REAL_DNS_ENDPOINTS:
        try:
            request = urllib.request.Request(endpoint, headers=headers)
            with urllib.request.urlopen(request, timeout=12) as response:
                payload = json.loads(response.read().decode('utf-8'))
            for answer in payload.get('Answer', []) or []:
                if int(answer.get('type', 0)) == 1:
                    add(str(answer.get('data', '')).strip())
        except Exception as exc:
            logging.warning("[P9] real DNS lookup failed via %s: %s", endpoint, exc)

    return resolved


@contextmanager
def _patched_smtp_dns(remote_ip: str):
    original_getaddrinfo = socket.getaddrinfo

    def patched_getaddrinfo(host, port, *args, **kwargs):
        if host == SMTP_HOST:
            return original_getaddrinfo(remote_ip, port, *args, **kwargs)
        return original_getaddrinfo(host, port, *args, **kwargs)

    socket.getaddrinfo = patched_getaddrinfo
    try:
        yield
    finally:
        socket.getaddrinfo = original_getaddrinfo


def _send_message(
    msg: MIMEText,
    *,
    sender_email: str,
    sender_password: str,
    receiver_email: str,
    remote_ip: str | None = None,
    source_ip: str | None = None,
) -> None:
    source_address = (source_ip, 0) if source_ip else None

    def send() -> None:
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(
            SMTP_HOST,
            SMTP_PORT,
            timeout=SMTP_TIMEOUT_SECONDS,
            source_address=source_address,
            context=context,
        ) as s:
            s.set_debuglevel(0)
            s.login(sender_email, sender_password)
            s.sendmail(sender_email, _split_receivers(receiver_email), msg.as_string())

    if remote_ip:
        with _patched_smtp_dns(remote_ip):
            send()
    else:
        send()


def send_report_via_email(content: str, *, sender_email: str, sender_password: str, receiver_email: str, now: datetime | None = None) -> None:
    msg = build_report_email_message(
        content,
        sender_email=sender_email,
        receiver_email=receiver_email,
        now=now,
    )
    try:
        _send_message(
            msg,
            sender_email=sender_email,
            sender_password=sender_password,
            receiver_email=receiver_email,
        )
        return
    except smtplib.SMTPAuthenticationError:
        raise
    except Exception as primary_exc:
        logging.warning("[P9] primary SMTP path failed, trying TUN-safe fallback: %s", primary_exc)

    real_smtp_ips = _resolve_real_smtp_ips()
    last_exc: Exception | None = None
    for source_ip in _candidate_source_ips():
        for remote_ip in real_smtp_ips:
            try:
                logging.info("[P9] retrying SMTP via source=%s remote=%s", source_ip, remote_ip)
                _send_message(
                    msg,
                    sender_email=sender_email,
                    sender_password=sender_password,
                    receiver_email=receiver_email,
                    remote_ip=remote_ip,
                    source_ip=source_ip,
                )
                return
            except smtplib.SMTPAuthenticationError:
                raise
            except Exception as exc:
                last_exc = exc
                logging.warning("[P9] SMTP fallback failed via source=%s remote=%s: %s", source_ip, remote_ip, exc)

    if last_exc:
        raise last_exc
    raise RuntimeError("SMTP delivery failed and no TUN-safe route candidates were available")
