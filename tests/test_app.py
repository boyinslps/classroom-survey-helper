import sys, tempfile, io, re, json
from pathlib import Path as _P
ROOT = _P(__file__).resolve().parent.parent
from pathlib import Path
sys.path.insert(0, str(ROOT))
import app as A
tmp = Path(tempfile.mkdtemp())
A.DATA_DIR = tmp; A.KEY_FILE = tmp/"key.dat"; A.CONFIG_FILE = tmp/"config.json"; A.STUDENTS_FILE = tmp/"students.dat"; A.LOG_FILE = tmp/"log.csv"; A.DONE_FILE = tmp/"done.dat"
A.SETTINGS["max_queries_per_minute"] = 12
c = A.app.test_client()
def csrf(client=c, path="/admin"):
    html = client.get(path).get_data(as_text=True)
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)
ok = lambda cond, msg: print(("PASS " if cond else "FAIL ") + msg) or (cond or sys.exit(1))

# 1 程式尚未載入名單
r = c.get("/api/status").get_json(); ok(not r["ok"] and r["surveys"] == [], "尚未載入名單時學生看不到可查班級: " + r["message"])
ok(c.post("/api/lookup", json={"class":"101","seat":"1"}).status_code == 503, "尚未載入時查詢被拒 (503)")
ok(c.post("/api/done", json={"class":"101","seat":"1"}).status_code == 503, "尚未載入時回報完成被拒 (503)")

# 2 不需要密碼:直接進管理頁,金鑰自動產生
ok(c.get("/admin/login").status_code == 302, "舊的登入網址直接導回管理頁")
ok(c.get("/admin").status_code == 200 and A.store.unlocked, "不用密碼直接進管理頁")
ok(A.KEY_FILE.exists(), "加密金鑰自動產生")
ok(c.post("/admin/classes", data={"action":"pause"}).status_code == 400, "缺 CSRF 的 POST 被擋")

# 3 上傳 xls
def upload(path_or_bytes, name):
    data = Path(path_or_bytes).read_bytes() if isinstance(path_or_bytes, (str, Path)) else path_or_bytes
    t = csrf(path="/admin")
    return c.post("/admin/upload", data={"csrf":t, "file": (io.BytesIO(data), name)}, content_type="multipart/form-data", follow_redirects=True).get_data(as_text=True)
page = upload(str(ROOT / "sample" / "範例名單.xls"), "a.xls")
ok("匯入成功:10 位學生、2 個班級" in page, "匯入 .xls 成功")
ok(c.get("/api/status").get_json()["classes"] == ["101","102"], "沒選班級時預設全部班級都能查")
r = c.post("/api/lookup", json={"class":"101","seat":"1"}); d = r.get_json()
ok(r.status_code == 200 and set(d) == {"account","password","name","done"} and d["done"] is False and d["name"] == "學生1011", f"學生拿到帳號密碼,外加姓名供對照 {d}")
ok(d["account"] == "21360060101" and d["password"] == "11251", "數字儲存格沒變成 1.0 / 科學記號")
ok(c.post("/api/lookup", json={"class":"101","seat":"01"}).status_code == 200, "座號 01 等同 1")
ok(c.post("/api/lookup", json={"class":"101","seat":"99"}).status_code == 404, "查無座號 404")
page = upload(str(ROOT / "sample" / "範例名單.xlsx"), "a.xlsx"); ok("匯入成功" in page, "匯入 .xlsx 成功")

# 偽裝成 xls 的 HTML 表格
html = "<table><tr><th>班級</th><th>座號</th><th>帳號</th><th>密碼</th></tr><tr><td>301</td><td>7</td><td>acc7</td><td>pw7</td></tr></table>".encode("utf-8")
page = upload(html, "fake.xls"); ok("匯入成功:1 位學生" in page, "HTML 偽裝的 .xls 也能匯入")
page = upload(b"garbage bytes", "x.xls"); ok("匯入失敗" in page, "亂碼檔案顯示錯誤而不是當機")
upload(str(ROOT / "sample" / "範例名單.xls"), "a.xls")

