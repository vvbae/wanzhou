// 全站统一页眉 + 页脚（每个页面引入即可，无需手抄）
(function () {
  const sub = "#6b6b66", line = "#e3e3df";
  // 页眉：万轴(回首页) + 账号
  const header = document.createElement("div");
  header.style.cssText = "display:flex;justify-content:space-between;align-items:center;" +
    "padding-bottom:10px;margin-bottom:14px;border-bottom:1px solid " + line + ";";
  header.innerHTML =
    '<a href="/" style="font-weight:700;font-size:18px;color:#1c1c1a;text-decoration:none">万轴</a>' +
    '<span id="__acct" style="font-size:13px"></span>';
  document.body.insertBefore(header, document.body.firstChild);

  // 页脚
  const footer = document.createElement("div");
  footer.style.cssText = "margin-top:30px;padding-top:14px;border-top:1px solid " + line +
    ";color:" + sub + ";font-size:12px;text-align:center;";
  footer.innerHTML =
    '<a href="/about" style="color:' + sub + '">关于</a> · ' +
    '<a href="/guide" style="color:' + sub + '">如何参与</a> · ' +
    '<a href="/contact" style="color:' + sub + '">联系</a> · ' +
    '<a href="/privacy" style="color:' + sub + '">隐私</a><br>万轴 · 中文开放图书馆 · 数据 CC0';
  document.body.appendChild(footer);

  // 账号区
  fetch("/auth/me").then(r => r.json()).then(me => {
    const a = document.getElementById("__acct"); if (!a) return;
    if (me.username) {
      const review = (me.role === "reviewer" || me.role === "admin")
        ? ' · <a href="/admin" style="color:#2c5b8f">审核</a>' : "";
      a.innerHTML = me.username + review + ' · <a href="#" id="__logout" style="color:' + sub + '">退出</a>';
      document.getElementById("__logout").onclick = async e => {
        e.preventDefault(); await fetch("/auth/logout", { method: "POST" }); location.reload();
      };
    } else {
      a.innerHTML = '<a href="/login" style="color:#2c5b8f">登录 / 注册</a>';
    }
  }).catch(() => {});
})();
