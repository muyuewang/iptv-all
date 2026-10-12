/* AES / 解密链 交叉验证：用真实的 .dat 文件在 Node 里跑 wvt.js 的解密逻辑，
   与 Python 侧结果逐字节对比。 */
const fs = require("fs");
const path = require("path");
const zlib = require("zlib");

global.window = global;
global.localStorage = { getItem: () => null, setItem: () => {} };
// 用 node 的 zlib 冒充浏览器的 DecompressionStream（只用于测试）
global.Response = function (readable) {
  this.arrayBuffer = () => new Promise((res, rej) => {
    const chunks = [];
    readable._r.on("data", (c) => chunks.push(c));
    readable._r.on("end", () => res(Buffer.concat(chunks).buffer));
    readable._r.on("error", rej);
  });
};
global.DecompressionStream = function () {
  const { Readable } = require("stream");
  const r = new Readable({ read() {} });
  const chunks = [];
  this.readable = { _r: r };
  this.writable = {
    getWriter: () => ({
      write: (c) => { chunks.push(Buffer.from(c)); },
      close: () => {
        try { r.push(zlib.gunzipSync(Buffer.concat(chunks))); }
        catch (e) { r.destroy(e); }
        r.push(null);
      },
      releaseLock: () => {},
    }),
  };
};

global.WVT_SNAPSHOT = { channels: [], adapters: [] };
eval(fs.readFileSync(path.join(__dirname, "..", "shared", "web", "wvt.js"), "utf8"));

// 样本目录：先跑一次下面的 Python 片段把真实 .dat + 期望明文放进去
//   python tools/gen_wvt_aes_fixture.py
// 或自定义：WVT_AES_DIR=/path/to/dir node tools/check_wvt_aes.js
const DIR = process.env.WVT_AES_DIR || path.join(__dirname, "..", "_aes_test");
const files = [
  ["full.dat", "channels"],
  ["webpage_adapter.dat", "adapters"],
  ["server_config.dat", "config"],
];

(async () => {
  let fails = 0;
  for (const [f, kind] of files) {
    const buf = fs.readFileSync(path.join(DIR, f));
    const ab = buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength);
    const expect = JSON.parse(fs.readFileSync(path.join(DIR, f + ".expect.json"), "utf8"));
    let got;
    try {
      got = await WVTCore.decryptDatJson(ab);
    } catch (e) {
      console.log(`[!!] ${f} 解密失败: ${e.message}`);
      fails++;
      continue;
    }
    const a = JSON.stringify(got), b = JSON.stringify(expect);
    if (a === b) {
      const n = Array.isArray(got) ? got.length : Object.keys(got).length;
      console.log(`[OK] ${f}  ${kind}  条数=${n}  与 Python 输出完全一致`);
    } else {
      fails++;
      console.log(`[!!] ${f} 与 Python 输出不一致`);
      console.log("     js : " + a.slice(0, 200));
      console.log("     py : " + b.slice(0, 200));
    }
  }
  console.log(fails ? `\n失败 ${fails} 项` : "\n全部一致 ✅");
  process.exit(fails ? 2 : 0);
})();
