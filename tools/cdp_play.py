# -*- coding: utf-8 -*-
"""真机播放实测：打开 CCTV1，等自动测速选路后播放，采样 20 秒统计卡顿与解码表现。"""
import json, time, urllib.request
from websocket import create_connection

def target():
    j = json.load(urllib.request.urlopen("http://127.0.0.1:9222/json", timeout=5))
    for t in j:
        if t.get("type") == "page":
            return t["webSocketDebuggerUrl"]
    raise SystemExit("no page target")

class CDP:
    def __init__(self, url):
        self.ws = create_connection(url, timeout=40, suppress_origin=True)
        self.i = 0
    def eval(self, expr, await_promise=False):
        self.i += 1
        self.ws.send(json.dumps({"id": self.i, "method": "Runtime.evaluate",
            "params": {"expression": expr, "returnByValue": True, "awaitPromise": await_promise}}))
        while True:
            m = json.loads(self.ws.recv())
            if m.get("id") == self.i:
                r = m.get("result", {})
                if "exceptionDetails" in r:
                    return "ERR: " + str(r["exceptionDetails"].get("text"))
                return r.get("result", {}).get("value")
    def close(self): self.ws.close()

# 采样脚本：记录 20 秒内的 waiting/stalled 次数与缓冲
SAMPLER = r"""
(function(){
  var v=document.getElementById('v');
  if(!v) return 'no video el';
  window.__s = {t0:Date.now(), waiting:0, playing:0, stall:0, samples:[]};
  v.addEventListener('waiting', function(){ window.__s.waiting++; });
  v.addEventListener('playing', function(){ window.__s.playing++; });
  v.addEventListener('stalled', function(){ window.__s.stall++; });
  window.__si = setInterval(function(){
    try{
      var b = v.buffered.length ? (v.buffered.end(v.buffered.length-1) - v.currentTime) : -1;
      window.__s.samples.push({
        t: ((Date.now()-window.__s.t0)/1000).toFixed(1),
        ct: +v.currentTime.toFixed(2),
        buf: +b.toFixed(2),
        rs: v.readyState,
        paused: v.paused
      });
    }catch(e){}
  }, 1000);
  return 'sampler started';
})()
"""

RESULT = r"""
(function(){
  clearInterval(window.__si);
  var s = window.__s || {};
  var v = document.getElementById('v');
  var buf = v && v.buffered.length ? (v.buffered.end(v.buffered.length-1)-v.currentTime) : null;
  return JSON.stringify({
    cur_title: (document.getElementById('cname')||{}).textContent,
    cur_epg:   (window.cur||{}).epg ? window.cur.epg.length : null,
    lines:     ((window.cur||{}).urls||[]).length,
    sel_line:  (window.cur||{}).idx,
    cfg_enableWorker: (typeof mpegtsCfg==='function') ? mpegtsCfg().enableWorker : null,
    tier: window.TIER,
    v_w: v?v.videoWidth:null, v_h: v?v.videoHeight:null,
    v_rs: v?v.readyState:null, v_paused: v?v.paused:null,
    v_ct: v?+v.currentTime.toFixed(2):null,
    buf_latency: buf,
    waiting_cnt: s.waiting, playing_cnt: s.playing, stalled_cnt: s.stall,
    samples: (s.samples||[]).filter(function(_,i){return i%3===0;})
  });
})()
"""

if __name__ == "__main__":
    c = CDP(target())
    print("== 1) 点开 CCTV1 ==")
    print(" ", c.eval("""(function(){
        var ch=document.querySelector('.ch'); if(!ch) return 'no card';
        ch.click(); return 'clicked: '+ch.innerText.split('\\n')[0];
    })()"""))
    # 等自动测速(8条*最多2.5s) + 起播
    time.sleep(6)
    print("== 2) 起播状态 ==")
    print(" ", c.eval("""(function(){
        var v=document.getElementById('v');
        return JSON.stringify({paused:v.paused, rs:v.readyState, ct:+v.currentTime.toFixed(2),
            w:v.videoWidth, h:v.videoHeight, err:v.error?v.error.code:null,
            status:(document.getElementById('status')||{}).textContent});
    })()"""))
    print("== 3) 开始采样 ==")
    print(" ", c.eval(SAMPLER))
    for k in range(20):
        time.sleep(1)
        s = c.eval("""(function(){var v=document.getElementById('v');
            return v.currentTime.toFixed(2)+'s rs='+v.readyState+' buf='+
            (v.buffered.length?(v.buffered.end(v.buffered.length-1)-v.currentTime).toFixed(2):'-1');})()""")
        print(f"   t={k+1:2}s  {s}")
    print("== 4) 汇总 ==")
    print(c.eval(RESULT))
    c.close()
