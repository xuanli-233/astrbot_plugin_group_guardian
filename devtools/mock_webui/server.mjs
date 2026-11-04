// 本地模拟环境：在 Pyodide 中运行真实插件代码，提供管理页面与模拟群聊。
//   npm install        （首次）
//   node server.mjs    然后打开 http://localhost:8765/
import http from "node:http";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { loadPyodide } from "pyodide";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const PLUGIN_DIR = path.resolve(HERE, "..", "..");
const PLUGIN_NAME = "astrbot_plugin_group_guardian";
const PORT = Number(process.env.PORT || 8765);

console.log("[mock] 正在启动 Pyodide ……");
const py = await loadPyodide();
await py.loadPackage(["sqlite3"]);
py.FS.mkdirTree(`/plugins/${PLUGIN_NAME}`);
py.FS.mount(py.FS.filesystems.NODEFS, { root: PLUGIN_DIR }, `/plugins/${PLUGIN_NAME}`);
py.FS.mkdirTree("/data");
py.runPython(fs.readFileSync(path.join(HERE, "host.py"), "utf8"));
console.log("[mock] 正在加载插件 ……");
const info = JSON.parse(py.globals.get("boot")("/plugins"));
console.log(`[mock] 插件已加载：${info.routes} 个 WebUI 接口，${info.commands} 条指令`);

const dispatch = py.globals.get("dispatch");
const sim = {
  state: py.globals.get("sim_state"),
  send: py.globals.get("sim_send"),
  member: py.globals.get("sim_member"),
  tick: py.globals.get("sim_tick"),
};

function readBody(req) {
  return new Promise((resolve) => {
    const chunks = [];
    req.on("data", (c) => chunks.push(c));
    req.on("end", () => resolve(Buffer.concat(chunks).toString("utf8")));
  });
}

function send(res, status, body, type = "application/json; charset=utf-8", headers = {}) {
  res.writeHead(status, { "Content-Type": type, "Cache-Control": "no-store", ...headers });
  res.end(body);
}

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, `http://localhost:${PORT}`);
  try {
    if (url.pathname === "/" || url.pathname === "/index.html") {
      return send(res, 200, fs.readFileSync(path.join(HERE, "index.html")), "text/html; charset=utf-8");
    }
    if (url.pathname === "/dashboard") {
      return send(res, 200, fs.readFileSync(path.join(PLUGIN_DIR, "pages", "dashboard", "index.html")),
        "text/html; charset=utf-8");
    }
    if (url.pathname === "/sim") {
      return send(res, 200, fs.readFileSync(path.join(HERE, "sim.html")), "text/html; charset=utf-8");
    }
    if (url.pathname === "/api/plugin/page/bridge-sdk.js") {
      return send(res, 200, fs.readFileSync(path.join(HERE, "bridge-sdk.js")), "application/javascript; charset=utf-8");
    }
    if (url.pathname.startsWith("/mock-api/")) {
      const apiPath = url.pathname.slice("/mock-api/".length);
      const query = Object.fromEntries(url.searchParams.entries());
      const body = req.method === "POST" ? await readBody(req) : "";
      const out = JSON.parse(dispatch(req.method, apiPath, JSON.stringify(query), body));
      const type = out.type === "json" ? "application/json; charset=utf-8" : (out.headers["Content-Type"] || "text/plain; charset=utf-8");
      return send(res, out.status, out.body, type, out.headers);
    }
    if (url.pathname.startsWith("/sim-api/")) {
      const action = url.pathname.slice("/sim-api/".length);
      const body = req.method === "POST" ? await readBody(req) : "";
      let out;
      if (action === "state") out = sim.state();
      else if (action === "send") out = sim.send(body);
      else if (action === "member") out = sim.member(body);
      else if (action === "tick") out = sim.tick(JSON.parse(body).when);
      else return send(res, 404, JSON.stringify({ ok: false, message: "unknown" }));
      return send(res, 200, out);
    }
    send(res, 404, "not found", "text/plain");
  } catch (e) {
    console.error(e);
    send(res, 500, JSON.stringify({ status: "error", message: String(e && e.message || e) }));
  }
});

server.listen(PORT, () => console.log(`[mock] 已就绪：http://localhost:${PORT}/`));
