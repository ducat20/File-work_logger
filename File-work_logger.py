# -*- coding: utf-8 -*-
"""
File Work Logger (tkinter) — single-file MVP (auto-refresh 3s)

- tkinter UI (표준 라이브러리)
- watchdog로 파일 이벤트(생성/수정/이동/삭제) 감시 (다중 폴더, 확장자 필터)
- SQLite 영구 로그 + 검색(키워드/기간/확장자/자연어) + CSV 내보내기
- 메모 요약(빈 줄로 항목 구분), 미처리건 선택 저장(체크박스), 다음 근무일 알림(schtasks)
- 권한 자가점검(사용자 홈 내부만 허용)
- ✅ 자동 새로고침: 3초마다 테이블 갱신

필수 패키지:
  py -m pip install watchdog win10toast
EXE 빌드:
  py -m PyInstaller -F -w file_work_logger_tk.py
"""

import os, sys, sqlite3, csv, threading, subprocess, platform, traceback
from datetime import datetime, timedelta
from pathlib import Path
import argparse

# --- Optional deps ---
try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
except Exception:
    Observer = None
    class FileSystemEventHandler: pass  # type: ignore

try:
    from win10toast import ToastNotifier
except Exception:
    ToastNotifier = None

# --- tkinter / ttk ---
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

APP_NAME = "WorkAssistantTK"
DB_DIR = Path(os.getenv("APPDATA", str(Path.home()/".work_assistant"))) / APP_NAME
DB_PATH = DB_DIR / "work_assistant.db"
DEFAULT_REMIND_HOUR = 9

# ---------- Crash log ----------
LOG_DIR = Path(os.getenv("LOCALAPPDATA", str(Path.home()))) / "WorkAssistant"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_PATH = LOG_DIR / "latest.log"
def log_line(msg:str):
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}\n")
    except Exception:
        pass

