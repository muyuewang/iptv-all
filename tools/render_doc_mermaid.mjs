/*
 * render_doc_mermaid.mjs —— 把含 Mermaid 的 Markdown 渲染成「离线自包含 HTML」
 * ============================================================================
 * 用途：`架构与流程图-两源两端.md` 这类带图文档，在没有 Mermaid 渲染器的环境
 *       （记事本 / 部分 IDE 预览）里看不出图。本脚本用本机 Edge（或 Chrome）
 *       把每张 Mermaid 图渲染成内嵌 SVG，再把整篇 Markdown 转成 HTML，
 *       产出一个**零外部依赖**的 .html —— 双击即可看图。
 *
 * 依赖（装在 workbuddy 的 node 工作区，不污染项目）：
 *   cd ~/.workbuddy/binaries/node/workspace && npm i mermaid puppeteer-core marked
 *   · mermaid        —— 提供 dist/mermaid.min.js（页面里渲染图）
 *   · puppeteer-core —— 驱动本机已装的 Edge/Chrome（不下载浏览器）
 *   · marked         —— Markdown → HTML
 *
 * 用法：
 *   node tools/render_doc_mermaid.mjs <输入.md> <输出.html>
 *   模块目录可用环境变量 WB_NODE_MODULES 覆盖（默认自动探测 node 工作区）。
 *
 * 说明：本脚本会用一个临时 HTML 把 mermaid.min.js 载进无头浏览器，
 *       逐块 mermaid.render() 取回 SVG，再替换回正文。任何一块渲染失败
 *       会退化成红色提示（不会让整篇产物报废）。
 * ============================================================================
 */
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

const SRC = process.argv[2];
const OUT = process.argv[3];
if (!SRC || !OUT) { console.error('用法: node render_doc_mermaid.mjs <in.md> <out.html>'); process.exit(1); }

/* ---------- 1) 定位依赖模块目录（ESM 按引入者目录解析，脚本在仓库里找不到工作区） ---------- */
function findModulesDir() {
  const cands = [
    process.env.WB_NODE_MODULES,
    path.join(os.homedir(), '.workbuddy', 'binaries', 'node', 'workspace', 'node_modules'),
    path.resolve('node_modules'),
  ].filter(Boolean);
  for (const c of cands) {
    try {
      if (fs.existsSync(path.join(c, 'mermaid', 'package.json'))
        && fs.existsSync(path.join(c, 'puppeteer-core', 'package.json'))
        && fs.existsSync(path.join(c, 'marked', 'package.json'))) return c;
    } catch (e) {}
  }
  console.error('未找到 node_modules（mermaid / puppeteer-core / marked）。');
  console.error('请先执行：cd ~/.workbuddy/binaries/node/workspace && npm i mermaid puppeteer-core marked');
  console.error('或用 WB_NODE_MODULES=<node_modules 绝对路径> 指定。');
  process.exit(2);
}
const MODS = findModulesDir();

/** 读包 package.json 的 exports['.'] / module / main，拿到 ESM 入口的绝对路径 */
function entryOf(pkg) {
  const j = JSON.parse(fs.readFileSync(path.join(MODS, pkg, 'package.json'), 'utf8'));
  const ex = j.exports && j.exports['.'];
  let rel = (ex && (ex.import || ex.default)) || j.module || j.main;
  if (typeof rel !== 'string') rel = j.module || j.main;
  return path.join(MODS, pkg, rel);
}
const puppeteer = (await import(pathToFileURL(entryOf('puppeteer-core')).href)).default;
const { marked } = await import(pathToFileURL(entryOf('marked')).href);

/* ---------- 2) 找浏览器 ---------- */
function findBrowser() {
  const cands = [
    'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
    'C:/Program Files/Microsoft/Edge/Application/msedge.exe',
    'C:/Program Files/Google/Chrome/Application/chrome.exe',
    'C:/Program Files (x86)/Google/Chrome/Application/chrome.exe',
    '/usr/bin/microsoft-edge', '/usr/bin/google-chrome', '/usr/bin/chromium',
  ];
  for (const c of cands) { try { if (fs.existsSync(c)) return c; } catch (e) {} }
  return null;
}
const EDGE = findBrowser();
if (!EDGE) { console.error('未找到 Edge/Chrome —— 无法渲染'); process.exit(2); }

const MERMAID = path.join(MODS, 'mermaid', 'dist', 'mermaid.min.js');
if (!fs.existsSync(MERMAID)) { console.error('缺少 mermaid/dist/mermaid.min.js'); process.exit(2); }

/* ---------- 3) 渲染每张 Mermaid 图 ---------- */
const md = fs.readFileSync(SRC, 'utf8');
const blocks = [...md.matchAll(/```mermaid\n([\s\S]*?)```/g)].map(m => m[1].trim());
console.log('mermaid 块:', blocks.length);

