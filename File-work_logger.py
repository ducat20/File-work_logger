import os
import sys
import sqlite3
import threading
import time
import csv
import subprocess
import platform
from datetime import datetime, timedelta
from pathlib import Path
import argparse

# ---- Optional dependencies ----
try:
    from win10toast import ToastNotifier  # Windows toast
except Exception:
    ToastNotifier = None

try:
    import PySimpleGUI as sg
except Exception as e:
    print("PySimpleGUI is required: pip install PySimpleGUI")
    raise

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
except Exception:
    Observer = None
    FileSystemEventHandler = object

APP_NAME = "WorkAssistantMVP"
DB_DIR = Path(os.getenv("APPDATA", str(Path.home() / ".work_assistant"))) / APP_NAME
DB_PATH = DB_DIR / "work_assistant.db"
DEFAULT_REMIND_HOUR = 9

# ------------------ Database ------------------
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS file_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_name TEXT,
    event_time TEXT,
    ext TEXT,
    dir TEXT,
    event_type TEXT,
    src_path TEXT,
    dest_path TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_time ON file_events(event_time);
CREATE INDEX IF NOT EXISTS idx_events_name ON file_events(file_name);

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_text TEXT NOT NULL,
    due_date TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_due ON tasks(due_date);

CREATE TABLE IF NOT EXISTS settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    watch_dir TEXT,
    extensions TEXT,
    remind_hour INTEGER DEFAULT 9
);
INSERT OR IGNORE INTO settings(id, watch_dir, extensions, remind_hour) VALUES(1, '', '', 9);
"""

def db_connect():
    DB_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


# ------------------ Watchdog ------------------
class FSHandler(FileSystemEventHandler):
    def __init__(self, conn, exts):
        self.conn = conn
        self.exts = {e.lower().strip() for e in exts if e.strip()}
        super().__init__()

    def _log(self, event_type, src_path=None, dest_path=None):
        try:
            p = Path(dest_path or src_path)
            ext = p.suffix.lower()
            if self.exts and ext not in self.exts:
                return
            file_name = p.name
            dir_ = str(p.parent)
            now = datetime.now().isoformat(timespec='seconds')
            with self.conn:
                self.conn.execute(
                    "INSERT INTO file_events(file_name, event_time, ext, dir, event_type, src_path, dest_path) VALUES(?,?,?,?,?,?,?)",
                    (file_name, now, ext, dir_, event_type, src_path, dest_path),
                )
        except Exception as e:
            print("Log error:", e)

    def on_created(self, event):
        if event.is_directory:
            return
        self._log('created', src_path=event.src_path)

    def on_modified(self, event):
        if event.is_directory:
            return
        self._log('modified', src_path=event.src_path)

    def on_moved(self, event):
        if event.is_directory:
            return
        self._log('moved', src_path=event.src_path, dest_path=event.dest_path)

    def on_deleted(self, event):
        if event.is_directory:
            return
        self._log('deleted', src_path=event.src_path)


class Watcher:
    def __init__(self, path, exts, conn):
        self.path = path
        self.exts = exts
        self.conn = conn
        self.observer = None
        self.thread = None
        self._stop = threading.Event()

    def start(self):
        if Observer is None:
            raise RuntimeError("watchdog is not installed")
        if self.observer:
            return
        handler = FSHandler(self.conn, self.exts)
        self.observer = Observer()
        self.observer.schedule(handler, self.path, recursive=True)
        self.observer.start()

    def stop(self):
        if self.observer:
            self.observer.stop()
            self.observer.join(timeout=5)
            self.observer = None


class MultiWatcher:
    """Manage multiple watch roots with a single Observer."""
    def __init__(self, paths, exts, conn):
        self.paths = paths
        self.exts = exts
        self.conn = conn
        self.observer = None

    def start(self):
        if Observer is None:
            raise RuntimeError("watchdog is not installed")
        if self.observer:
            return
        handler = FSHandler(self.conn, self.exts)
        self.observer = Observer()
        for p in self.paths:
            self.observer.schedule(handler, p, recursive=True)
        self.observer.start()

    def stop(self):
        if self.observer:
            self.observer.stop()
            self.observer.join(timeout=5)
            self.observer = None


# ------------------ Utilities ------------------

def is_safe_watch_dir(path_str: str) -> bool:
    """Restrict watch target to user's home to avoid system/other users' folders.
    - Allowed: inside Path.home()
    - Disallowed: Windows, Program Files, ProgramData, Users\OtherUser, root of drive, etc.
    """
    try:
        p = Path(path_str).resolve()
        home = Path.home().resolve()
        # must be under home
        if home in p.parents or p == home:
            # explicitly block common system subpaths under home (rare), else allow
            return True
        return False
    except Exception:
        return False


# -------- Natural Language Query (NLQ) Parser --------
# 간단 한국어 자연어 쿼리를 필터로 변환합니다.
# 지원: 오늘/어제/이번주/지난주/이번달/지난달, YYYY-MM-DD, YYYY-MM-DD~YYYY-MM-DD
#      이벤트: 생성/수정/이동/삭제
#      확장자 별칭: 워드/엑셀/한글/파워포인트/파포/PDF/텍스트/이미지/사진

def parse_nl_query(nlq: str):
    text = (nlq or '').strip()
    if not text:
        return {}

    now = datetime.now()
    d0 = now.replace(hour=0, minute=0, second=0, microsecond=0)

    def daterange_for_text(t: str):
        t2 = t.replace(' ', '')
        if '오늘' in t2:
            s, e = d0, d0 + timedelta(days=1) - timedelta(seconds=1)
            return s, e
        if '어제' in t2:
            s, e = d0 - timedelta(days=1), d0 - timedelta(seconds=1)
            return s, e
        if '이번주' in t2:
            s = d0 - timedelta(days=d0.weekday())
            e = s + timedelta(days=7) - timedelta(seconds=1)
            return s, e
        if '지난주' in t2:
            s = d0 - timedelta(days=d0.weekday()+7)
            e = s + timedelta(days=7) - timedelta(seconds=1)
            return s, e
        if '이번달' in t2:
            s = d0.replace(day=1)
            nm = s.replace(year=s.year+1, month=1) if s.month==12 else s.replace(month=s.month+1)
            e = nm - timedelta(seconds=1)
            return s, e
        if '지난달' in t2:
            first_this = d0.replace(day=1)
            last_prev = first_this - timedelta(seconds=1)
            s = last_prev.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            e = last_prev
            return s, e
        if '~' in t:
            a, b = t.split('~', 1)
            try:
                s = datetime.fromisoformat(a.strip()[:10] + ' 00:00:00')
                e = datetime.fromisoformat(b.strip()[:10] + ' 23:59:59')
                return s, e
            except Exception:
                return None, None
        for part in t.split():
            if len(part) >= 10 and part[4] == '-' and part[7] == '-':
                try:
                    s = datetime.fromisoformat(part[:10] + ' 00:00:00')
                    e = datetime.fromisoformat(part[:10] + ' 23:59:59')
                    return s, e
                except Exception:
                    pass
        return None, None

    events_map = {
        '생성': 'created', '만들': 'created', '추가': 'created',
        '수정': 'modified', '변경': 'modified',
        '이동': 'moved', '옮기': 'moved',
        '삭제': 'deleted', '없어지': 'deleted', '제거': 'deleted'
    }
    event_types = []
    for k, v in events_map.items():
        if k in text and v not in event_types:
            event_types.append(v)

    ext_syn = {
        '워드': ['.doc', '.docx'], '엑셀': ['.xls', '.xlsx'], '한글': ['.hwp'],
        '파워포인트': ['.ppt', '.pptx'], '파포': ['.ppt', '.pptx'],
        'pdf': ['.pdf'], '텍스트': ['.txt'], '이미지': ['.png', '.jpg', '.jpeg'], '사진': ['.png', '.jpg', '.jpeg']
    }
    extensions = []
    for token in text.replace(',', ' ').split():
        if token.startswith('.') and len(token) <= 6:
            extensions.append(token.lower())
    tl = text.lower()
    for syn, lst in ext_syn.items():
        if syn.lower() in tl:
            for e in lst:
                if e not in extensions:
                    extensions.append(e)

    s, e = daterange_for_text(text)
    start = s.isoformat(sep=' ') if s else None
    end = e.isoformat(sep=' ') if e else None

    drop = {'오늘','어제','이번','지난','이번주','지난주','이번달','지난달','파일','확장자','중','포함'}
    keywords = []
    for token in text.replace(',', ' ').split():
        if token in drop:
            continue
        if token.startswith('.'):
            continue
        if any(k in token for k in events_map.keys()):
            continue
        if token.lower() in ext_syn:
            continue
        keywords.append(token)
    keyword = ' '.join(keywords)

    return {'keyword': keyword, 'start': start, 'end': end, 'event_types': event_types, 'extensions': extensions}


def to_rows(records):
    rows = []
    for r in records:
        id_, name, ts, ext, dir_, etype, src, dst = r
        rows.append([id_, name, ts, ext, dir_, etype])
    return rows


def search_events(conn, keyword='', start=None, end=None, ext_filter='', event_types=None):
    q = "SELECT id, file_name, event_time, ext, dir, event_type, src_path, dest_path FROM file_events WHERE 1=1"
    params = []
    if keyword:
        q += " AND (file_name LIKE ? OR dir LIKE ? OR event_type LIKE ?)"
        k = f"%{keyword}%"
        params += [k, k, k]
    if start