# ---------- DB ----------
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS file_events(
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 file_name TEXT, event_time TEXT, ext TEXT, dir TEXT,
 event_type TEXT, src_path TEXT, dest_path TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_time ON file_events(event_time);
CREATE INDEX IF NOT EXISTS idx_events_name ON file_events(file_name);

CREATE TABLE IF NOT EXISTS tasks(
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 task_text TEXT NOT NULL, due_date TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_due ON tasks(due_date);

CREATE TABLE IF NOT EXISTS settings(
 id INTEGER PRIMARY KEY CHECK (id=1),
 watch_dir TEXT, extensions TEXT, remind_hour INTEGER DEFAULT 9
);
INSERT OR IGNORE INTO settings(id,watch_dir,extensions,remind_hour) VALUES(1,'','',9);
"""
def db_connect():
    DB_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn

# ---------- Utilities ----------
def is_safe_watch_dir(path_str: str) -> bool:
    try:
        p = Path(path_str).resolve(); home = Path.home().resolve()
        return (home in p.parents) or (p == home)
    except Exception:
        return False

def show_toast(title:str, msg:str, duration:int=6):
    try:
        if ToastNotifier and platform.system()=="Windows":
            ToastNotifier().show_toast(title, msg, duration=duration, threaded=True)
        else:
            log_line(f"[TOAST]{title} | {msg}")
    except Exception as e:
        log_line(f"toast error: {e}")

def next_business_day(dt:datetime, hour:int=DEFAULT_REMIND_HOUR):
    nd = dt + timedelta(days=1)
    while nd.weekday() >= 5: nd += timedelta(days=1)
    return nd.replace(hour=hour, minute=0, second=0, microsecond=0)

def ensure_task_scheduler(hour:int):
    if platform.system()!="Windows": return False
    task_name = APP_NAME + "_DailyReminder"
    time_str = f"{hour:02d}:00"
    if getattr(sys,'frozen',False):
        tr = f'"{sys.executable}" --remind'
    else:
        tr = f'"{sys.executable}" "{Path(__file__).resolve()}" --remind'
    cmd = ['schtasks','/Create','/F','/SC','DAILY','/TN',task_name,'/TR',tr,'/ST',time_str]
    try:
        subprocess.run(cmd, check=True); return True
    except Exception as e:
        log_line(f"schtasks error: {e}"); return False

# ---- NLQ (간단 한국어 자연어) ----
def parse_nl_query(nlq:str):
    text = (nlq or '').strip()
    if not text: return {}
    now = datetime.now(); d0 = now.replace(hour=0,minute=0,second=0,microsecond=0)
    def drange(t:str):
        t2 = t.replace(' ','')
        if '오늘' in t2: return d0, d0+timedelta(days=1)-timedelta(seconds=1)
        if '어제' in t2: return d0-timedelta(days=1), d0-timedelta(seconds=1)
        if '이번주' in t2:
            s = d0 - timedelta(days=d0.weekday()); return s, s+timedelta(days=7)-timedelta(seconds=1)
        if '지난주' in t2:
            s = d0 - timedelta(days=d0.weekday()+7); return s, s+timedelta(days=7)-timedelta(seconds=1)
        if '이번달' in t2:
            s = d0.replace(day=1); nm = s.replace(year=s.year+1,month=1) if s.month==12 else s.replace(month=s.month+1)
            return s, nm-timedelta(seconds=1)
        if '지난달' in t2:
            ft = d0.replace(day=1); lp = ft-timedelta(seconds=1)
            return lp.replace(day=1,hour=0,minute=0,second=0,microsecond=0), lp
        if '~' in t:
            a,b = t.split('~',1)
            try:
                s = datetime.fromisoformat(a.strip()[:10]+' 00:00:00')
                e = datetime.fromisoformat(b.strip()[:10]+' 23:59:59'); return s,e
            except: pass
        for part in t.split():
            if len(part)>=10 and part[4]=='-' and part[7]=='-':
                try:
                    s = datetime.fromisoformat(part[:10]+' 00:00:00')
                    e = datetime.fromisoformat(part[:10]+' 23:59:59'); return s,e
                except: pass
        return None, None
    events_map = {'생성':'created','만들':'created','추가':'created','수정':'modified','변경':'modified',
                  '이동':'moved','옮기':'moved','삭제':'deleted','없어지':'deleted','제거':'deleted'}
    event_types=[]
    for k,v in events_map.items():
        if k in text and v not in event_types: event_types.append(v)
    ext_syn={'워드':['.doc','.docx'],'엑셀':['.xls','.xlsx'],'한글':['.hwp'],
            '파워포인트':['.ppt','.pptx'],'파포':['.ppt','.pptx'],'pdf':['.pdf'],
            '텍스트':['.txt'],'이미지':['.png','.jpg','.jpeg'],'사진':['.png','.jpg','.jpeg']}
    extensions=[]
    for tok in text.replace(',',' ').split():
        if tok.startswith('.') and len(tok)<=6: extensions.append(tok.lower())
    tl=text.lower()
    for syn,lst in ext_syn.items():
        if syn.lower() in tl:
            for e in lst:
                if e not in extensions: extensions.append(e)
    s,e = drange(text)
    start = s.isoformat(sep=' ') if s else None
    end   = e.isoformat(sep=' ') if e else None
    drop={'오늘','어제','이번','지난','이번주','지난주','이번달','지난달','파일','확장자','중','포함'}
    keywords=[]
    for tok in text.replace(',',' ').split():
        if tok in drop or tok.startswith('.'): continue
        if any(k in tok for k in events_map.keys()): continue
        if tok.lower() in ext_syn: continue
        keywords.append(tok)
    return {'keyword':' '.join(keywords),'start':start,'end':end,'event_types':event_types,'extensions':extensions}

# ---------- Watcher ----------
class FSHandler(FileSystemEventHandler):
    def __init__(self, conn, exts):
        super().__init__(); self.conn=conn; self.exts={e.lower().strip() for e in exts if e.strip()}
    def _log(self, etype, src=None, dst=None):
        try:
            p = Path(dst or src); ext=p.suffix.lower()
            if self.exts and ext not in self.exts: return
            now = datetime.now().isoformat(timespec='seconds')
            with self.conn:
                self.conn.execute(
                    "INSERT INTO file_events(file_name,event_time,ext,dir,event_type,src_path,dest_path) VALUES(?,?,?,?,?,?,?)",
                    (p.name, now, ext, str(p.parent), etype, src, dst)
                )
        except Exception as e: log_line(f"log error: {e}")
    def on_created(self, ev):  # type: ignore
        if getattr(ev,'is_directory',False): return
        self._log('created', src=ev.src_path)
    def on_modified(self, ev):
        if getattr(ev,'is_directory',False): return
        self._log('modified', src=ev.src_path)
    def on_moved(self, ev):
        if getattr(ev,'is_directory',False): return
        self._log('moved', src=ev.src_path, dst=ev.dest_path)
    def on_deleted(self, ev):
        if getattr(ev,'is_directory',False): return
        self._log('deleted', src=ev.src_path)

class MultiWatcher:
    def __init__(self, paths, exts, conn):
        self.paths=paths; self.exts=exts; self.conn=conn; self.observer=None
    def start(self):
        if Observer is None: raise RuntimeError("watchdog 미설치: py -m pip install watchdog")
        if self.observer: return
        h=FSHandler(self.conn,self.exts); obs=Observer()
        for p in self.paths: obs.schedule(h, p, recursive=True)
        obs.start(); self.observer=obs
    def stop(self):
        if self.observer:
            self.observer.stop(); self.observer.join(timeout=5); self.observer=None

# ---------- Query / Export ----------
def to_rows(records):
    return [[r[0],r[1],r[2],r[3],r[4],r[5]] for r in records]

def search_events(conn, keyword='', start=None, end=None, ext_filter='', event_types=None):
    q="SELECT id,file_name,event_time,ext,dir,event_type,src_path,dest_path FROM file_events WHERE 1=1"
    params=[]
    if keyword:
        q+=" AND (file_name LIKE ? OR dir LIKE ? OR event_type LIKE ?)"
        k=f"%{keyword}%"; params+=[k,k,k]
    if start: q+=" AND event_time >= ?"; params.append(start)
    if end:   q+=" AND event_time <= ?"; params.append(end)
    if isinstance(ext_filter,(list,tuple)):
        exts=[e.lower() for e in ext_filter if e]; 
        if exts:
            q+=f" AND ext IN ({','.join('?'*len(exts))})"; params+=exts
    elif ext_filter:
        q+=" AND ext = ?"; params.append(str(ext_filter).lower())
    if event_types:
        q+=f" AND event_type IN ({','.join('?'*len(event_types))})"; params+=event_types
    q+=" ORDER BY event_time DESC LIMIT 1000"
    return conn.execute(q, params).fetchall()

def export_csv(conn, path):
    cur = conn.execute("SELECT file_name,event_time,ext,dir,event_type,src_path,dest_path FROM file_events ORDER BY event_time DESC")
    with open(path,'w',newline='',encoding='utf-8') as f:
        w=csv.writer(f); w.writerow(["file_name","event_time","ext","dir","event_type","src_path","dest_path"]); w.writerows(cur)

# ---------- Memo ----------
def parse_memo(text:str):
    blocks=[b.strip() for b in text.strip().split("\n\n") if b.strip()]
    out=[]
    for i,b in enumerate(blocks,1):
        lines=b.splitlines(); title=lines[0][:120]; details=" ".join(lines[1:])[:500]
        out.append({"idx":i,"title":title,"details":details})
    return out

def summarize_memo(text:str):
    items=parse_memo(text)
    if not items: return "(메모가 비어 있습니다)"
    return "오늘 메모 요약:\n" + "\n".join(f"- #{it['idx']}: {it['title']}" for it in items)

def save_tasks(conn, indices, text, due_date):
    items=parse_memo(text); now=datetime.now().isoformat(timespec='seconds')
    with conn:
        for it in items:
            if it['idx'] in indices:
                conn.execute("INSERT INTO tasks(task_text,due_date,status,created_at) VALUES(?,?,?,?)",
                             (f"#{it['idx']} {it['title']}", due_date,'pending',now))

def get_due_tasks(conn, date_str):
    return conn.execute("SELECT id,task_text FROM tasks WHERE due_date=? AND status='pending' ORDER BY id",(date_str,)).fetchall()

# ---------- Permission self-check ----------
def check_dirs_permissions(dirs):
    lines_ok,lines_warn,lines_block=[],[],[]
    for one in dirs:
        pref=f"- {one}"
        if not os.path.isdir(one): lines_block.append(f"{pref}  [차단] 경로 없음"); continue
        if not is_safe_watch_dir(one): lines_block.append(f"{pref}  [차단] 홈 내부만 허용"); continue
        try: os.listdir(one)
        except Exception as e: lines_warn.append(f"{pref}  [경고] 읽기 오류: {e}"); continue
        try:
            p=Path(one)/".wa_perm_test.tmp"
            with open(p,'w',encoding='utf-8') as f: f.write('ok')
            try: p.unlink()
            except: pass
            lines_ok.append(f"{pref}  [정상] 접근 가능")
        except Exception as e: lines_warn.append(f"{pref}  [경고] 쓰기 실패: {e}")
    r=["권한 자가 점검 결과",""]
    if lines_block: r+=["[차단]",*lines_block,""]
    if lines_warn:  r+=["[경고]",*lines_warn,""]
    if lines_ok:    r+=["[정상]",*lines_ok]
    if not(lines_ok or lines_warn or lines_block): r.append("점검 대상 없음")
    return "\n".join(r)

# ---------- GUI ----------
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("File Work Logger (tk)")
        self.geometry("1100x720")
        self.minsize(900,600)

        self.conn=db_connect()
        with self.conn: self.conn.executescript(SCHEMA_SQL)
        row=self.conn.execute("SELECT watch_dir,extensions,remind_hour FROM settings WHERE id=1").fetchone()
        self.watch_dir=row[0]; self.extensions=row[1]; self.remind_hour=row[2]

        self.watcher=None

        self.create_widgets()
        self.refresh_table()

        # ✅ 자동 새로고침: 3초마다 테이블 갱신
        self.after(3000, self.auto_refresh)

    # 자동 새로고침 루프
    def auto_refresh(self):
        try:
            self.refresh_table()
        finally:
            self.after(3000, self.auto_refresh)

    # UI
    def create_widgets(self):
        # Top controls
        top=ttk.Frame(self); top.pack(fill="x", padx=8, pady=8)

        ttk.Label(top,text="감시 폴더(;로 다중)").grid(row=0,column=0,sticky="w")
        self.e_dir=tk.Entry(top,width=60); self.e_dir.grid(row=0,column=1,sticky="w")
        self.e_dir.insert(0,self.watch_dir)
        ttk.Button(top,text="찾기",command=self.browse_dir).grid(row=0,column=2,padx=4)
        ttk.Button(top,text="자가점검",command=self.self_check).grid(row=0,column=3,padx=4)

        ttk.Label(top,text="확장자(.docx;.xlsx;...)").grid(row=1,column=0,sticky="w")
        self.e_ext_all=tk.Entry(top,width=60); self.e_ext_all.grid(row=1,column=1,sticky="w"); self.e_ext_all.insert(0,self.extensions)
        ttk.Button(top,text="감시 시작",command=self.start_watch).grid(row=1,column=2,padx=4)
        ttk.Button(top,text="정지",command=self.stop_watch).grid(row=1,column=3,padx=4)

        # Search row
        ttk.Label(top,text="검색").grid(row=2,column=0,sticky="w")
        self.e_search=tk.Entry(top,width=30); self.e_search.grid(row=2,column=1,sticky="w")
        ttk.Label(top,text="시작").grid(row=2,column=2,sticky="e"); self.e_from=tk.Entry(top,width=18); self.e_from.grid(row=2,column=3,sticky="w")
        ttk.Label(top,text="종료").grid(row=2,column=4,sticky="e"); self.e_to=tk.Entry(top,width=18); self.e_to.grid(row=2,column=5,sticky="w")

        ttk.Label(top,text="필터 확장자").grid(row=2,column=6,sticky="e"); self.e_fext=tk.Entry(top,width=10); self.e_fext.grid(row=2,column=7,sticky="w")
        ttk.Button(top,text="새로고침",command=self.refresh_table).grid(row=2,column=8,padx=4)
        ttk.Button(top,text="CSV 내보내기",command=self.export_csv_dialog).grid(row=2,column=9,padx=4)

        # NLQ row
        ttk.Label(top,text="자연어 검색(예: 지난주 삭제 xlsx)").grid(row=3,column=0,sticky="w")
        self.e_nlq=tk.Entry(top,width=60); self.e_nlq.grid(row=3,column=1,columnspan=5,sticky="w")
        self.var_multi=tk.BooleanVar(value=False)
        ttk.Checkbutton(top,text="다중 확장자",variable=self.var_multi, command=self.toggle_multi).grid(row=3,column=6,sticky="w")
        self.e_fexts=tk.Entry(top,width=18, state="disabled"); self.e_fexts.grid(row=3,column=7,sticky="w")

        # Tree (log)
        cols=("ID","파일명","이벤트시각","확장자","디렉토리","이벤트")
        self.tree=ttk.Treeview(self,columns=cols, show="headings")
        for c,w in zip(cols,(60,260,150,80,420,90)):
            self.tree.heading(c,text=c); self.tree.column(c,width=w,anchor="w")
        self.tree.pack(fill="both", expand=True, padx=8, pady=(0,6))

        # Memo frame
        memo=ttk.LabelFrame(self,text="메모 & 리마인더")
        memo.pack(fill="both", expand=False, padx=8, pady=(0,8))
        ttk.Label(memo,text="리마인드 시각(0-23시)").grid(row=0,column=0,sticky="w")
        self.e_rhour=tk.Entry(memo,width=4); self.e_rhour.insert(0,str(self.remind_hour)); self.e_rhour.grid(row=0,column=1,sticky="w",padx=4)

        self.t_memo=tk.Text(memo,height=10,wrap="word"); self.t_memo.grid(row=1,column=0,columnspan=8,sticky="nsew",padx=(0,6),pady=4)
        memo.rowconfigure(1,weight=1); memo.columnconfigure(7,weight=1)

        ttk.Button(memo,text="요약",command=self.do_summary).grid(row=2,column=0,sticky="w",pady=4)
        ttk.Button(memo,text="미처리건 선택 저장",command=self.save_pending_dialog).grid(row=2,column=1,sticky="w",padx=4)
        ttk.Button(memo,text="오늘 알림 테스트",command=self.test_today).grid(row=2,column=2,sticky="w",padx=4)
        ttk.Button(memo,text="리마인드 예약(매일)",command=self.schedule_daily).grid(row=2,column=3,sticky="w",padx=4)

        self.t_out=tk.Text(memo,height=5,state="disabled"); self.t_out.grid(row=3,column=0,columnspan=8,sticky="nsew",pady=(4,6))

    # Handlers
    def toggle_multi(self):
        self.e_fexts.config(state=("normal" if self.var_multi.get() else "disabled"))

    def browse_dir(self):
        d = filedialog.askdirectory()
        if not d: return
        cur = self.e_dir.get().strip()
        self.e_dir.delete(0,tk.END)
        self.e_dir.insert(0, (cur+"; "+d) if cur else d)

    def self_check(self):
        d = self.e_dir.get().strip()
        if not d: 
            messagebox.showinfo("안내","점검할 폴더를 입력하세요."); return
        dirs=[x.strip() for x in d.split(';') if x.strip()]
        messagebox.showinfo("권한 자가 점검", check_dirs_permissions(dirs))

    def start_watch(self):
        d = self.e_dir.get().strip()
        exts_raw = self.e_ext_all.get().strip()
        if not d:
            messagebox.showwarning("경고","유효한 폴더를 입력하세요."); return
        dirs=[x.strip() for x in d.split(';') if x.strip()]
        bad=[]
        for one in dirs:
            if not os.path.isdir(one) or not is_safe_watch_dir(one):
                bad.append(one)
        if bad:
            messagebox.showerror("오류","허용되지 않거나 존재하지 않는 경로:\n"+"\n".join(bad)); return
        exts=[e if e.startswith('.') else ('.'+e if e else '') for e in exts_raw.split(';')]

        with self.conn:
            self.conn.execute("UPDATE settings SET watch_dir=?, extensions=? WHERE id=1",(d,exts_raw))

        try:
            if self.watcher: self.watcher.stop()
            self.watcher = MultiWatcher(dirs, exts, self.conn)
            self.watcher.start()
            messagebox.showinfo("안내","감시 시작됨")
        except Exception as e:
            messagebox.showerror("오류","감시 시작 실패: "+str(e))

    def stop_watch(self):
        if self.watcher:
            self.watcher.stop(); self.watcher=None
            messagebox.showinfo("안내","감시 중지됨")

    def refresh_table(self):
        nl = parse_nl_query(self.e_nlq.get().strip()) if self.e_nlq.get().strip() else {}
        if self.var_multi.get():
            mult=self.e_fexts.get().strip()
            if mult:
                ext_filter=[e.lower() if e.startswith('.') else ('.'+e.lower() if e else '') for e in mult.split(';') if e.strip()]
            else:
                ext_filter=nl.get('extensions','')
        else:
            ext_filter=self.e_fext.get().lower().strip() or (nl.get('extensions')[0] if nl.get('extensions') else '')

        start_v=self.e_from.get().strip() or nl.get('start')
        end_v  =self.e_to.get().strip()   or nl.get('end')
        keyword=' '.join(x for x in [self.e_search.get().strip(), nl.get('keyword','')] if x)

        rows=search_events(self.conn, keyword=keyword, start=start_v, end=end_v,
                           ext_filter=ext_filter, event_types=nl.get('event_types'))
        for i in self.tree.get_children(): self.tree.delete(i)
        for r in to_rows(rows): self.tree.insert('', 'end', values=r)

    def export_csv_dialog(self):
        path=filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("CSV","*.csv")])
        if not path: return
        try:
            export_csv(self.conn, path); messagebox.showinfo("안내","내보내기 완료")
        except Exception as e:
            messagebox.showerror("오류","내보내기 실패: "+str(e))

    def do_summary(self):
        s=summarize_memo(self.t_memo.get("1.0",tk.END))
        self.t_out.config(state="normal"); self.t_out.delete("1.0",tk.END); self.t_out.insert("1.0",s); self.t_out.config(state="disabled")

    def save_pending_dialog(self):
        items=parse_memo(self.t_memo.get("1.0",tk.END))
        if not items: messagebox.showinfo("안내","저장할 항목이 없습니다."); return
        try:
            hour=int(self.e_rhour.get().strip() or self.remind_hour)
        except: hour=self.remind_hour
        due_dt=next_business_day(datetime.now(),hour)

        sel_win=tk.Toplevel(self); sel_win.title("미처리건 선택")
        vars_map={}
        ttk.Label(sel_win,text="미처리 항목을 선택하세요").pack(anchor="w", padx=8, pady=6)
        frm=ttk.Frame(sel_win); frm.pack(fill="both",expand=True,padx=8,pady=4)
        for it in items:
            v=tk.BooleanVar(value=True); vars_map[it['idx']]=v
            ttk.Checkbutton(frm,text=f"#{it['idx']}: {it['title']}",variable=v).pack(anchor="w")
        btnbar=ttk.Frame(sel_win); btnbar.pack(fill="x", padx=8, pady=6)
        def sel_all(val:bool):
            for v in vars_map.values(): v.set(val)
        ttk.Button(btnbar,text="전체 선택",command=lambda: sel_all(True)).pack(side="left")
        ttk.Button(btnbar,text="전체 해제",command=lambda: sel_all(False)).pack(side="left")
        def do_save():
            indices=[k for k,v in vars_map.items() if v.get()]
            if not indices: messagebox.showwarning("경고","선택된 항목이 없습니다."); return
            save_tasks(self.conn, indices, self.t_memo.get("1.0",tk.END), due_dt.strftime('%Y-%m-%d'))
            messagebox.showinfo("안내","미처리건 저장됨"); sel_win.destroy()
        ttk.Button(btnbar,text="저장",command=do_save).pack(side="right")
        ttk.Button(btnbar,text="닫기",command=sel_win.destroy).pack(side="right",padx=4)

    def test_today(self):
        today=datetime.now().strftime('%Y-%m-%d')
        tasks=get_due_tasks(self.conn,today)
        if not tasks: show_toast("오늘의 미처리건","미처리건이 없습니다")
        else: show_toast("오늘의 미처리건","\n".join(t[1] for t in tasks))

    def schedule_daily(self):
        try:
            hour=int(self.e_rhour.get().strip() or self.remind_hour)
        except: hour=self.remind_hour
        with self.conn: self.conn.execute("UPDATE settings SET remind_hour=? WHERE id=1",(hour,))
        ok=ensure_task_scheduler(hour)
        messagebox.showinfo("안내","매일 알림 예약 완료" if ok else "스케줄 등록 실패(Windows 전용)")

# ---------- Reminder mode ----------
def reminder_mode():
    conn=db_connect()
    today=datetime.now().strftime('%Y-%m-%d')
    tasks=get_due_tasks(conn,today)
    if not tasks: show_toast("오늘의 미처리건","미처리건이 없습니다. 좋은 하루!")
    else: show_toast("오늘의 미처리건","\n".join(t[1] for t in tasks), duration=min(12,4+len(tasks)))

# ---------- Entry ----------
def main():
    try:
        parser=argparse.ArgumentParser()
        parser.add_argument('--remind',action='store_true')
        args=parser.parse_args()
        if args.remind:
            reminder_mode(); return
        app=App(); app.mainloop()
    except Exception as e:
        tb=traceback.format_exc()
        log_line("FATAL:\n"+tb)
        messagebox.showerror("오류", f"프로그램 오류가 발생했습니다.\n\n{e}\n\n로그: {LOG_PATH}")
        raise

if __name__=="__main__":
    main()
