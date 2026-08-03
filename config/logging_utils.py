"""
Logging building blocks referenced from `config.settings.base.LOGGING`:

* `request_id_var` / `RequestIDFilter` -- makes the per-request id (set by
  `config.middleware.RequestIDMiddleware`) available to every log record as
  `%(request_id)s`, defaulting to "-" outside a request (management commands,
  the Django-Q worker, the MCP service).
* `DailyRotatingFileHandler` -- one `app-YYYY-MM-DD.log` file per day under
  `./logs`, rotating at local midnight and keeping the newest `backup_count`
  days.

Kept separate from `settings` so the dictConfig entries can reference these by
dotted path without importing Django at module load time.
"""

import contextvars
import logging
import os
import re
import time
from logging.handlers import TimedRotatingFileHandler

# Current request id, or "-" when no request is in scope. A ContextVar (not a
# thread-local) so it stays correct under ASGI/async views as well as WSGI.
request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id", default="-"
)


class RequestIDFilter(logging.Filter):
    """Attaches the current `request_id` to every record so `%(request_id)s`
    in a formatter always resolves, even for records emitted outside a request.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


class DailyRotatingFileHandler(TimedRotatingFileHandler):
    """Writes to ``<log_dir>/<prefix>-YYYY-MM-DD.log``, rotating at local
    midnight and retaining the newest ``backup_count`` dated files.

    The stock `TimedRotatingFileHandler` keeps a fixed base filename and only
    appends a date *suffix* to already-rotated files, so the active file never
    carries today's date. Here the active file itself is dated, which is what
    the ``app-YYYY-MM-DD.log`` requirement asks for; rollover just re-points the
    handler at the new day's file and prunes anything older than the retention
    window.
    """

    def __init__(self, log_dir, prefix="app", backup_count=7, encoding="utf-8"):
        self.log_dir = str(log_dir)
        self.prefix = prefix
        # exist_ok makes a start against a pre-existing ./logs a no-op rather
        # than a crash (the settings module also ensures the directory, this is
        # belt-and-suspenders for handlers built outside dictConfig).
        os.makedirs(self.log_dir, exist_ok=True)
        super().__init__(
            self._current_filename(),
            when="midnight",
            interval=1,
            backupCount=backup_count,
            encoding=encoding,
            utc=False,
        )
        # Prune stale files left over from previous runs on startup.
        self._remove_old_logs()

    def _current_filename(self) -> str:
        today = time.strftime("%Y-%m-%d", time.localtime())
        return os.path.join(self.log_dir, f"{self.prefix}-{today}.log")

    def doRollover(self) -> None:
        if self.stream:
            self.stream.close()
            self.stream = None

        # Point at today's file instead of renaming yesterday's (its name is
        # already correct for the day it covers).
        self.baseFilename = os.path.abspath(self._current_filename())
        if not self.delay:
            self.stream = self._open()

        # Schedule the next midnight rollover, mirroring the DST correction the
        # stock handler applies so rollovers don't drift by an hour twice a year.
        current_time = int(time.time())
        new_rollover_at = self.computeRollover(current_time)
        while new_rollover_at <= current_time:
            new_rollover_at += self.interval
        if not self.utc:
            dst_now = time.localtime(current_time)[-1]
            dst_at_rollover = time.localtime(new_rollover_at)[-1]
            if dst_now != dst_at_rollover:
                new_rollover_at += -3600 if dst_now else 3600
        self.rolloverAt = new_rollover_at

        self._remove_old_logs()

    def _remove_old_logs(self) -> None:
        """Keep only the newest ``backupCount`` ``<prefix>-YYYY-MM-DD.log`` files."""
        if self.backupCount <= 0:
            return
        pattern = re.compile(
            rf"^{re.escape(self.prefix)}-(\d{{4}}-\d{{2}}-\d{{2}})\.log$"
        )
        dated = sorted(
            name for name in os.listdir(self.log_dir) if pattern.match(name)
        )
        for name in dated[: -self.backupCount]:
            try:
                os.remove(os.path.join(self.log_dir, name))
            except FileNotFoundError:
                # Another process (web/worker/mcp share ./logs) already pruned
                # it -- expected, and only this narrow case is ignored.
                pass
