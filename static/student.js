(function () {
  "use strict";
  var $ = function (id) { return document.getElementById(id); };
  var form = $("lookup-form"), clsSel = $("class"), seatInput = $("seat");
  var banner = $("banner"), errorBox = $("error"), resultBox = $("result");
  var submitBtn = $("submit");

  function showBanner(msg) {
    banner.textContent = msg;
    banner.classList.toggle("hidden", !msg);
  }
  function showError(msg) {
    errorBox.textContent = msg;
    errorBox.classList.toggle("hidden", !msg);
  }
  var current = null;   // 目前畫面上這位的 {cls, seat}
  var doneBtn = $("done-btn"), doneOk = $("done-ok"), doneErr = $("done-error");

  function showDone(isDone) {
    doneBtn.classList.toggle("hidden", isDone);
    doneOk.classList.toggle("hidden", !isDone);
    if (isDone && current) $("done-seat").textContent = current.seat;
  }
  // 座號:可以直接輸入,也可以從下拉清單選;清單與提示都會帶出姓名,方便對照是不是自己
  var seatMap = {}, seatsFor = null;
  // 輸入框的內容可能是「11」、從清單選的「11 小明」,或直接打姓名「小明」→ 都換算成座號
  function seatFrom(raw) {
    var v = raw.trim(), m = /^[0-9]+/.exec(v);
    if (m) return String(parseInt(m[0], 10));
    var hit = Object.keys(seatMap).filter(function (s) { return seatMap[s] === v; });
    return hit.length === 1 ? hit[0] : v;
  }
  function updateSeatHint() {
    var name = seatMap[seatFrom(seatInput.value)];
    $("seat-name").textContent = name ? "→ " + name : "";
  }
  function loadSeats(cls) {
    seatsFor = cls;
    seatMap = {};
    $("seat-list").textContent = "";
    updateSeatHint();
    if (!cls) return Promise.resolve();
    return fetch("/api/seats?class=" + encodeURIComponent(cls), { cache: "no-store" })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (clsSel.value !== cls) return;                       // 期間換了班,這份作廢
        var dl = $("seat-list");
        (d.seats || []).forEach(function (x) {
          seatMap[x.seat] = x.name;
          var o = document.createElement("option");
          o.value = x.name ? x.seat + " " + x.name : x.seat;   // 座號與姓名放同一行
          dl.appendChild(o);
        });
        updateSeatHint();
      })
      .catch(function () { seatsFor = null; });
  }

  function clearResult() {
    $("v-account").textContent = "";
    $("v-password").textContent = "";
    $("who").textContent = "";
    resultBox.classList.add("hidden");
    current = null;
    showDone(false);
    doneErr.classList.add("hidden");
    doneBtn.disabled = false;
  }

  // 問卷連結:每個一列「網址 + 前往」,可以有多個
  var surveyKey = "";
  function renderSurveys(items) {
    items = items.filter(function (s) { return s && /^https?:\/\//i.test(s.url); });
    var key = JSON.stringify(items);
    if (key === surveyKey) return;
    surveyKey = key;
    var box = $("survey-list");
    box.textContent = "";
    items.forEach(function (s) {
      var row = document.createElement("div");
      row.className = "survey-row";
      var info = document.createElement("div");
      info.className = "survey-info";
      var name = document.createElement("div");
      name.className = "survey-name";
      name.textContent = s.name;
      var url = document.createElement("div");
      url.className = "survey-url";
      url.textContent = s.url;
      info.appendChild(name);
      info.appendChild(url);
      var go = document.createElement("a");
      go.className = "btn primary";
      go.textContent = "前往";
      go.href = s.url;
      go.target = "_blank";
      go.rel = "noopener noreferrer";
      row.appendChild(info);
      row.appendChild(go);
      box.appendChild(row);
    });
    $("survey").classList.toggle("hidden", items.length === 0);
  }

  function loadStatus() {
    return fetch("/api/status", { cache: "no-store" })
      .then(function (r) { return r.json(); })
      .then(function (s) {
        var list = s.classes || [];
        var labels = s.labels || {};                    // 505 → 五年五班
        var previous = clsSel.value;
        var shown = Array.prototype.slice.call(clsSel.options, 1).map(function (o) { return o.value; });
        if (shown.join("|") !== list.join("|")) {      // 班級有變才重畫,避免選到一半被打斷
          while (clsSel.options.length > 1) clsSel.remove(1);
          list.forEach(function (c) {
            var o = document.createElement("option");
            o.value = c; o.textContent = labels[c] ? c + "　" + labels[c] : c;
            clsSel.appendChild(o);
          });
          clsSel.value = list.indexOf(previous) >= 0 ? previous : "";
        }
        // 老師只開放一個班:直接顯示「這堂課:101 班」,學生只要輸入座號
        var single = list.length === 1;
        if (single) clsSel.value = list[0];
        $("class-wrap").classList.toggle("hidden", single);
        $("row").classList.toggle("single", single);
        $("class-fixed").textContent = single ? "這堂課:" + (labels[list[0]] || list[0] + " 班") : "";
        $("class-fixed").classList.toggle("hidden", !single);
        if (clsSel.value !== seatsFor) loadSeats(clsSel.value);
        showBanner(s.ok ? "" : (s.message || "目前無法查詢。"));
        renderSurveys(s.surveys || []);
        submitBtn.disabled = !s.ok;
      })
      .catch(function () {
        showBanner("連不上查詢主機,請告訴老師。");
        submitBtn.disabled = true;
      });
  }

  // 複製:HTTP 區網網址不是「安全環境」,clipboard API 不可用,所以要有備援做法。
  function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(text).then(function () { return true; })
        .catch(function () { return fallbackCopy(text); });
    }
    return Promise.resolve(fallbackCopy(text));
  }
  function fallbackCopy(text) {
    var ta = document.createElement("textarea");
    ta.value = text;
    ta.className = "sr-copy";
    ta.setAttribute("readonly", "");
    document.body.appendChild(ta);
    ta.select();
    ta.setSelectionRange(0, text.length);
    var ok = false;
    try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
    document.body.removeChild(ta);
    return ok;
  }

  Array.prototype.forEach.call(document.querySelectorAll(".copy-btn"), function (btn) {
    btn.addEventListener("click", function () {
      var text = $(btn.getAttribute("data-target")).textContent;
      if (!text) return;
      copyText(text).then(function (ok) {
        var old = btn.getAttribute("data-label");
        btn.textContent = ok ? "已複製 ✓" : "請手動選取";
        btn.classList.toggle("done", ok);
        setTimeout(function () { btn.textContent = old; btn.classList.remove("done"); }, 1500);
      });
    });
  });

  form.addEventListener("submit", function (ev) {
    ev.preventDefault();
    showError("");
    clearResult();
    var cls = clsSel.value, seat = seatFrom(seatInput.value);
    if (!cls) { showError("請先選擇班級。"); return; }
    if (!/^[0-9]{1,3}$/.test(seat)) { showError("請輸入座號(數字),或從清單選。"); return; }
    submitBtn.disabled = true;
    fetch("/api/lookup", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      cache: "no-store",
      body: JSON.stringify({ "class": cls, "seat": seat })
    })
      .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
      .then(function (res) {
        if (!res.ok) { showError(res.d.error || "查詢失敗。"); return; }
        $("v-account").textContent = res.d.account;
        $("v-password").textContent = res.d.password;
        current = { cls: cls, seat: String(parseInt(seat, 10)) };
        var who = $("who");
        who.textContent = "座號 " + current.seat + " 號";
        if (res.d.name) {
          var nm = document.createElement("span");
          nm.textContent = "　" + res.d.name;
          who.appendChild(nm);
        }
        showDone(!!res.d.done);
        resultBox.classList.remove("hidden");
      })
      .catch(function () { showError("連線失敗,請再試一次。"); })
      .then(function () { submitBtn.disabled = false; });
  });

  // 自我檢查:按下「我已經完成問卷」,老師端就會看到這個座號
  doneBtn.addEventListener("click", function () {
    if (!current) return;
    if (!window.confirm("確定你已經完成問卷了嗎?\n按下去之後,老師會看到你的號碼。")) return;
    doneBtn.disabled = true;
    doneErr.classList.add("hidden");
    fetch("/api/done", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      cache: "no-store",
      body: JSON.stringify({ "class": current.cls, "seat": current.seat })
    })
      .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
      .then(function (res) {
        if (!res.ok) {
          doneErr.textContent = res.d.error || "送出失敗,請再按一次。";
          doneErr.classList.remove("hidden");
          doneBtn.disabled = false;
          return;
        }
        showDone(true);
      })
      .catch(function () {
        doneErr.textContent = "連線失敗,請再按一次。";
        doneErr.classList.remove("hidden");
        doneBtn.disabled = false;
      });
  });

  // 改了班級或座號,就把上一位的結果從畫面上清掉
  [clsSel, seatInput].forEach(function (el) {
    el.addEventListener("input", function () { clearResult(); showError(""); });
  });
  clsSel.addEventListener("change", function () { seatInput.value = ""; loadSeats(clsSel.value); });
  seatInput.addEventListener("input", updateSeatHint);

  window.addEventListener("pageshow", clearResult);
  loadStatus();
  window.addEventListener("focus", loadStatus);
  setInterval(function () { if (!document.hidden) loadStatus(); }, 10000);   // 老師換班時自動更新
})();
