#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""QQ 邮箱 HTML 邮件发送脚本（逐封独立发送）。

功能：
    读取 config.ini 里的发件邮箱与授权码，读取一个 HTML 文件作为邮件正文，
    再把 recipients.txt 里的收件人一个一个单独发送（每封邮件只有一位收件人）。

用法：
    python send_mail.py                         # 使用同目录的 config.ini + recipients.txt
    python send_mail.py --dry-run               # 只校验配置和收件人，不真正发送
    python send_mail.py --html a.html --subject "通知"
    python send_mail.py --recipients other.txt --config other.ini --yes

仅使用标准库，无需 pip 安装任何依赖。
"""

from __future__ import annotations

import argparse
import configparser
import html as html_lib
import re
import base64
import smtplib
import socket
import ssl
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from email.utils import formataddr, parseaddr
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = BASE_DIR / "config.ini"
DEFAULT_RECIPIENTS_PATH = BASE_DIR / "recipients.txt"
LOG_PATH = BASE_DIR / "send_log.txt"
FAILED_PATH = BASE_DIR / "failed_recipients.txt"

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+$")
TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
PLACEHOLDER_EMAIL = "your_qq_number@qq.com"

CONFIG_TEMPLATE = """; ============================================================
; QQ 邮箱自动发送脚本 - 配置文件
; 注释以 ; 或 # 开头，可写在行首，也可以写在某一行值的后面。
; ============================================================

[smtp]
; QQ 邮箱固定为 smtp.qq.com
host = smtp.qq.com
; 使用 SSL 加密时端口为 465，使用 STARTTLS 时为 587
port = 465
; true = SSL(465)；false = STARTTLS(587)
use_ssl = true

[sender]
; 发件人 QQ 邮箱，必须与下面的授权码对应同一个账号
email = your_qq_number@qq.com
; QQ 邮箱「授权码」共 16 位，不是 QQ 登录密码！
; 获取方式：登录网页版 QQ 邮箱 -> 设置 -> 账户 -> 开启「POP3/SMTP 服务」-> 生成授权码
auth_code =
; 收件人看到的发件人名称，可留空，支持中文，例如：小明
display_name =

[mail]
; 作为邮件正文的 HTML 文件，相对路径以本配置文件所在目录为准
html_file = content.html
; 邮件主题，留空则自动读取 HTML 里的 <title>，再没有就用 HTML 文件名
subject =
; 每封邮件之间的间隔秒数，用于避免触发 QQ 的频率限制（建议 3 ~ 10 秒）
delay_seconds = 4
; 单封邮件发送失败后的重试次数
retry_times = 2
; 每次重试前的等待秒数（第 n 次重试等待 n 倍）
retry_wait_seconds = 15
"""


class ConfigError(Exception):
    """配置文件缺失或不合法。"""


class AuthFailed(Exception):
    """登录被服务器拒绝（或登录阶段被断开），继续发送无意义。"""


@dataclass
class Config:
    host: str
    port: int
    use_ssl: bool
    sender_email: str
    auth_code: str
    display_name: str
    html_file: Path
    subject: str
    delay_seconds: float
    retry_times: int
    retry_wait_seconds: float


# --------------------------------------------------------------------------- #
# 输出与日志
# --------------------------------------------------------------------------- #
def setup_console() -> None:
    """避免 Windows 控制台在输出中文/特殊字符时抛 UnicodeEncodeError。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError, OSError):
            pass


def say(message: str = "") -> None:
    print(message, flush=True)


