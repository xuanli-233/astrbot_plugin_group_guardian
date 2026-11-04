// 模拟 AstrBot 的插件页面 Bridge SDK：把 apiGet / apiPost / download 转到本地模拟服务。
(function () {
  function qs(params) {
    if (!params) return "";
    var parts = [];
    Object.keys(params).forEach(function (k) {
      var v = params[k];
      if (v === undefined || v === null) return;
      parts.push(encodeURIComponent(k) + "=" + encodeURIComponent(String(v)));
    });
    return parts.length ? "?" + parts.join("&") : "";
  }
  async function parse(resp) {
    var text = await resp.text();
    try { return JSON.parse(text); } catch (e) { return text; }
  }
  window.AstrBotPluginPage = {
    ready: async function () { return true; },
    apiGet: async function (path, params) {
      return parse(await fetch("/mock-api/" + String(path).replace(/^\//, "") + qs(params)));
    },
    apiPost: async function (path, body) {
      return parse(await fetch("/mock-api/" + String(path).replace(/^\//, ""), {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}),
      }));
    },
    download: async function (path, params, filename) {
      var resp = await fetch("/mock-api/" + String(path).replace(/^\//, "") + qs(params));
      if (!resp.ok) throw new Error("HTTP " + resp.status);
      var blob = await resp.blob();
      var a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = filename || "download";
      a.click();
      setTimeout(function () { URL.revokeObjectURL(a.href); }, 1000);
    },
  };
})();