const tmp = path.resolve(process.cwd(), '_render_tmp.html');
fs.writeFileSync(tmp, `<!DOCTYPE html><html><head><meta charset="utf-8"></head><body>
<script src="${pathToFileURL(MERMAID).href}"></script>
<script>window.__ready=false;try{mermaid.initialize({startOnLoad:false,theme:'dark',
 securityLevel:'loose',fontFamily:'"Segoe UI","Microsoft YaHei",sans-serif'});window.__ready=true;}
 catch(e){window.__err=String(e);}</script></body></html>`, 'utf8');

const browser = await puppeteer.launch({
  executablePath: EDGE, headless: 'new',
  args: ['--no-sandbox', '--disable-gpu', '--allow-file-access-from-files'],
});
const page = await browser.newPage();
await page.setViewport({ width: 1280, height: 900, deviceScaleFactor: 1 });
await page.goto(pathToFileURL(tmp).href, { waitUntil: 'load' });
if (!(await page.evaluate(() => window.__ready === true))) {
  console.error('mermaid 初始化失败', await page.evaluate(() => window.__err));
  process.exit(2);
}

const svgs = [];
for (let i = 0; i < blocks.length; i++) {
  try {
    svgs.push(await page.evaluate(async (def, id) => (await mermaid.render('mg' + id, def)).svg,
              blocks[i], i));
    console.log(`  渲染 OK  #${i + 1}`);
  } catch (e) {
    console.error(`  渲染 FAIL #${i + 1}: ${String(e).split('\n')[0]}`);
    svgs.push(null);
  }
}
await browser.close();
try { fs.unlinkSync(tmp); } catch (e) {}

/* ---------- 4) Markdown → HTML，把 SVG 填回占位 ---------- */
let k = 0;
const withPH = md.replace(/```mermaid\n[\s\S]*?```/g, () => `\n<div class="mmd">@@MMD${k++}@@</div>\n`);
let body = marked.parse(withPH, { mangle: false });
svgs.forEach((svg, i) => {
  body = body.replace(`@@MMD${i}@@`, svg || '<pre class="raw">渲染失败，见 Markdown 源</pre>');
});

fs.writeFileSync(OUT, `<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>纯净电视 · 两源两端架构与流程图</title>
<style>
  :root{ --bg:#12151c;--panel:#181c25;--panel2:#1e2330;--fg:#e6e9ef;--dim:#a7b0c0;
         --line:#2b3242;--brand:#e8543f;--brand2:#f0913e;--code:#1b2130; }
  *{ box-sizing:border-box; }
  body{ margin:0;background:var(--bg);color:var(--fg);
        font:15px/1.75 "Segoe UI","Microsoft YaHei",system-ui,sans-serif;padding:32px 20px 80px; }
  .wrap{ max-width:1080px;margin:0 auto; }
  h1{ font-size:28px;margin:0 0 6px;background:linear-gradient(90deg,var(--brand),var(--brand2));
      -webkit-background-clip:text;background-clip:text;color:transparent; }
  h2{ font-size:21px;margin:38px 0 12px;padding-bottom:8px;border-bottom:1px solid var(--line); }
  h3{ font-size:17px;margin:26px 0 10px;color:#fff; }
  h4{ font-size:15px;margin:20px 0 8px;color:var(--brand2); }
  a{ color:var(--brand2); }
  blockquote{ margin:12px 0;padding:10px 16px;background:var(--panel2);
              border-left:3px solid var(--brand);border-radius:0 8px 8px 0;color:var(--dim); }
  code{ background:var(--code);padding:2px 6px;border-radius:5px;font-size:13px;
        font-family:"Cascadia Mono",Consolas,monospace;color:#ffd9a0; }
  pre{ background:var(--code);padding:14px 16px;border-radius:10px;overflow:auto;border:1px solid var(--line); }
  pre code{ background:none;padding:0;color:#cfe3ff; }
  table{ border-collapse:collapse;width:100%;margin:14px 0;font-size:13.5px; }
  th,td{ border:1px solid var(--line);padding:8px 11px;text-align:left;vertical-align:top; }
  th{ background:var(--panel2);color:#fff; }
  tr:nth-child(even) td{ background:#161a22; }
  hr{ border:none;border-top:1px solid var(--line);margin:34px 0; }
  .mmd{ background:var(--panel);border:1px solid var(--line);border-radius:12px;
        padding:18px;margin:18px 0;overflow-x:auto;text-align:center; }
  .mmd svg{ max-width:100%;height:auto; }
  .raw{ color:#ff9a9a; }
</style></head>
<body><div class="wrap">
${body}
</div></body></html>`, 'utf8');
console.log('已写出:', OUT, '(' + (fs.statSync(OUT).size / 1024).toFixed(0) + ' KB)');
