(function () {
  "use strict";
  Array.prototype.forEach.call(document.querySelectorAll("form[data-confirm]"), function (f) {
    f.addEventListener("submit", function (ev) {
      if (!window.confirm(f.getAttribute("data-confirm"))) ev.preventDefault();
    });
  });

  // ── 問卷連結:每列「名稱 + 連結」,可新增、刪除 ──
  var rows = document.getElementById("survey-rows");
  if (rows) {
    document.getElementById("survey-add").addEventListener("click", function () {
      var tpl = document.getElementById("survey-row-tpl");
      var row = tpl.content.firstElementChild.cloneNode(true);
      rows.appendChild(row);
      row.querySelector("input").focus();
    });
    rows.addEventListener("click", function (ev) {
      if (!ev.target.classList.contains("row-del")) return;
      var row = ev.target.closest(".survey-edit-row");
      if (rows.children.length > 1) {
        row.remove();
      } else {                                           // 最後一列:清空就好,保留一列可填
        Array.prototype.forEach.call(row.querySelectorAll("input"), function (i) { i.value = ""; });
      }
    });
  }

  // ── 問卷完成狀況:定時向主機要資料,用 textContent 畫出來(不使用 innerHTML) ──
  var panel = document.getElementById("progress");
  if (!panel) return;
  var body = document.getElementById("progress-body");
  var sum = document.getElementById("progress-sum");
  var clearBtn = document.getElementById("progress-clear");
  var csrf = panel.getAttribute("data-csrf");

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  }

  function post(fields) {
    var fd = new FormData();
    fd.append("csrf", csrf);
    Object.keys(fields).forEach(function (k) { fd.append(k, fields[k]); });
    return fetch("/admin/done", { method: "POST", body: fd, cache: "no-store" }).then(refresh);
  }

  function render(d) {
    sum.textContent = "已完成 " + d.done_total + " / " + d.total + " 人";
    sum.classList.remove("hidden");
    clearBtn.classList.toggle("hidden", d.done_total === 0);
    body.textContent = "";
    if (!d.classes.length) {
      body.appendChild(el("p", "muted", "目前沒有開放的班級。"));
      return;
    }
    d.classes.forEach(function (c) {
      var box = el("div", "prog-class");
      var head = el("div", "prog-head");
      head.appendChild(el("strong", "", c["class"] + " 班" + (c.open ? "" : "(已關閉)")));
      head.appendChild(el("span", "prog-count", c.done.length + " / " + c.total));
      box.appendChild(head);

      var bar = el("div", "bar");
      var fill = el("div", "bar-fill");
      fill.style.width = (c.total ? Math.round(100 * c.done.length / c.total) : 0) + "%";
      bar.appendChild(fill);
      box.appendChild(bar);

      var chips = el("div", "seat-chips");
      if (!c.done.length) chips.appendChild(el("span", "muted", "還沒有人回報完成"));
      c.done.forEach(function (s) {
        var b = el("button", "seat done", s);
        b.type = "button";
        b.title = "按一下取消 " + s + " 號的完成標記";
        b.addEventListener("click", function () {
          if (window.confirm("取消 " + c["class"] + " 班 " + s + " 號的完成標記?"))
            post({ action: "unmark", "class": c["class"], seat: s });
        });
        chips.appendChild(b);
      });
      box.appendChild(chips);

      if (c.pending.length)
        box.appendChild(el("p", "muted pending", "還沒完成:" + c.pending.join("、") + " 號"));
      else if (c.total)
        box.appendChild(el("p", "muted pending", "全班都完成了 ✓"));
      body.appendChild(box);
    });
  }

  function refresh() {
    return fetch("/admin/done.json", { cache: "no-store" })
      .then(function (r) {
        if (r.status === 403) throw new Error("locked");
        return r.json();
      })
      .then(render)
      .catch(function (e) {
        sum.classList.add("hidden");
        body.textContent = "";
        body.appendChild(el("p", "muted",
          e.message === "locked" ? "已鎖定或登入逾時,請重新整理頁面後登入。" : "暫時連不上,稍後自動重試。"));
      });
  }

  clearBtn.addEventListener("click", function () {
    if (window.confirm("確定要清除全部的完成紀錄嗎?")) post({ action: "clear_all" });
  });

  refresh();
  setInterval(function () { if (!document.hidden) refresh(); }, 5000);
})();
