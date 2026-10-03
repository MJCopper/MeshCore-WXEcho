"""Bounded, redacted process logs, including Python stdout and stderr."""
from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import re
import sys
import threading

SECRET = re.compile(r'(?i)(authorization|password|passwd|token|secret|api[_-]?key|channel[_-]?key|pin)(["\s:=]+)([^\s,;}]+)')


def redact(value):
    if isinstance(value, dict):
        return {k: ("[redacted]" if re.search(r"(?i)password|passwd|token|secret|key|pin", k)
                    else redact(v)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    if isinstance(value, str):
        value = re.sub(r'(?i)Bearer\s+\S+', 'Bearer [redacted]', value)
        value = re.sub(r'(https?://)[^/\s:@]+:[^/\s@]+@', r'\1[redacted]@', value)
        return SECRET.sub(lambda m: m[1] + m[2] + '[redacted]', value)
    return value


class ProcessLogs:
    def __init__(self, path: Path | None = None, capacity=1000):
        self.rows = deque(maxlen=capacity)
        self.lock = threading.RLock()
        self.sequence = 0
        self.file = None
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                # Read only the bounded tail even if an external file is oversized.
                with path.open('rb') as stream:
                    stream.seek(max(0, path.stat().st_size - 1024 * 1024))
                    for line in stream.read().decode('utf-8', errors='replace').splitlines():
                        try:
                            row = json.loads(line)
                            self.sequence += 1
                            row['id'] = self.sequence
                            self.rows.append(redact(row))
                        except (ValueError, TypeError, AttributeError):
                            continue
            self.file = RotatingFileHandler(path, maxBytes=1024 * 1024, backupCount=2)
            self.file.setFormatter(logging.Formatter('%(message)s'))
            self.file.handleError = lambda record: None
        self.original_streams = None
        self.handler = None

    def add(self, service, level, message):
        with self.lock:
            self.sequence += 1
            row = dict(id=self.sequence, ts=datetime.now(timezone.utc).isoformat(timespec='milliseconds'),
                       service=service, level=level, message=redact(str(message))[:8192])
            self.rows.append(row)
            if self.file:
                record = logging.LogRecord('diagnostics', logging.INFO, '', 0, json.dumps(row), (), None)
                self.file.emit(record)

    def query(self, after=0, service='', level='', search=''):
        with self.lock:
            rows = [r for r in self.rows if r['id'] > after
                    and (not service or service.casefold() in r['service'].casefold())
                    and (not level or r['level'] == level)
                    and (not search or search.casefold() in r['message'].casefold())]
            return dict(rows=rows, cursor=self.sequence,
                        truncated=bool(self.rows and after and after < self.rows[0]['id'] - 1))

    def install(self):
        logs = self
        class Capture(logging.Handler):
            def emit(self, record):
                try:
                    message = self.format(record)
                    logs.add(record.name, record.levelname, message)
                except Exception:
                    pass  # diagnostic collection must not break the process
        self.handler = Capture()
        logging.getLogger().addHandler(self.handler)
        self.original_streams = (sys.stdout, sys.stderr)
        sys.stdout = StreamCapture(sys.stdout, self, 'stdout')
        sys.stderr = StreamCapture(sys.stderr, self, 'stderr')

    def close(self):
        if self.original_streams:
            sys.stdout.flush()
            sys.stderr.flush()
            sys.stdout, sys.stderr = self.original_streams
            self.original_streams = None
        if self.handler:
            logging.getLogger().removeHandler(self.handler)
        if self.file:
            self.file.close()


class StreamCapture:
    def __init__(self, stream, logs, name):
        self.stream, self.logs, self.name = stream, logs, name
        self.pending = ''
        self.lock = threading.RLock()

    def write(self, text):
        result = self.stream.write(text)
        with self.lock:
            self.pending += text
            while '\n' in self.pending:
                line, self.pending = self.pending.split('\n', 1)
                if line.strip():
                    self.logs.add(self.name, 'ERROR' if self.name == 'stderr' else 'INFO', line)
            if len(self.pending) > 8192:
                self.logs.add(self.name, 'INFO', self.pending[:8192])
                self.pending = ''
        return result

    def flush(self):
        self.stream.flush()
        with self.lock:
            if self.pending:
                self.logs.add(self.name, 'INFO', self.pending)
                self.pending = ''

    def __getattr__(self, name):
        return getattr(self.stream, name)