# 4 目前班級(老師輸入)
st = {"REMOTE_ADDR":"127.0.0.2"}
t = csrf(path="/admin")
def set_classes(text): return c.post("/admin/classes", data={"csrf":csrf(path="/admin"), "action":"set", "classes":text}, follow_redirects=True).get_data(as_text=True)
def open_classes(): return c.get("/api/status").get_json()["classes"]
page = set_classes("102")
ok(c.post("/api/lookup", json={"class":"101","seat":"1"}).status_code == 403, "輸入 102 後,101 被擋")
ok(c.post("/api/lookup", json={"class":"102","seat":"1"}).status_code == 200, "102 可查")
ok(open_classes() == ["102"], "學生下拉選單只列出老師輸入的班級")
ok('value="102"' in page and "一年二班" in page, "管理頁顯示目前班級(輸入框與中文班名)")
set_classes("101, 102"); ok(open_classes() == ["101","102"], "可以同時輸入兩個班(逗號分開)")
set_classes("一年二班"); ok(open_classes() == ["102"], "輸入「一年二班」等於 102")
set_classes("1年1班、102"); ok(open_classes() == ["101","102"], "混用「1年1班」和代碼,頓號分開")
set_classes("102")
page = set_classes("三年三班")
ok("找不到班級:三年三班" in page and open_classes() == ["102"], "輸入找不到的班級會提示,而且不會改變目前班級")
set_classes(""); ok(open_classes() == ["101","102"] and A.store.open == set(), "留空 = 全部班級")
ok(c.get("/api/status").get_json()["labels"] == {"101":"一年一班","102":"一年二班"}, "學生端班級帶中文班名")
d = c.get("/api/seats?class=101", environ_overrides=st).get_json()["seats"]
ok([x["seat"] for x in d] == ["1","2","3","4","5"] and d[0]["name"] == "學生1011", "座號清單依座號排序並帶姓名")
ok(c.get("/api/seats?class=999").get_json()["seats"] == [], "不存在的班級沒有座號清單")
set_classes("102"); ok(c.get("/api/seats?class=101").get_json()["seats"] == [], "沒開放的班級拿不到座號清單"); set_classes("")
# 班級說法辨識(單元)
L = ["101", "505", "506", "510", "612"]
for text, want in [("505","505"), ("五年五班","505"), ("5年5班","505"), ("五年級5班","505"), ("505班","505"), ("５０５","505"), ("五年十班","510"), ("六年十二班","612"), (" 五年六班 ","506"), ("七年一班",None), ("五五",None), ("abc",None)]:
    ok(A.resolve_class(text, L) == want, f"班級說法 {text!r} → {want}")
ok(A.class_label("505") == "五年五班" and A.class_label("612") == "六年十二班" and A.class_label("520") == "五年二十班" and A.class_label("A1") is None and A.class_label("500") is None, "班級代碼轉中文班名")
ok(A.cn_to_int("二十三") == 23 and A.cn_to_int("十") == 10 and A.cn_to_int("一〇") == 10, "中文數字轉換")
c.post("/admin/classes", data={"csrf":t, "action":"pause"})
ok(c.post("/api/lookup", json={"class":"101","seat":"1"}).status_code == 503, "暫停後不可查")
c.post("/admin/classes", data={"csrf":t, "action":"resume"})

