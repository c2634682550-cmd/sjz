// 纯本地脚本：复制按钮、趋势条高度、危险操作确认。无外部依赖。
(function () {
  function copy(text, btn) {
    var done = function () {
      var old = btn.textContent;
      btn.textContent = "已复制";
      setTimeout(function () { btn.textContent = old; }, 1200);
    };
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(done, fallback);
    } else { fallback(); }
    function fallback() {
      var ta = document.createElement("textarea");
      ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0";
      document.body.appendChild(ta); ta.select();
      try { document.execCommand("copy"); done(); } catch (e) {}
      document.body.removeChild(ta);
    }
  }

  document.addEventListener("click", function (ev) {
    var btn = ev.target.closest("[data-copy],[data-copy-target]");
    if (!btn) { return; }
    ev.preventDefault();
    var text = btn.getAttribute("data-copy");
    var sel = btn.getAttribute("data-copy-target");
    if (sel) {
      var el = document.querySelector(sel);
      text = el ? (el.value !== undefined ? el.value : el.textContent) : "";
    }
    copy(text || "", btn);
  });

  document.querySelectorAll(".trend .bar").forEach(function (bar) {
    var pct = parseInt(bar.getAttribute("data-pct") || "0", 10);
    bar.style.height = Math.max(4, Math.min(100, pct)) + "%";
  });

  document.querySelectorAll("form[data-confirm]").forEach(function (form) {
    form.addEventListener("submit", function (ev) {
      if (!window.confirm(form.getAttribute("data-confirm"))) { ev.preventDefault(); }
    });
  });
})();