def write_log(status: str, recipient: str, detail: str) -> None:
    """追加一行发送记录；日志里绝不写入授权码。"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    detail = " ".join(str(detail).split())
    try:
        with LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(f"{timestamp} | {status:<4} | {recipient} | {detail}\n")
    except OSError as exc:
        say(f"（警告：写入日志失败：{exc}）")


# --------------------------------------------------------------------------- #
# 配置文件
# --------------------------------------------------------------------------- #
def _read_option(parser: configparser.ConfigParser, section: str, option: str, default: str = "") -> str:
    if not parser.has_option(section, option):
        return default
    value = parser.get(section, option, raw=True).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1].strip()
    return value


def _read_int(parser: configparser.ConfigParser, section: str, option: str, default: int,
              minimum: int, maximum: int) -> int:
    raw = _read_option(parser, section, option, str(default))
    if not raw:
        return default
    try:
        value = int(float(raw))
    except ValueError:
        raise ConfigError(f"[{section}] {option} 必须是整数，当前为：{raw!r}") from None
    if not minimum <= value <= maximum:
        raise ConfigError(f"[{section}] {option} 应在 {minimum} ~ {maximum} 之间，当前为：{value}")
    return value


def _read_float(parser: configparser.ConfigParser, section: str, option: str, default: float,
                minimum: float, maximum: float) -> float:
    raw = _read_option(parser, section, option, str(default))
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ConfigError(f"[{section}] {option} 必须是数字，当前为：{raw!r}") from None
    if not minimum <= value <= maximum:
        raise ConfigError(f"[{section}] {option} 应在 {minimum} ~ {maximum} 之间，当前为：{value}")
    return value


def _read_bool(parser: configparser.ConfigParser, section: str, option: str, default: bool) -> bool:
    raw = _read_option(parser, section, option, "true" if default else "false").lower()
    if raw in ("1", "true", "yes", "y", "on"):
        return True
    if raw in ("0", "false", "no", "n", "off"):
        return False
    raise ConfigError(f"[{section}] {option} 只能填 true 或 false，当前为：{raw!r}")


def load_config(path: Path) -> Config:
    if not path.exists():
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(CONFIG_TEMPLATE, encoding="utf-8")
        except OSError as exc:
            raise ConfigError(f"配置文件不存在且无法生成模板：{path}（{exc}）") from None
        raise ConfigError(
            f"未找到配置文件，已生成模板：{path}\n"
            "请填写 [sender] 下的 email 与 auth_code 后重新运行。"
        )

    parser = configparser.ConfigParser(interpolation=None, inline_comment_prefixes=(";",))
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            parser.read_file(handle)
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"读取配置文件失败：{path}（{exc}）") from None
    except configparser.Error as exc:
        raise ConfigError(f"配置文件格式有误：{exc}") from None

    missing = [section for section in ("smtp", "sender", "mail") if not parser.has_section(section)]
    if missing:
        raise ConfigError(f"配置文件缺少段落：{', '.join('[' + s + ']' for s in missing)}")

    sender_email = _read_option(parser, "sender", "email")
    if not sender_email or sender_email == PLACEHOLDER_EMAIL:
        raise ConfigError("请先在 [sender] email 填上你自己的 QQ 邮箱（例如 12345678@qq.com）。")
    if not EMAIL_RE.match(sender_email):
        raise ConfigError(f"[sender] email 不是合法的邮箱地址：{sender_email!r}")

    auth_code = _read_option(parser, "sender", "auth_code")
    # 授权码可能从网页复制时带入空格、全角空格或换行
    auth_code = re.sub(r"[\s\u3000\u00a0\u200b\ufeff]+", "", auth_code)
    if not auth_code or auth_code.lower().startswith("your"):
        raise ConfigError(
            "[sender] auth_code 还没填（或仍是占位符）。\n"
            "请登录网页版 QQ 邮箱 -> 设置 -> 账户 -> 开启「POP3/SMTP 服务」-> 生成授权码，\n"
            "然后把 16 位授权码填到 auth_code（注意：不是 QQ 登录密码）。"
        )
    if len(auth_code) != 16 or not auth_code.isascii():
        say("警告：[sender] auth_code 看起来不是标准的 16 位 QQ 授权码"
            f"（当前 {len(auth_code)} 位，含非 ASCII 字符：{'是' if not auth_code.isascii() else '否'}）。")
        say("      正常授权码是 16 个英文字母，建议重新生成并整段复制。")
        say()

    html_value = _read_option(parser, "mail", "html_file", "content.html") or "content.html"
    html_file = Path(html_value)
    if not html_file.is_absolute():
        html_file = (path.parent / html_file).resolve()

    port = _read_int(parser, "smtp", "port", 465, 1, 65535)
    use_ssl = _read_bool(parser, "smtp", "use_ssl", True)

    return Config(
        host=_read_option(parser, "smtp", "host", "smtp.qq.com") or "smtp.qq.com",
        port=port,
        use_ssl=use_ssl,
        sender_email=sender_email,
        auth_code=auth_code,
        display_name=_read_option(parser, "sender", "display_name"),
        html_file=html_file,
        subject=_read_option(parser, "mail", "subject"),
        delay_seconds=_read_float(parser, "mail", "delay_seconds", 4.0, 0.0, 3600.0),
        retry_times=_read_int(parser, "mail", "retry_times", 2, 0, 10),
        retry_wait_seconds=_read_float(parser, "mail", "retry_wait_seconds", 15.0, 0.0, 600.0),
    )


# --------------------------------------------------------------------------- #
# HTML 与收件人
# --------------------------------------------------------------------------- #
def read_html(path: Path) -> str:
    if not path.exists():
        raise ConfigError(f"找不到 HTML 正文文件：{path}")
    if not path.is_file():
        raise ConfigError(f"HTML 正文路径不是文件：{path}")
    for encoding in ("utf-8-sig", "gbk"):
        try:
            text = path.read_text(encoding=encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = path.read_text(encoding="utf-8", errors="replace")
    if not text.strip():
        raise ConfigError(f"HTML 正文文件是空的：{path}")
    return text


def html_to_text(html_text: str) -> str:
    """生成纯文本兜底内容，供不支持 HTML 的客户端阅读。"""
    text = re.sub(r"(?is)<(script|style|head)\b.*?</\1>", " ", html_text)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(?:p|div|tr|h[1-6]|li|table)>", "\n", text)
    text = re.sub(r"<!--.*?-->", " ", text, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_lib.unescape(text)
    text = re.sub(r"[ \t\u00a0]+", " ", text)
    text = re.sub(r"\s*\n\s*", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_title(html_text: str) -> str:
    match = TITLE_RE.search(html_text)
    if not match:
        return ""
    title = re.sub(r"<[^>]+>", " ", match.group(1))
    title = " ".join(html_lib.unescape(title).split())
    return title


def parse_recipients(path: Path) -> tuple[list[str], list[tuple[int, str]]]:
    """返回 (合法收件人列表, [(行号, 原始内容), ...] 非法行)。"""
    if not path.exists():
        raise ConfigError(f"找不到收件人列表：{path}")
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        try:
            raw = path.read_text(encoding="gbk")
        except UnicodeDecodeError:
            raw = path.read_text(encoding="utf-8", errors="replace")

    valid: list[str] = []
    invalid: list[tuple[int, str]] = []
    seen: set[str] = set()

    for lineno, line in enumerate(raw.splitlines(), 1):
        item = line.strip()
        if not item or item.startswith(("#", ";")):
            continue
        # 兼容 "张三 <a@b.com>" 这种写法（只取地址部分）
        _, addr = parseaddr(item)
        addr = (addr or item).strip()
        if not EMAIL_RE.match(addr):
            invalid.append((lineno, item))
            continue
        key = addr.lower()
        if key in seen:
            say(f"（提示：第 {lineno} 行 {addr} 重复，已跳过）")
            continue
        seen.add(key)
        valid.append(addr)

    return valid, invalid


# --------------------------------------------------------------------------- #
# 发送
# --------------------------------------------------------------------------- #
def build_message(cfg: Config, subject: str, html_text: str, to_addr: str) -> EmailMessage:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = formataddr((cfg.display_name, cfg.sender_email))
    message["To"] = to_addr
    plain = html_to_text(html_text) or "本邮件为 HTML 格式，请使用支持 HTML 的邮件客户端查看。"
    message.set_content(plain, charset="utf-8")
    message.add_alternative(html_text, subtype="html", charset="utf-8")
    return message


def open_server(cfg: Config) -> smtplib.SMTP:
    """建立到 SMTP 服务器的连接（SSL 或 STARTTLS）。"""
    return connect(cfg.host, cfg.port, cfg.use_ssl)


AUTH_HINT = (
    "常见原因：\n"
    "  1. auth_code 填的不是授权码（QQ 邮箱要求 16 位授权码，不是登录密码）；\n"
    "  2. 该邮箱还没开启 SMTP 服务：网页版 QQ 邮箱 -> 设置 -> 账户 -> 开启「POP3/SMTP 服务」；\n"
    "  3. 授权码已被重置或失效，需重新生成；\n"
    "  4. 账号存在安全限制（异地登录/新设备验证），请登录网页版邮箱处理后再试；\n"
    "  5. 短时间内登录失败次数过多，被临时限流（通常是前 4 项之一引发的），需等 30 分钟以上再试。"
)

FREQUENCY_HINT = (
    "服务器明确提到「login frequency limited」，说明 535 的直接原因是登录过于频繁被临时限流，\n"
    "这通常是被前面的失败尝试触发的。请先停止重试，等待 30 分钟以上（必要时 1~2 小时），\n"
    "再运行一次 python send_mail.py --check。期间不要反复试，否则会重新计时。"
)


def _auth_advice(server_text: str) -> str:
    """根据服务器 535 原文给出更有针对性的排查建议。"""
    text = server_text.lower()
    if "frequency" in text or "too many" in text:
        return FREQUENCY_HINT
    if "service is not open" in text:
        return (
            "服务器提示「service is not open」，即该账号的 SMTP 服务未开启（或未生效）。\n"
            "请到网页版 QQ 邮箱 -> 设置 -> 账户，确认「POP3/SMTP 服务」是已开启状态；\n"
            "若刚刚开启，请等待几分钟再试；若之前用独立密码/微信绑定登录过，也可能需要重新生成授权码。"
        )
    if "account is abnormal" in text:
        return (
            "服务器提示「Account is abnormal」，说明该邮箱账号本身处于异常状态（可能被冻结、\n"
            "需要安全验证或实名认证）。请先登录网页版 QQ 邮箱按提示解除异常后再试。"
        )
    return AUTH_HINT


def _auth_reply_message(code: int, response: object) -> str:
    text = " ".join(_decode(response).split())
    detail = f"服务器返回 {code}：{text}" if text else f"服务器返回 {code}"
    if code == 535:
        return f"登录被拒绝。{detail}\n{_auth_advice(text)}"
    return f"登录失败。{detail}\n{AUTH_HINT}"


def connect(host: str, port: int, use_ssl: bool, timeout: int = 25) -> smtplib.SMTP:
    """按指定参数建立连接并完成 EHLO/STARTTLS。"""
    context = ssl.create_default_context()
    if use_ssl:
        server: smtplib.SMTP = smtplib.SMTP_SSL(host, port, timeout=timeout, context=context)
        server.ehlo()
        return server
    server = smtplib.SMTP(host, port, timeout=timeout)
    server.ehlo()
    server.starttls(context=context)
    server.ehlo()
    return server


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def auth_plain(server: smtplib.SMTP, email: str, code: str) -> tuple[int, object]:
    return server.docmd("AUTH", f"PLAIN {_b64(chr(0) + email + chr(0) + code)}")


def auth_login(server: smtplib.SMTP, email: str, code: str) -> tuple[int, object]:
    """手动走一遍 AUTH LOGIN（用户名 -> 授权码），便于看到每一步的服务器回复。"""
    code1, response1 = server.docmd("AUTH", "LOGIN")
    if code1 != 334:
        return code1, response1
    code2, response2 = server.docmd(_b64(email))
    if code2 != 334:
        return code2, response2
    return server.docmd(_b64(code))


def login(server: smtplib.SMTP, cfg: Config) -> None:
    """登录。优先用 AUTH PLAIN 直接发一次，避免 smtplib 在 535 后改用 LOGIN
    机制继续尝试而被服务器断连，从而把认证错误误判成网络故障。"""
    mechanisms = server.esmtp_features.get("auth", "").upper().split()
    try:
        if "PLAIN" in mechanisms:
            code, response = auth_plain(server, cfg.sender_email, cfg.auth_code)
        else:
            code, response = auth_login(server, cfg.sender_email, cfg.auth_code)
    except smtplib.SMTPServerDisconnected as exc:
        raise AuthFailed(
            "服务器在登录阶段直接断开了连接。\n" + AUTH_HINT
        ) from exc

    if code in (235, 503):
        return
    raise AuthFailed(_auth_reply_message(code, response))


def diagnose(cfg: Config) -> int:
    """逐个尝试「端口/加密方式/认证机制」组合，只登录，不发送邮件。"""
    say("=" * 62)
    say("SMTP 登录诊断（不会发送任何邮件）")
    say("=" * 62)
    say(f"账号：{cfg.sender_email}")
    say(f"授权码：{len(cfg.auth_code)} 位，"
        f"纯英文字母={'是' if cfg.auth_code.isalpha() else '否'}，"
        f"全小写={'是' if cfg.auth_code.islower() else '否'}")
    say()

    combos = [
        (465, True, "PLAIN"),
        (465, True, "LOGIN"),
        (587, False, "PLAIN"),
    ]
    for index, (port, use_ssl, mechanism) in enumerate(combos, 1):
        label = f"{port} {'SSL' if use_ssl else 'STARTTLS'} + AUTH {mechanism}"
        say(f"[{index}/{len(combos)}] {label}")
        server = None
        try:
            server = connect(cfg.host, port, use_ssl)
            features = server.esmtp_features
            say(f"      连接成功：{cfg.host}:{port}")
            say(f"      服务器宣告的认证方式：{features.get('auth', '未提供') or '未提供'}")
            if mechanism == "PLAIN":
                code, response = auth_plain(server, cfg.sender_email, cfg.auth_code)
            else:
                code, response = auth_login(server, cfg.sender_email, cfg.auth_code)
            text = " ".join(_decode(response).split())
            if code in (235, 503):
                say(f"      登录成功（{code}）-> 这一组合可用")
                say()
                say("结论：该组合可以正常登录。")
                return 0
            say(f"      登录失败（{code}）：{text or '无返回内容'}")
            say(f"      建议：{_auth_advice(text).splitlines()[0]}")
        except AuthFailed as exc:
            say(f"      登录失败：{str(exc).splitlines()[0]}")
        except smtplib.SMTPServerDisconnected:
            say("      登录阶段被服务器直接断开（QQ 在认证失败时常这样做）")
        except Exception as exc:  # noqa: BLE001
            say(f"      连接阶段失败：{friendly_error(exc)}")
        finally:
            if server is not None:
                try:
                    server.quit()
                except Exception:  # noqa: BLE001
                    pass
        say()

    say("=" * 62)
    say("所有组合都登录失败。结合服务器原文，按以下顺序排查：")
    say(FREQUENCY_HINT)
    say()
    say("若等待后仍然 535：")
    say(AUTH_HINT)
    say("=" * 62)
    return 3


def send_one(cfg: Config, subject: str, html_text: str, to_addr: str) -> None:
    """单独建立一次连接，只发给一位收件人。"""
    message = build_message(cfg, subject, html_text, to_addr)
    server = open_server(cfg)
    try:
        login(server, cfg)
        server.send_message(message)
    finally:
        try:
            server.quit()
        except Exception:  # noqa: BLE001 - 关闭连接失败不影响结果
            pass


def check_login(cfg: Config) -> None:
    """只验证能否连接并登录，不发送任何邮件。"""
    server = open_server(cfg)
    try:
        login(server, cfg)
    finally:
        try:
            server.quit()
        except Exception:  # noqa: BLE001
            pass


def _decode(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def friendly_error(exc: BaseException) -> str:
    if isinstance(exc, AuthFailed):
        return str(exc)
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return _auth_reply_message(exc.smtp_code, exc.smtp_error)
    if isinstance(exc, smtplib.SMTPSenderRefused):
        return f"发件人被服务器拒绝({exc.smtp_code})：{_decode(exc.smtp_error)}"
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        detail = "；".join(
            f"{addr}: {_decode(info[1])}" for addr, info in exc.recipients.items()
        )
        return f"收件人被服务器拒绝：{detail or exc}"
    if isinstance(exc, smtplib.SMTPDataError):
        return f"邮件内容被服务器拒绝({exc.smtp_code})：{_decode(exc.smtp_error)}"
    if isinstance(exc, smtplib.SMTPResponseException):
        return f"服务器返回错误({exc.smtp_code})：{_decode(exc.smtp_error)}"
    if isinstance(exc, smtplib.SMTPServerDisconnected):
        return ("与服务器的连接被中断：可能是网络不稳定/发送过于频繁，"
                "也可能是登录被拒绝后服务器主动断连。")
    if isinstance(exc, ssl.SSLError):
        return f"SSL 握手失败：{exc}（若使用代理/VPN，请尝试关闭后重试）"
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return "连接超时：网络不可达或被代理拦截，请检查网络设置。"
    if isinstance(exc, socket.gaierror):
        return f"无法解析服务器地址：{exc}，请检查 [smtp] host。"
    if isinstance(exc, ConnectionError):
        return f"网络连接失败：{exc}"
    return f"{type(exc).__name__}: {exc}"


def send_with_retry(cfg: Config, subject: str, html_text: str, to_addr: str) -> tuple[bool, str, bool]:
    """返回 (是否成功, 错误描述, 是否认证失败需中止整个任务)。"""
    error = "未知错误"
    for attempt in range(cfg.retry_times + 1):
        if attempt > 0:
            wait = cfg.retry_wait_seconds * attempt
            say(f"\n    第 {attempt}/{cfg.retry_times} 次重试，等待 {wait:.0f} 秒 ...")
            time.sleep(wait)
        try:
            send_one(cfg, subject, html_text, to_addr)
            return True, "", False
        except AuthFailed as exc:
            # 授权码/服务未开启这类错误重试也没用，直接中止整批
            return False, str(exc), True
        except smtplib.SMTPAuthenticationError as exc:
            return False, _auth_reply_message(exc.smtp_code, exc.smtp_error), True
        except Exception as exc:  # noqa: BLE001 - 单封失败不应中断整体流程
            error = friendly_error(exc)
            if attempt < cfg.retry_times:
                say(f"\n    第 {attempt + 1} 次尝试失败：{error}")
    return False, error, False


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="使用 QQ 邮箱把 HTML 文件作为正文，逐个收件人单独发送邮件。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--config", help=f"配置文件路径（默认 {DEFAULT_CONFIG_PATH.name}）")
    parser.add_argument("--html", help="HTML 正文文件路径（覆盖配置里的 html_file）")
    parser.add_argument("--recipients", help=f"收件人列表路径（默认 {DEFAULT_RECIPIENTS_PATH.name}）")
    parser.add_argument("--subject", help="邮件主题（覆盖配置；留空则读取 HTML 的 <title>）")
    parser.add_argument("--dry-run", action="store_true", help="只检查配置与收件人，不实际发送")
    parser.add_argument("--check", action="store_true",
                        help="只测试 SMTP 连接与登录（会真实登录服务器），不发送邮件")
    parser.add_argument("--debug", action="store_true",
                        help="检查各端口/加密方式/认证机制能否登录，并打印服务器原始回复")
    parser.add_argument("-y", "--yes", action="store_true", help="跳过发送前的确认提示")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    setup_console()
    args = parse_args(argv)

    config_path = Path(args.config).expanduser() if args.config else DEFAULT_CONFIG_PATH
    recipients_path = Path(args.recipients).expanduser() if args.recipients else DEFAULT_RECIPIENTS_PATH

    try:
        cfg = load_config(config_path)
        if args.html:
            cfg.html_file = Path(args.html).expanduser().resolve()

        # 只做连接/登录测试，不需要正文与收件人
        if args.debug:
            return diagnose(cfg)

        if args.check:
            say("正在测试 SMTP 连接与登录 ...")
            try:
                check_login(cfg)
            except AuthFailed as exc:
                say(f"登录失败：{exc}")
                return 3
            except Exception as exc:  # noqa: BLE001
                say(f"连接失败：{friendly_error(exc)}")
                return 3
            say(f"连接并登录成功：{cfg.sender_email} @ {cfg.host}:{cfg.port}")
            return 0

        html_text = read_html(cfg.html_file)
        subject = (args.subject or cfg.subject or extract_title(html_text) or cfg.html_file.stem).strip()
        if not subject:
            subject = cfg.html_file.stem
        recipients, invalid = parse_recipients(recipients_path)
        if not recipients:
            raise ConfigError(
                f"收件人列表为空：{recipients_path}\n"
                "请一行一个邮箱地址写进该文件（以 # 开头的行会被忽略）。"
            )
    except ConfigError as exc:
        say(f"错误：{exc}")
        return 2

    if invalid:
        say("以下行不是合法的邮箱地址，已跳过：")
        for lineno, item in invalid:
            say(f"  第 {lineno} 行：{item}")
        say()

    # 发送前概览
    say("=" * 62)
    say(f"发件邮箱：{cfg.sender_email}" + (f"（{cfg.display_name}）" if cfg.display_name else ""))
    say(f"SMTP 服务器：{cfg.host}:{cfg.port}（{'SSL' if cfg.use_ssl else 'STARTTLS'}）")
    say(f"邮件主题：{subject}")
    say(f"HTML 正文：{cfg.html_file}")
    say(f"收件人数：{len(recipients)}")
    say(f"发送间隔：{cfg.delay_seconds:g} 秒/封，失败重试 {cfg.retry_times} 次")
    say(f"总计：每封单独发送，共 {len(recipients)} 封邮件")
    say("=" * 62)

    if args.dry_run:
        say("\n[dry-run] 收件人清单：")
        for index, addr in enumerate(recipients, 1):
            say(f"  {index:>3}. {addr}")
        say("\n[dry-run] 配置检查通过，未实际发送任何邮件。")
        return 0

    if not args.yes:
        try:
            answer = input("确认开始发送？(y/N) ").strip().lower()
        except EOFError:
            answer = ""
        if answer not in ("y", "yes"):
            say("已取消，未发送任何邮件。")
            return 0
        say()

    send_start = time.time()
    sent: list[str] = []
    failed: list[tuple[str, str]] = []
    aborted = False
    total = len(recipients)

    for index, addr in enumerate(recipients, 1):
        print(f"[{index}/{total}] {addr} 发送中 ... ", end="", flush=True)
        ok, error, auth_failed = send_with_retry(cfg, subject, html_text, addr)
        if ok:
            print("成功")
            sent.append(addr)
            write_log("OK", addr, subject)
        else:
            print(f"失败（{error}）")
            failed.append((addr, error))
            write_log("FAIL", addr, error)
            if auth_failed:
                aborted = True
                say("\n授权码认证失败，继续发送只会重复报错，已中止本次任务。")
                break
        if index < total:
            time.sleep(cfg.delay_seconds)

    elapsed = time.time() - send_start
    say()
    say("=" * 62)
    say(f"发送完成：成功 {len(sent)} 封，失败 {len(failed)} 封，"
        f"未处理 {total - len(sent) - len(failed)} 封，耗时 {elapsed:.0f} 秒")

    if failed:
        try:
            FAILED_PATH.write_text(
                "# 以下收件人发送失败，可直接用 --recipients failed_recipients.txt 重新发送\n"
                + "\n".join(addr for addr, _ in failed)
                + "\n",
                encoding="utf-8",
            )
            say(f"失败名单已保存到：{FAILED_PATH}")
        except OSError as exc:
            say(f"（警告：保存失败名单时出错：{exc}）")
    elif FAILED_PATH.exists():
        # 本轮全部成功，清掉上次遗留的失败名单，避免误用旧名单重复发信
        try:
            FAILED_PATH.unlink()
            say("上次遗留的失败名单已清除。")
        except OSError:
            pass
    say(f"详细记录：{LOG_PATH}")
    say("=" * 62)

    if aborted:
        return 3
    return 0 if not failed else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        say("\n已手动中断，日志见 send_log.txt")
        raise SystemExit(130)
