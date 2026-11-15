# ============================================================================
# NOTIFICATIONS  (mail, webhook, telegram)
#
# The message is built from a whitelist of fields: task path, project, host,
# kind, status, duration, per-action summaries and structured failures
# ({action, entity, reason}). Command lines, stderr text and secrets never get
# in, even though --debug writes full commands to the log on purpose.
#
# Delivery problems are warnings only: a notification must never change the
# exit code of the backup, and a silent config (no block, empty `on`, no
# channels) writes nothing to the log either.
# ============================================================================

class Notifier:
    @staticmethod
    def enabled(notify, code):
        if notify is None or not notify.on or not notify.channels():
            return False
        if "always" in notify.on:
            return True
        if code != 0:
            return "failure" in notify.on
        return "success" in notify.on

    @staticmethod
    def run(cfg, kind, code, details, started, failures=None):
        """Send one message per run; never raises, never changes the code."""
        notify = getattr(cfg, "notify", None)
        if DRY or not Notifier.enabled(notify, code):
            return
        subject, text = Notifier.build_message(cfg, kind, code, details,
                                               started, failures)
        for channel in notify.channels():
            Notifier._deliver(notify, channel, subject, text)

    @staticmethod
    def build_message(cfg, kind, code, details, started, failures):
        status = "OK" if code == 0 else "FAILED"
        elapsed = 0.0
        if started is not None:
            elapsed = max(0.0, (dt.datetime.now() - started).total_seconds())
        lines = [f"task:      {cfg.path}"]
        if cfg.project != Path(cfg.path).stem:
            lines.append(f"project:   {cfg.project}")
        lines += [
            f"host:      {socket.gethostname()}",
            f"kind:      {kind}",
            f"status:    {status}, exit {code}",
            f"duration:  {elapsed:.1f}s",
        ]
        if details:
            lines.append("actions:")
            lines.extend(f"  {item}" for item in details)
        if failures:
            lines.append("failures:")
            for item in failures:
                what = item.get("action") or "run"
                if item.get("entity"):
                    what += f" \u2192 {item['entity']}"
                lines.append(f"  {what}: {item.get('reason') or 'failed'}")
        subject = f"[kwbackup] {cfg.project}: {kind} {status.lower()}"
        if code:
            subject += f" (exit {code})"
        return subject, "\n".join(lines)

    @staticmethod
    def _deliver(notify, channel, subject, text):
        label = Notifier._label(channel)
        attempts = max(0, notify.retries or 0) + 1
        for attempt in range(1, attempts + 1):
            try:
                if isinstance(channel, MailCfg):
                    Notifier._send_mail(channel, subject, text, notify.timeout)
                elif isinstance(channel, WebhookCfg):
                    Notifier._send_webhook(channel, subject, text, notify.timeout)
                else:
                    Notifier._send_telegram(channel, subject, text, notify.timeout)
                LOG.info("notify: sent via %s", label)
                return True
            except Exception as exc:          # delivery must never break a run
                if attempt < attempts:
                    LOG.debug("notify: %s: %s, retry %d/%d", label,
                              Notifier._describe(exc), attempt, notify.retries)
                    time.sleep(max(0, notify.retry_delay or 0))
                    continue
                LOG.warning("notify: %s: %s", label, Notifier._describe(exc))
        return False

    @staticmethod
    def _label(channel):
        if isinstance(channel, MailCfg):
            return "mail to " + ", ".join(channel.to)
        if isinstance(channel, WebhookCfg):
            return "webhook " + (urllib.parse.urlsplit(channel.url).netloc or "?")
        return "telegram chat " + channel.chat_id

    @staticmethod
    def _describe(exc):
        """Never include a URL: the telegram one carries the token."""
        if isinstance(exc, urllib.error.HTTPError):
            return f"HTTP {exc.code}"
        if isinstance(exc, urllib.error.URLError):
            reason = exc.reason
            if isinstance(reason, OSError):
                return reason.strerror or type(reason).__name__
            return str(reason)[:200]
        if isinstance(exc, smtplib.SMTPException):
            return type(exc).__name__
        if isinstance(exc, OSError):
            return exc.strerror or type(exc).__name__
        return f"{type(exc).__name__}: {str(exc)[:200]}"

    @staticmethod
    def _send_mail(mail, subject, text, timeout):
        message = EmailMessage()
        message["From"] = mail.sender
        message["To"] = ", ".join(mail.to)
        message["Subject"] = subject
        message.set_content(text)
        user = os.environ.get(mail.user_env, "") if mail.user_env else ""
        password = os.environ.get(mail.password_env, "") if mail.password_env else ""
        if mail.user_env and not user:
            raise RuntimeError(f"environment variable {mail.user_env} is not set")
        with smtplib.SMTP(mail.smtp_host, mail.port, timeout=timeout) as smtp:
            if mail.tls:
                smtp.starttls()
            if user:
                smtp.login(user, password)
            smtp.send_message(message)

    @staticmethod
    def _send_webhook(hook, subject, text, timeout):
        payload = {"source": PROG, "subject": subject, "text": text}
        headers = {"Content-Type": "application/json"}
        headers.update(hook.headers)
        Notifier._http_json(hook.url, payload, headers, timeout)

    @staticmethod
    def _send_telegram(tg, subject, text, timeout):
        token = os.environ.get(tg.token_env, "")
        if not token:
            raise RuntimeError(f"environment variable {tg.token_env} is not set")
        body = f"{subject}\n\n{text}"
        if len(body) > 4000:              # Telegram caps a message at 4096
            body = body[:3997] + "..."
        Notifier._http_json(
            f"https://api.telegram.org/bot{token}/sendMessage",
            {"chat_id": tg.chat_id, "text": body,
             "disable_web_page_preview": True}, {}, timeout)

    @staticmethod
    def _http_json(url, payload, headers, timeout):
        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(url, data=data, method="POST")
        for key, value in headers.items():
            request.add_header(str(key), str(value))
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response.read(4096)