# 4b 我已經完成問卷(用另一個本機位址,避免跟後面的限流測試互相影響)
st = {"REMOTE_ADDR":"127.0.0.2"}
def done_json(): return c.get("/admin/done.json").get_json()
ok(c.post("/api/done", json={"class":"101","seat":"2"}, environ_overrides=st).status_code == 200, "學生按『我已經完成問卷』成功")
ok(c.post("/api/done", json={"class":"101","seat":"02"}, environ_overrides=st).status_code == 200, "重複送出(座號 02)不會出錯")
ok(c.post("/api/done", json={"class":"101","seat":"99"}, environ_overrides=st).status_code == 404, "不存在的座號不能回報")
ok(c.post("/api/done", json={"class":"999","seat":"1"}, environ_overrides=st).status_code == 403, "沒開放的班級不能回報")
d = done_json(); k = next(x for x in d["classes"] if x["class"] == "101")
ok(d["done_total"] == 1 and k["done"] == ["2"] and "2" not in k["pending"] and k["total"] == 5, f"老師端看到完成號碼(只有一筆,重複不累加): {k['done']}")
ok(c.post("/api/lookup", json={"class":"101","seat":"2"}, environ_overrides=st).get_json()["done"] is True, "再查詢時顯示已完成")
t = csrf(path="/admin")
ok(c.post("/admin/done", data={"csrf":t,"action":"unmark","class":"101","seat":"2"}).status_code == 200 and done_json()["done_total"] == 0, "老師可取消單一座號的完成標記")
c.post("/api/done", json={"class":"101","seat":"3"}, environ_overrides=st)
c.post("/api/done", json={"class":"102","seat":"1"}, environ_overrides=st)
ok(done_json()["done_total"] == 2, "兩個班各一人完成")
c.post("/admin/classes", data={"csrf":t, "only":"102"})
ok([x["class"] for x in done_json()["classes"]] == ["101","102"], "已關閉但有完成紀錄的班級仍顯示")
c.post("/admin/classes", data={"csrf":t, "action":"open_all"})
c.post("/admin/classes", data={"csrf":t, "action":"pause"})
ok(c.post("/api/done", json={"class":"101","seat":"4"}, environ_overrides=st).status_code == 503, "暫停後不可回報完成")
c.post("/admin/classes", data={"csrf":t, "action":"resume"})
ok(c.post("/admin/done", data={"action":"clear_all"}).status_code == 400, "清除完成紀錄缺 CSRF 被擋")
c.post("/admin/done", data={"csrf":t,"action":"clear_all"})
ok(done_json()["done_total"] == 0, "老師可清除全部完成紀錄")
c.post("/api/done", json={"class":"101","seat":"3"}, environ_overrides=st)   # 留一筆給後面的加密與重新啟動測試

# 4c 問卷連結
SURVEY = "https://forms.example.com/abc?x=1"
SURVEY2 = "http://survey.example.org/q2"
STORED = [{"name":"生活問卷","url":SURVEY}, {"name":"","url":SURVEY2}]          # 老師存的
SHOWN = [{"name":"生活問卷","url":SURVEY}, {"name":"問卷 2","url":SURVEY2}]     # 學生看到的(沒填名稱補「問卷 N」)
t = csrf(path="/admin")
def set_survey(rows): return c.post("/admin/survey", data={"csrf":t, "name":[r[0] for r in rows], "url":[r[1] for r in rows]}, follow_redirects=True).get_data(as_text=True)
ok(c.get("/api/status").get_json()["surveys"] == [], "還沒設定時學生看不到問卷連結")
for bad in ("javascript:alert(1)", "ftp://x.example.com/a", "forms.example.com/abc", "data:text/html,<b>x"):
    ok("格式不對" in set_survey([("問卷", bad)]) and A.store.surveys == [], f"拒絕不安全或格式錯誤的連結: {bad}")
