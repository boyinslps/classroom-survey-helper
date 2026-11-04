# -*- coding: utf-8 -*-
"""校園問卷帳密助手(區網專用)

設計重點
  * 只在教師電腦上執行,老師上課時才開啟。
  * 只接受「教室所在區網」的連線;管理頁只允許老師電腦本機(localhost)。
  * 程式不會主動連到任何外部網站,頁面也不載入任何外部資源。
  * 名單加密後存在硬碟,金鑰受 Windows 帳號保護;老師不需要設定或輸入密碼。
  * 學生只會拿到「帳號」與「密碼」,不會取得姓名、學號等其他欄位。
"""
from __future__ import annotations

import csv
import hmac
import io
import ipaddress
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
import unicodedata
import webbrowser
from collections import Counter, defaultdict, deque
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.fernet import Fernet, InvalidToken
from flask import (Flask, abort, flash, jsonify, redirect, render_template,
                   request, send_file, session, url_for)

APP_DIR = Path(__file__).resolve().parent
DATA_DIR = APP_DIR / "data"
SETTINGS_FILE = APP_DIR / "settings.json"
KEY_FILE = DATA_DIR / "key.dat"
CONFIG_FILE = DATA_DIR / "config.json"
STUDENTS_FILE = DATA_DIR / "students.dat"
DONE_FILE = DATA_DIR / "done.dat"
LOG_FILE = DATA_DIR / "query_log.csv"

DEFAULT_SETTINGS = {
    "host": "0.0.0.0",
    "port": 8765,
    # "auto" = 自動允許本機所在的 /24 網段;也可改成 ["192.168.10.0/24", "10.1.0.0/16"]
    "allowed_networks": "auto",
    "max_queries_per_minute": 30,
}


