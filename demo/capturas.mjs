// Capturas del README (móvil y escritorio) por CDP (Chrome headless con viewport de móvil de verdad).
// Uso, con `python -m demo.servidor` en marcha: node demo/capturas.mjs docs/capturas http://127.0.0.1:8765
import { spawn } from "node:child_process";
import { writeFileSync, mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const [, , salida, base] = process.argv;
const CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const chrome = spawn(CHROME, ["--headless=new", "--remote-debugging-port=9333", "--hide-scrollbars",
  `--user-data-dir=${mkdtempSync(join(tmpdir(), "cdp-"))}`, "about:blank"], { stdio: "ignore" });
const espera = ms => new Promise(r => setTimeout(r, ms));
await espera(1500);
const [tab] = (await (await fetch("http://127.0.0.1:9333/json")).json()).filter(t => t.type === "page");
const ws = new WebSocket(tab.webSocketDebuggerUrl);
await new Promise(r => ws.addEventListener("open", r));
let n = 0; const pend = {};
ws.addEventListener("message", e => { const m = JSON.parse(e.data); if (pend[m.id]) { pend[m.id](m.result); delete pend[m.id]; } });
const cdp = (method, params = {}) => new Promise(r => { const id = ++n; pend[id] = r; ws.send(JSON.stringify({ id, method, params })); });

await cdp("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 2, mobile: true });
await cdp("Emulation.setEmulatedMedia", { features: [{ name: "prefers-color-scheme", value: "light" }] });
for (const [vista, alto] of [["hoy", 844], ["semana", 844], ["compra", 844]]) {
  await cdp("Page.navigate", { url: "about:blank" }); await espera(300);
  await cdp("Page.navigate", { url: `${base}/#${vista}` });
  await espera(2500);
  const { data } = await cdp("Page.captureScreenshot", { format: "png", clip: { x: 0, y: 0, width: 390, height: alto, scale: 1 } });
  writeFileSync(join(salida, `movil-${vista === "semana" ? "plan" : vista}.png`), Buffer.from(data, "base64"));
}
await cdp("Emulation.setDeviceMetricsOverride", { width: 1400, height: 1000, deviceScaleFactor: 1.5, mobile: false });
await cdp("Page.navigate", { url: "about:blank" }); await espera(300);
await cdp("Page.navigate", { url: `${base}/` });
await espera(2500);
const { data } = await cdp("Page.captureScreenshot", { format: "png", clip: { x: 0, y: 0, width: 1400, height: 1000, scale: 1 } });
writeFileSync(join(salida, "escritorio.png"), Buffer.from(data, "base64"));
ws.close(); chrome.kill();