ok("還沒有填連結" in set_survey([("生活問卷", "")]) and A.store.surveys == [], "只有名稱沒有連結的列會提示")
ok("格式不對" in set_survey([("好", SURVEY), ("壞", "ftp://bad.example.com")]) and A.store.surveys == [], "有一個連結壞掉就整批不儲存")
ok(c.post("/admin/survey", data={"name":"a", "url":SURVEY}).status_code == 400, "設定連結缺 CSRF 被擋")
page = set_survey([("生活問卷", SURVEY), ("", SURVEY2), ("重複", SURVEY), ("", "")])      # 含重複與空列
ok("已儲存 2 個問卷連結" in page and A.store.surveys == STORED, "一次放兩個問卷(重複連結與空列會被整理掉)")
ok(page.count('class="survey-edit-row"') == 3 and 'value="生活問卷"' in page and SURVEY.replace("&", "&amp;") in page, "管理頁每個問卷一列「名稱 + 連結」兩欄")
ok(c.get("/api/status", environ_overrides=st).get_json()["surveys"] == SHOWN, "學生頁面取得名稱與連結(沒填名稱補「問卷 2」)")
c.post("/admin/classes", data={"csrf":t, "action":"pause"})
ok(c.get("/api/status").get_json()["surveys"] == SHOWN, "暫停查詢時學生仍看得到問卷連結")
c.post("/admin/classes", data={"csrf":t, "action":"resume"})
ok(json.loads(A.CONFIG_FILE.read_text(encoding="utf-8"))["surveys"] == STORED, "問卷連結存檔")
ok("最多 10 個" in set_survey([("", f"https://x.example.com/{i}") for i in range(11)]) and A.store.surveys == STORED, "超過 10 個連結被拒絕")
page = set_survey([("<b>x</b>", 'https://a.example.com/"><script>x</script>')])
ok("<script>x" not in page and "<b>x</b>" not in page, "惡意名稱與連結在管理頁被跳脫")
set_survey([("生活問卷", SURVEY), ("", SURVEY2)])

# 5 IP 限制
ext = {"REMOTE_ADDR":"8.8.8.8"}
ok(c.get("/", environ_overrides=ext).status_code == 403, "外網 IP 連首頁被拒")
ok(c.post("/api/lookup", json={"class":"101","seat":"1"}, environ_overrides=ext).status_code == 403, "外網 IP 無法查詢")
net = str(A.ALLOWED_NETS[-1]).split("/")[0] if len(A.ALLOWED_NETS) > 2 else None
lan = {"REMOTE_ADDR":"192.168.1.50"}
A.ALLOWED_NETS.append(A.ipaddress.ip_network("192.168.1.0/24"))
ok(c.get("/", environ_overrides=lan).status_code == 200, "區網學生電腦可開首頁")
ok(c.get("/admin", environ_overrides=lan).status_code == 403, "區網學生電腦打不開管理頁")
ok(c.post("/admin/upload", environ_overrides=lan).status_code == 403, "區網學生電腦不能上傳")
ok(c.get("/admin/done.json", environ_overrides=lan).status_code == 403, "區網學生電腦看不到完成名單")
ok(c.post("/admin/done", data={"csrf":"x","action":"clear_all"}, environ_overrides=lan).status_code == 403, "區網學生電腦不能改完成紀錄")
ok(c.post("/admin/survey", data={"csrf":"x","name":"x","url":"https://evil.example"}, environ_overrides=lan).status_code == 403, "區網學生電腦不能改問卷連結")
ok(c.post("/admin/quit", data={"csrf":"x"}, environ_overrides=lan).status_code == 403, "區網學生電腦不能關閉程式")
ok(c.post("/admin/quit").status_code == 400, "關閉程式缺 CSRF 被擋")
ok(c.get("/api/status", environ_overrides=lan).get_json()["surveys"] == SHOWN, "區網學生電腦看得到問卷連結")
ok(c.post("/api/done", json={"class":"101","seat":"4"}, environ_overrides=lan).status_code == 200, "區網學生電腦可以回報完成")
c.post("/admin/done", data={"csrf":csrf(path="/admin"),"action":"unmark","class":"101","seat":"4"})
ok(c.get("/admin", headers={"Host":"evil.example"}).status_code == 403, "非 localhost 的 Host 標頭被拒 (DNS rebinding)")
ok(c.get("/admin", environ_overrides={"REMOTE_ADDR":"::ffff:192.168.1.50"}).status_code == 403, "IPv4-mapped 位址同樣處理")

# 6 速率限制
codes = [c.post("/api/lookup", json={"class":"101","seat":str(i%5+1)}, environ_overrides={"REMOTE_ADDR":"192.168.1.77"}).status_code for i in range(15)]
ok(429 in codes and codes[:12] == [200]*12, f"超過每分鐘上限被限流: {codes}")