# ───────────────────────── 設定 ─────────────────────────
def load_settings() -> dict:
    settings = dict(DEFAULT_SETTINGS)
    if SETTINGS_FILE.exists():
        try:
            settings.update(json.loads(SETTINGS_FILE.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            print("⚠ settings.json 讀取失敗,改用預設值。")
    else:
        try:
            SETTINGS_FILE.write_text(
                json.dumps(DEFAULT_SETTINGS, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass
    return settings


SETTINGS = load_settings()


# ───────────────────────── 網路 ─────────────────────────
def local_ipv4s() -> list[str]:
    ips: set[str] = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    try:  # UDP connect 只查詢本機路由,不會真的送出任何封包
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    return sorted(ip for ip in ips if not ip.startswith(("127.", "169.254.", "0.")))


def build_allowed_networks(settings: dict) -> list:
    nets = [ipaddress.ip_network("127.0.0.0/8"), ipaddress.ip_network("::1/128")]
    cfg = settings.get("allowed_networks", "auto")
    if cfg == "auto":
        for ip in local_ipv4s():
            nets.append(ipaddress.ip_network(f"{ip}/24", strict=False))
    else:
        for item in cfg:
            nets.append(ipaddress.ip_network(str(item), strict=False))
    return nets


ALLOWED_NETS = build_allowed_networks(SETTINGS)


def client_ip():
    try:
        ip = ipaddress.ip_address(request.remote_addr or "")
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip


# ───────────────────────── 狀態 ─────────────────────────
def class_sort_key(c: str):
    return (0, int(c), c) if c.isdigit() else (1, 0, c)


seat_sort_key = class_sort_key


# ── 班級輸入:支援 505、5年5班、五年五班、五年十二班… ──
_CN_DIGIT = {"零": 0, "〇": 0, "○": 0, "一": 1, "二": 2, "兩": 2, "三": 3, "四": 4,
             "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_NUM = "0-9零〇○一二兩三四五六七八九十"
_GRADE_CLASS = re.compile(rf"([{_NUM}]+)年級?([{_NUM}]+)班?")


def _digit(ch: str) -> int:
    return int(ch) if ch.isdigit() else _CN_DIGIT[ch]


def cn_to_int(s: str) -> int | None:
    """'5'、'五'、'十二'、'二十三'、'一〇' → 數字;看不懂回傳 None。"""
    try:
        if "十" in s:
            a, _, b = s.partition("十")
            return (_digit(a) if a else 1) * 10 + (_digit(b) if b else 0)
        return int("".join(str(_digit(ch)) for ch in s))
    except (KeyError, ValueError):
        return None


def resolve_class(token: str, classes: list[str]) -> str | None:
    """把老師輸入的一個班級說法,對應到名單裡的班級代碼;找不到回傳 None。"""
    t = token.strip()
    if t in classes:
        return t
    t = unicodedata.normalize("NFKC", t).replace(" ", "")
    if t in classes:
        return t
    cands = []
    m = _GRADE_CLASS.fullmatch(t)
    if m:
        g, c = cn_to_int(m.group(1)), cn_to_int(m.group(2))
        if g and c and g < 10:
            cands.append(f"{g}{c:02d}")
    m = re.fullmatch(r"(\d+)班?", t)
    if m:
        cands.append(m.group(1))
    for cand in cands:
        if cand in classes:
            return cand
        hit = [k for k in classes if k.isdigit() and int(k) == int(cand)] if cand.isdigit() else []
        if hit:
            return hit[0]
    return None


def resolve_classes(text: str, classes: list[str]) -> tuple[list[str], list[str]]:
    """一次輸入多個班級(逗號、頓號、空白分隔)。回傳 (對得到的班級, 看不懂的項目)。"""
    good, bad = [], []
    for tok in re.split(r"[,，、;；\s]+", text.strip()):
        if not tok:
            continue
        cls = resolve_class(tok, classes)
        if cls is None:
            bad.append(tok)
        elif cls not in good:
            good.append(cls)
    return good, bad


def _cn_num(n: int) -> str:
    d = "零一二三四五六七八九"
    if n < 10:
        return d[n]
    return ("" if n < 20 else d[n // 10]) + "十" + (d[n % 10] if n % 10 else "")


def class_label(code: str) -> str | None:
    """505 → '五年五班';不是三位數班級代碼就回傳 None。"""
    if re.fullmatch(r"[1-9]\d{2}", code) and int(code[1:]) >= 1:
        return f"{_cn_num(int(code[0]))}年{_cn_num(int(code[1:]))}班"
    return None


class Store:
    def __init__(self):
        self.lock = threading.RLock()
        self.key: bytes | None = None          # 載入後才有;只存在記憶體
        self.load_failed = False               # 硬碟上有舊名單但這台電腦讀不出來
        self.surveys: list[dict] = []          # 老師放的問卷 [{"name","url"}](可多個),學生頁面會顯示
        self.records: dict[tuple[str, str], dict] = {}
        self.classes: list[str] = []
        self.open: set[str] = set()
        self.done: dict[tuple[str, str], str] = {}   # (班級, 座號) -> 回報完成的時間
        self.paused = False
        self.log: deque = deque(maxlen=300)

    @property
    def unlocked(self) -> bool:
        return self.key is not None

    def class_open(self, cls: str) -> bool:
        """老師沒選任何班級 → 全部班級都能查;選了 → 只有被選的班級能查。"""
        return cls in self.classes and (not self.open or cls in self.open)

    def set_records(self, rows: list[dict]):
        self.records = {(r["class"], r["seat"]): r for r in rows}
        self.classes = sorted({r["class"] for r in rows}, key=class_sort_key)


store = Store()


# ───────────────────────── 加密金鑰(不需要密碼) ─────────────────────────
def _dpapi(data: bytes, protect: bool) -> bytes:
    """用 Windows 帳號保護資料(DPAPI):只有「同一台電腦的同一個 Windows 帳號」解得開。"""
    import ctypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.c_void_p)]

    buf = ctypes.create_string_buffer(data, len(data))
    src = Blob(len(data), ctypes.cast(buf, ctypes.c_void_p))
    out = Blob()
    fn = ctypes.windll.crypt32.CryptProtectData if protect else ctypes.windll.crypt32.CryptUnprotectData
    if not fn(ctypes.byref(src), None, None, None, None, 0, ctypes.byref(out)):
        raise OSError("DPAPI 失敗")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        ctypes.windll.kernel32.LocalFree(out.pbData)


def get_data_key() -> bytes:
    """名單的加密金鑰。第一次啟動自動產生,之後自動讀取,老師不需要輸入任何密碼。
    金鑰檔受 Windows 帳號保護:整個資料夾被複製到別台電腦,名單也讀不出來(需重新上傳)。"""
    DATA_DIR.mkdir(exist_ok=True)
    if KEY_FILE.exists():
        try:
            raw = KEY_FILE.read_bytes()
            return _dpapi(raw, False) if os.name == "nt" else raw
        except Exception:
            pass                                     # 讀不出來 → 重新產生,舊名單作廢
    key = Fernet.generate_key()
    KEY_FILE.write_bytes(_dpapi(key, True) if os.name == "nt" else key)
    return key


def save_students():
    token = Fernet(store.key).encrypt(
        json.dumps(list(store.records.values()), ensure_ascii=False).encode("utf-8"))
    tmp = STUDENTS_FILE.with_suffix(".tmp")
    tmp.write_bytes(token)
    os.replace(tmp, STUDENTS_FILE)


def save_done():
    token = Fernet(store.key).encrypt(
        json.dumps([[c, s, t] for (c, s), t in store.done.items()]).encode("utf-8"))
    tmp = DONE_FILE.with_suffix(".tmp")
    tmp.write_bytes(token)
    os.replace(tmp, DONE_FILE)


def unlock_store(key: bytes):
    with store.lock:
        first_time = store.key is None
        store.key = key
        if first_time:
            load_config()
            rows = []
            store.load_failed = False
            if STUDENTS_FILE.exists():
                try:
                    rows = json.loads(Fernet(key).decrypt(STUDENTS_FILE.read_bytes()))
                except (InvalidToken, ValueError):
                    rows = []
                    store.load_failed = True
            store.set_records(rows)
            store.open = set()              # 空集合 = 沒選班級 = 全部班級都能查
            store.paused = False
            store.done = {}
            if DONE_FILE.exists():
                try:
                    for c, s, t in json.loads(Fernet(key).decrypt(DONE_FILE.read_bytes())):
                        if (c, s) in store.records:
                            store.done[(c, s)] = t
                except (InvalidToken, ValueError):
                    pass


def ensure_store():
    """程式啟動(或第一次開管理頁)時自動載入名單,不需要登入。"""
    if not store.unlocked:
        unlock_store(get_data_key())


def valid_survey_url(url: str) -> bool:
    parts = urlsplit(url)
    return (len(url) <= 500 and parts.scheme in ("http", "https") and bool(parts.netloc)
            and not any(ch.isspace() for ch in url))


MAX_SURVEYS = 10


def parse_surveys(names: list[str], urls: list[str]) -> tuple[list[dict], str | None]:
    """管理頁每一列「名稱 + 連結」。回傳 (整理好的清單, 錯誤訊息或 None)。
    名稱可以空白;連結空白的列直接略過(名稱也空白時),重複的連結只留第一個。"""
    out: list[dict] = []
    for i, url in enumerate(urls, 1):
        name = (names[i - 1] if i - 1 < len(names) else "").strip()[:40]
        url = url.strip()
        if not name and not url:
            continue
        if not url:
            return [], f"第 {i} 列「{name}」還沒有填連結。"
        if not valid_survey_url(url):
            return [], f"第 {i} 列的連結格式不對(要以 http:// 或 https:// 開頭,不能有空白):{url[:60]}"
        if all(s["url"] != url for s in out):
            out.append({"name": name, "url": url})
    if len(out) > MAX_SURVEYS:
        return [], f"最多 {MAX_SURVEYS} 個問卷連結。"
    return out, None


def survey_items(surveys: list[dict]) -> list[dict]:
    """給學生端顯示用:沒填名稱的補上「問卷 N」。"""
    return [{"name": s["name"] or f"問卷 {i}", "url": s["url"]} for i, s in enumerate(surveys, 1)]


def save_config():
    CONFIG_FILE.write_text(json.dumps({"surveys": store.surveys}, ensure_ascii=False),
                           encoding="utf-8")


def load_config():
    try:
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        items = cfg.get("surveys")
        if items is None:                               # 舊版:只有網址清單或單一網址
            old = cfg.get("survey_urls")
            if old is None:
                old = [cfg.get("survey_url", "")]
            items = [{"name": "", "url": u} for u in old]
        store.surveys = [{"name": str(s.get("name", ""))[:40], "url": s["url"]}
                         for s in items if isinstance(s, dict) and isinstance(s.get("url"), str)
                         and valid_survey_url(s["url"])][:MAX_SURVEYS]
    except (OSError, ValueError, AttributeError, TypeError):
        store.surveys = []


# ───────────────────────── Excel / 名單解析 ─────────────────────────
def cell_str(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, float):
        return str(int(v)) if v.is_integer() else repr(v)
    return str(v).strip()


class _TableParser(HTMLParser):
    """有些系統匯出的「.xls」其實是 HTML 表格,用這個讀取。"""

    def __init__(self):
        super().__init__()
        self.rows: list[list[str]] = []
        self._row = None
        self._cell = None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append("".join(self._cell).strip())
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None


def iter_sheets(data: bytes):
    """依檔案內容(而非副檔名)判斷格式,逐一產生 (工作表名稱, 二維列表)。"""
    if data[:2] == b"PK":                                   # .xlsx
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        try:
            for ws in wb.worksheets:
                yield ws.title, [list(r) for r in ws.iter_rows(values_only=True)]
        finally:
            wb.close()
    elif data[:4] == b"\xd0\xcf\x11\xe0":                   # 舊版 .xls
        import xlrd
        wb = xlrd.open_workbook(file_contents=data)
        for sh in wb.sheets():
            yield sh.name, [[sh.cell_value(r, c) for c in range(sh.ncols)] for r in range(sh.nrows)]
    else:
        text = None
        for enc in ("utf-8-sig", "cp950"):
            try:
                text = data.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        if text is None:
            raise ValueError("無法辨識檔案格式,請使用 .xls 或 .xlsx。")
        if "<table" in text.lower():
            parser = _TableParser()
            parser.feed(text)
            yield "html", parser.rows
        else:
            yield "csv", list(csv.reader(io.StringIO(text)))


REQUIRED = ("班級", "座號", "密碼")
OPTIONAL = ("姓名", "學號", "帳號")


def parse_students(data: bytes) -> tuple[list[dict], dict]:
    records: dict[tuple[str, str], dict] = {}
    report = {"sheets": 0, "duplicates": 0, "skipped": 0}
    for _name, rows in iter_sheets(data):
        header_row, idx = None, {}
        for i, row in enumerate(rows[:15]):
            labels = [cell_str(c).replace(" ", "").replace("　", "") for c in row]
            if all(k in labels for k in REQUIRED):
                header_row = i
                idx = {k: labels.index(k) for k in REQUIRED + OPTIONAL if k in labels}
                break
        if header_row is None:
            continue
        report["sheets"] += 1
        for row in rows[header_row + 1:]:
            def get(key):
                j = idx.get(key)
                return cell_str(row[j]) if j is not None and j < len(row) else ""

            cls, seat, pw = get("班級"), get("座號"), get("密碼")
            if seat.isdigit():
                seat = str(int(seat))
            if not cls or not seat:
                if any(cell_str(c) for c in row):
                    report["skipped"] += 1
                continue
            if (cls, seat) in records:
                report["duplicates"] += 1
                continue
            records[(cls, seat)] = {
                "class": cls, "seat": seat, "account": get("帳號"), "password": pw,
                "name": get("姓名"), "sid": get("學號"),
            }
    if not records:
        raise ValueError("找不到名單:檔案需要有「班級、座號、密碼」欄位(帳號、姓名、學號為選填)。")
    return list(records.values()), report


# ───────────────────────── Flask ─────────────────────────
app = Flask(__name__)
app.secret_key = secrets.token_bytes(32)            # 每次啟動重新產生 → 重啟後需重新登入
app.config.update(
    MAX_CONTENT_LENGTH=10 * 1024 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Strict",
    SESSION_COOKIE_NAME="admin_session",
    JSON_AS_ASCII=False,
)
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


@app.errorhandler(403)
def _forbidden(_e):
    return "此網路不允許存取。", 403


@app.errorhandler(413)
def _too_large(_e):
    return "檔案太大(上限 10MB)。", 413


@app.before_request
def guard():
    ip = client_ip()
    if ip is None or not any(ip in n for n in ALLOWED_NETS):
        abort(403)
    if request.path.startswith("/admin"):
        host = (urlsplit("//" + request.host).hostname or "").lower()
        if not ip.is_loopback or host not in LOOPBACK_HOSTS:
            abort(403)
        ensure_store()
        if request.method == "POST":
            sent = request.form.get("csrf", "")
            if not sent or not hmac.compare_digest(sent, session.get("csrf", "")):
                abort(400)


@app.after_request
def secure_headers(resp):
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; frame-ancestors 'none'; "
        "form-action 'self'; base-uri 'none'")
    return resp


@app.context_processor
def inject_csrf():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(24)
    return {"csrf": session["csrf"]}


# ── 速率限制 ──
_hits: dict[str, deque] = defaultdict(deque)
_blocked_until: dict[str, float] = {}
_rate_lock = threading.Lock()


def rate_ok(ip: str) -> bool:
    now = time.time()
    with _rate_lock:
        if _blocked_until.get(ip, 0) > now:
            return False
        dq = _hits[ip]
        while dq and dq[0] < now - 60:
            dq.popleft()
        dq.append(now)
        if len(dq) > int(SETTINGS["max_queries_per_minute"]):
            _blocked_until[ip] = now + 60
            return False
    return True


# ── 查詢紀錄(不記錄密碼) ──
_log_lock = threading.Lock()


def csv_safe(text: str) -> str:
    """學生輸入的字會寫進 CSV;開頭是 = + - @ 時,Excel 會當公式執行,所以前面加單引號擋掉。"""
    text = text.replace("\r", " ").replace("\n", " ")
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t") else text


def write_log(ip: str, cls: str, seat: str, result: str):
    entry = {"time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
             "ip": ip, "class": csv_safe(cls[:20]), "seat": csv_safe(seat[:10]), "result": result}
    with _log_lock:
        store.log.append(entry)
        try:
            new = not LOG_FILE.exists()
            DATA_DIR.mkdir(exist_ok=True)
            with open(LOG_FILE, "a", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f)
                if new:
                    w.writerow(["時間", "來源電腦", "班級", "座號", "結果"])
                w.writerow([entry["time"], ip, entry["class"], entry["seat"], result])
        except OSError:
            pass


# ───────────────────────── 學生端 ─────────────────────────
@app.get("/")
def student_page():
    return render_template("student.html")


@app.get("/api/status")
def api_status():
    with store.lock:
        survey = survey_items(store.surveys)
        if not store.unlocked:
            return jsonify(ok=False, message="老師尚未開放查詢。", classes=[], surveys=survey)
        if store.paused:
            return jsonify(ok=False, message="老師暫時關閉了查詢。", classes=[], surveys=survey)
        classes = [c for c in store.classes if store.class_open(c)]
    if not classes:
        return jsonify(ok=False, message="目前沒有可查詢的班級(還沒有匯入名單)。", classes=[], surveys=survey)
    labels = {c: class_label(c) for c in classes if class_label(c)}      # 505 → 五年五班
    return jsonify(ok=True, classes=classes, labels=labels, surveys=survey)


@app.post("/api/lookup")
def api_lookup():
    ip = str(client_ip())
    if not rate_ok(ip):
        return jsonify(error="查詢太頻繁,請稍候一分鐘再試。"), 429
    body = request.get_json(silent=True) or {}
    cls = str(body.get("class", "")).strip()
    seat = str(body.get("seat", "")).strip()
    if seat.isdigit():
        seat = str(int(seat))
    with store.lock:
        if not store.unlocked or store.paused:
            return jsonify(error="老師目前沒有開放查詢。"), 503
        if not store.class_open(cls):
            write_log(ip, cls, seat, "班級未開放")
            return jsonify(error="這個班級目前沒有開放查詢。"), 403
        rec = store.records.get((cls, seat))
        if rec is None:
            write_log(ip, cls, seat, "查無資料")
            return jsonify(error="查無這個座號,請確認班級與座號。"), 404
        write_log(ip, cls, seat, "成功")
        # 回傳帳號與密碼,外加姓名(讓學生對照是不是自己)與「是否已回報完成」
        return jsonify(account=rec["account"], password=rec["password"], name=rec["name"],
                       done=(cls, seat) in store.done)


@app.get("/api/seats")
def api_seats():
    """某個開放班級的座號與姓名,給學生端的座號下拉選單與對照用。"""
    cls = request.args.get("class", "").strip()
    with store.lock:
        if not store.unlocked or store.paused or not store.class_open(cls):
            return jsonify(seats=[])
        rows = sorted(((r["seat"], r["name"]) for (c, _s), r in store.records.items() if c == cls),
                      key=lambda x: seat_sort_key(x[0]))
    return jsonify(seats=[{"seat": s, "name": n} for s, n in rows])


@app.post("/api/done")
def api_done():
    """學生自我檢查:按下「我已經完成問卷」,老師端就會看到這個座號。"""
    ip = str(client_ip())
    if not rate_ok(ip):
        return jsonify(error="操作太頻繁,請稍候一分鐘再試。"), 429
    body = request.get_json(silent=True) or {}
    cls = str(body.get("class", "")).strip()
    seat = str(body.get("seat", "")).strip()
    if seat.isdigit():
        seat = str(int(seat))
    with store.lock:
        if not store.unlocked or store.paused:
            return jsonify(error="老師目前沒有開放。"), 503
        if not store.class_open(cls):
            return jsonify(error="這個班級目前沒有開放。"), 403
        if (cls, seat) not in store.records:
            return jsonify(error="查無這個座號。"), 404
        if (cls, seat) not in store.done:
            store.done[(cls, seat)] = datetime.now().strftime("%H:%M:%S")
            try:
                save_done()
            except OSError:
                pass
            write_log(ip, cls, seat, "回報完成")
        return jsonify(ok=True)


# ───────────────────────── 老師管理頁(僅限本機) ─────────────────────────
def student_urls() -> list[str]:
    return [f"http://{ip}:{SETTINGS['port']}" for ip in local_ipv4s()]


@app.get("/admin/login")
def admin_login():
    return redirect(url_for("admin_home"))          # 舊書籤:已經不需要登入


@app.get("/admin")
def admin_home():
    with store.lock:
        if store.load_failed:
            flash("這台電腦讀不出之前存的名單(可能整個資料夾是從別台電腦或別的 Windows 帳號複製來的),請重新上傳名單。")
            store.load_failed = False
        counts = Counter(r["class"] for r in store.records.values())
        classes = [(c, counts[c], c in store.open) for c in store.classes]
        ctx = dict(total=len(store.records), classes=classes, paused=store.paused,
                   open_names=[class_label(c) or f"{c} 班" for c in store.classes if c in store.open],
                   logs=list(reversed(store.log))[:60], surveys=[dict(s) for s in store.surveys],
                   open_classes=[c for c in store.classes if c in store.open])
    return render_template("admin.html", urls=student_urls(), port=SETTINGS["port"],
                           nets=[str(n) for n in ALLOWED_NETS if not n.is_loopback], **ctx)


@app.post("/admin/upload")
def admin_upload():
    f = request.files.get("file")
    if not f or not f.filename:
        flash("請先選擇檔案。")
        return redirect(url_for("admin_home"))
    try:
        rows, report = parse_students(f.read())
    except Exception as exc:  # 解析失敗不應讓伺服器出錯
        flash(f"匯入失敗:{exc}")
        return redirect(url_for("admin_home"))
    with store.lock:
        store.set_records(rows)
        store.open = set()
        store.paused = False
        store.done = {}
        save_students()
        DONE_FILE.unlink(missing_ok=True)
    msg = f"匯入成功:{len(rows)} 位學生、{len(store.classes)} 個班級。舊名單已被取代。"
    if report["duplicates"]:
        msg += f"(有 {report['duplicates']} 筆班級座號重複,只保留第一筆)"
    if report["skipped"]:
        msg += f"(略過 {report['skipped']} 筆缺班級或座號的資料)"
    flash(msg)
    return redirect(url_for("admin_home"))


@app.post("/admin/classes")
def admin_classes():
    action = request.form.get("action", "")
    with store.lock:
        if action == "set":
            # 輸入 505、五年五班、5年5班 都可以;多班用逗號分開;留空 = 全部班級
            good, bad = resolve_classes(request.form.get("classes", ""), store.classes)
            if bad:
                have = "、".join(store.classes[:30]) + (" …" if len(store.classes) > 30 else "")
                flash(f"找不到班級:{'、'.join(bad)}。名單裡有:{have or '(還沒有名單)'}。目前班級沒有改變。")
            else:
                store.open = set(good)
        elif action == "pause":
            store.paused = True
        elif action == "resume":
            store.paused = False
    return redirect(url_for("admin_home"))


@app.get("/admin/done.json")
def admin_done_json():
    """問卷完成狀況(管理頁每幾秒更新一次)。只列出開放中、或已有人完成的班級。"""
    with store.lock:
        totals = Counter(r["class"] for r in store.records.values())
        done_by = defaultdict(list)
        for (c, s) in store.done:
            done_by[c].append(s)
        classes = []
        for c in store.classes:
            if not store.class_open(c) and not done_by[c]:
                continue
            done = sorted(done_by[c], key=seat_sort_key)
            pending = sorted((s for (cc, s) in store.records if cc == c and s not in done),
                             key=seat_sort_key)
            classes.append({"class": c, "open": store.class_open(c), "total": totals[c],
                            "done": done, "pending": pending})
        return jsonify(done_total=len(store.done), total=len(store.records), classes=classes)


@app.post("/admin/done")
def admin_done_edit():
    action = request.form.get("action", "")
    with store.lock:
        if action == "unmark":
            key = (request.form.get("class", ""), request.form.get("seat", ""))
            store.done.pop(key, None)
        elif action == "clear_all":
            store.done = {}
        else:
            abort(400)
        try:
            save_done()
        except OSError:
            pass
    return jsonify(ok=True)


@app.post("/admin/survey")
def admin_survey():
    """設定問卷連結:每一列「名稱 + 連結」,可以有多列。學生查詢頁面會顯示名稱、連結與「前往」。"""
    surveys, error = parse_surveys(request.form.getlist("name"), request.form.getlist("url"))
    if error:
        flash(f"{error}沒有儲存,請修正後再按一次。")
        return redirect(url_for("admin_home"))
    with store.lock:
        store.surveys = surveys
        try:
            save_config()
        except OSError:
            pass
    flash(f"已儲存 {len(surveys)} 個問卷連結,學生頁面會看到。" if surveys else "已清除問卷連結。")
    return redirect(url_for("admin_home"))


@app.post("/admin/quit")
def admin_quit():
    """下課:關閉整個程式(名單與完成紀錄仍加密保留在這台電腦)。"""
    threading.Timer(0.7, os._exit, args=(0,)).start()
    return "<!doctype html><meta charset=utf-8><title>已關閉</title><p style='font:20px sans-serif;padding:2em'>程式已關閉,學生無法再查詢。可以關掉這個分頁。</p>"


@app.post("/admin/wipe")
def admin_wipe():
    with store.lock:
        store.set_records([])
        store.open = set()
        store.done = {}
        STUDENTS_FILE.unlink(missing_ok=True)
        DONE_FILE.unlink(missing_ok=True)
    flash("名單已全部刪除(含硬碟上的加密檔)。")
    return redirect(url_for("admin_home"))


@app.get("/admin/template.xlsx")
def admin_template():
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "名單"
    ws.append(["學校代碼", "學校", "學制", "班級", "座號", "姓名", "學號", "帳號", "密碼"])
    ws.append(["000000", "範例國小", "國小", "101", 1, "王小明", "111001", "示範帳號", "示範密碼"])
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name="名單範本.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


# ───────────────────────── 啟動 ─────────────────────────
FIREWALL_RULE = "ClassroomLookup-LAN-only"


def firewall_ready(port: int) -> bool:
    try:
        r = subprocess.run(["netsh", "advfirewall", "firewall", "show", "rule",
                            f"name={FIREWALL_RULE}"], capture_output=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return True                     # 查不到就不打擾老師
    return r.returncode == 0 and str(port).encode() in r.stdout


def ensure_firewall(port: int):
    """第一次啟動時自動設定防火牆:只讓「同一個區網」的電腦連到這個連接埠。
    需要系統管理員權限,所以會跳出一次 Windows 的「是否允許」視窗,按「是」即可。"""
    if os.name != "nt" or os.environ.get("CLASSROOM_NO_FIREWALL") == "1" or firewall_ready(port):
        return
    print(" 第一次使用:正在設定防火牆(只允許教室區網連入)。")
    print(" 請在跳出的 Windows 視窗按「是」。")
    cmd = (f"netsh advfirewall firewall delete rule name={FIREWALL_RULE} & "
           f"netsh advfirewall firewall add rule name={FIREWALL_RULE} dir=in action=allow "
           f"protocol=TCP localport={port} remoteip=LocalSubnet profile=any")
    ps = (f"Start-Process -FilePath cmd.exe -ArgumentList '/c {cmd}' "
          "-Verb RunAs -WindowStyle Hidden -Wait")
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       capture_output=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        pass
    if firewall_ready(port):
        print(" 防火牆設定完成。")
    else:
        print(" ⚠ 防火牆沒有設定成功:這台電腦自己能用,但學生電腦可能連不上。")
        print("   請關閉後重新開啟,並在視窗按「是」。")


def pick_port(host: str, start: int) -> int:
    """設定的連接埠被別的程式占用(例如 Windows 設定程式用了 5000)就自動換下一個。"""
    for p in range(start, start + 30):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind((host, p))
            return p
        except OSError:
            continue
        finally:
            s.close()
    return start


def main():
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    wanted = int(SETTINGS["port"])
    port = pick_port(SETTINGS["host"], wanted)
    SETTINGS["port"] = port
    ensure_firewall(port)
    ensure_store()
    print("=" * 56)
    print(" 校園問卷帳密助手(區網專用)")
    print("=" * 56)
    print(f" 老師管理頁(只有這台電腦能開): http://localhost:{port}/admin")
    for u in student_urls():
        print(f" 學生查詢網址: {u}")
    print(" 允許連線的網段: " + ", ".join(str(n) for n in ALLOWED_NETS if not n.is_loopback))
    print(" 結束服務:關閉此視窗或按 Ctrl+C")
    print("=" * 56)
    from waitress import serve
    admin_url = f"http://localhost:{port}/admin"
    if os.environ.get("CLASSROOM_NO_BROWSER") != "1":
        threading.Timer(1.5, webbrowser.open, args=(admin_url,)).start()
    import logging
    logging.getLogger("waitress.queue").setLevel(logging.ERROR)   # 全班同時按時偶爾的排隊提示不是錯誤
    try:
        serve(app, host=SETTINGS["host"], port=port, threads=8)
    except OSError as exc:
        print(f"無法啟動(連接埠 {port} 可能被占用,是不是已經開了一個?):{exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