# 7 資料加密與重新解鎖
raw = A.STUDENTS_FILE.read_bytes()
ok(b"11251" not in raw and b"21360060101" not in raw, "硬碟上的名單檔看不到明文帳密")
ok("11251" not in A.LOG_FILE.read_text(encoding="utf-8-sig"), "查詢紀錄不含密碼")
ok(A.DONE_FILE.exists() and b"101" not in A.DONE_FILE.read_bytes(), "完成紀錄也是加密存檔")
A.write_log("10.0.0.9", "=cmd|' /C calc'!A0", "@SUM(1)", "查無資料")
A.write_log("10.0.0.9", "+1", "-2", "查無資料")
rows = list(A.csv.reader(A.LOG_FILE.read_text(encoding="utf-8-sig").splitlines()))
bad = [r for r in rows[1:] if r[2][:1] in "=+-@" or r[3][:1] in "=+-@"]
ok(not bad and any(r[2].startswith("'=cmd") for r in rows), "查詢紀錄的 CSV 不會被當成 Excel 公式執行")
if A.os.name == "nt":
    ok(A.get_data_key() not in A.KEY_FILE.read_bytes(), "金鑰檔不是明文(受 Windows 帳號保護)")
A.store.__init__()                                   # 模擬關掉程式再重新開啟
A.ensure_store()
ok(A.store.unlocked and len(A.store.records) == 10, "重新啟動後不用密碼,名單從加密檔自動還原")
ok(A.store.open == set() and c.post("/api/lookup", json={"class":"101","seat":"1"}, environ_overrides=st).get_json()["password"] == "11251", "重新啟動後沒選班級,全部班級都能查")
ok(list(A.store.done) == [("101","3")], "重新啟動後完成紀錄從加密檔還原")
ok(A.store.surveys == STORED, "重新啟動後問卷連結(含名稱)還在")
A.CONFIG_FILE.write_text(json.dumps({"survey_urls": [SURVEY, SURVEY2]}), encoding="utf-8"); A.load_config()
ok(A.store.surveys == [{"name":"","url":SURVEY}, {"name":"","url":SURVEY2}], "舊版只有網址清單的設定檔也讀得到")
A.CONFIG_FILE.write_text(json.dumps({"survey_url": SURVEY}), encoding="utf-8"); A.load_config()
ok(A.store.surveys == [{"name":"","url":SURVEY}], "更舊版只有單一連結的設定檔也讀得到")

# 8 標頭
h = c.get("/").headers
ok("no-store" in h["Cache-Control"] and "default-src 'self'" in h["Content-Security-Policy"], "no-store 與 CSP 標頭存在")
# 9 範本與 XSS
ok(c.get("/admin/template.xlsx").status_code == 200, "範本下載")
evil = "<table><tr><td>班級</td><td>座號</td><td>密碼</td></tr><tr><td>&lt;script&gt;x&lt;/script&gt;</td><td>1</td><td>p</td></tr></table>".encode()
upload(evil, "e.xls"); body = c.get("/admin").get_data(as_text=True)
ok("<script>x" not in body, "名單內的惡意字串被跳脫")
ok(not A.store.done and not A.DONE_FILE.exists(), "上傳新名單後完成紀錄清空")
# 資料夾被複製到別台電腦/別的帳號:金鑰讀不出來 → 不當機,提示重新上傳
A.KEY_FILE.write_bytes(b"garbage"); A.store.__init__(); A.ensure_store()
ok(not A.store.records and A.store.load_failed, "金鑰讀不出來時名單為空並標記失敗")
ok("請重新上傳名單" in c.get("/admin").get_data(as_text=True), "管理頁提示重新上傳名單")
t = csrf(path="/admin"); c.post("/admin/wipe", data={"csrf":t})
ok(not A.STUDENTS_FILE.exists() and not A.DONE_FILE.exists() and not A.store.records, "刪除全部名單連同完成紀錄")
print("ALL OK")
