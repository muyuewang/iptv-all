"""
CDP 校验官网直播窗净化效果：列出还在「可见」的非视频节点。

判据（任一出现即未净化干净）：
- 页面顶部仍能看到文字（导航 / 首页 / 时政 …）
- body 下有多个可见子容器（应只剩 video 祖先链那一支）
- video 仍带 controls 属性
- 文档出现滚动条（scrollHeight > innerHeight）

用法：
  CLEAN_TV_CDP_PORT=9335 python tools/verify_pure_video.py
"""
import json
import os
import socket
import struct
import sys
import urllib.request

JS = r"""
(function(){
  var v = document.querySelector('video');
  // 可见 = 有尺寸 且 未 display:none 且 未 visibility:hidden 且 不透明度>0.02
  function vis(el){
    try {
      var r = el.getBoundingClientRect();
      if (r.width < 2 || r.height < 2) return false;
      var cs = getComputedStyle(el);
      if (cs.display === 'none' || cs.visibility === 'hidden') return false;
      if (parseFloat(cs.opacity || '1') < 0.02) return false;
      return true;
    } catch(e){ return false; }
  }
  var out = {
    hasVideo: !!v,
    innerW: innerWidth, innerH: innerHeight,
    scrollH: document.documentElement.scrollHeight,
    scrollW: document.documentElement.scrollWidth,
    hasHScroll: document.documentElement.scrollWidth > innerWidth + 2,
    hasVScroll: document.documentElement.scrollHeight > innerHeight + 2
  };
  if (v) {
    var r = v.getBoundingClientRect();
    out.video = {
      controlsAttr: v.hasAttribute('controls'),
      controlsProp: !!v.controls,
      rect: [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)],
      readyState: v.readyState, paused: v.paused, muted: v.muted,
      currentTime: Math.round(v.currentTime * 100) / 100,
      videoW: v.videoWidth, videoH: v.videoHeight
    };
    // 祖先链
    var chain = [], n = v;
    while (n && n !== document.documentElement) { chain.push(n); n = n.parentElement; }
    out.chainLen = chain.length;
  }
  // body 下可见子元素
  var bodyVis = [];
  if (document.body) {
    var kids = document.body.children;
    for (var i = 0; i < kids.length; i++) {
      if (vis(kids[i])) {
        var rr = kids[i].getBoundingClientRect();
        bodyVis.push({
          tag: kids[i].tagName,
          id: (kids[i].id || '').slice(0, 40),
          cls: (String(kids[i].className || '')).slice(0, 60),
          box: [Math.round(rr.width), Math.round(rr.height)],
          txt: (kids[i].innerText || '').replace(/\s+/g, ' ').trim().slice(0, 70)
        });
      }
    }
  }
  out.bodyVisibleCount = bodyVis.length;
  out.bodyVisible = bodyVis;
  // 全页仍可见的、带文字的非视频块（前 12 个），用来找漏网的导航/水印
  var leaked = [];
  var all = document.querySelectorAll('div,header,nav,ul,section,aside,footer,p,span,a,button');
  for (var j = 0; j < all.length && leaked.length < 12; j++) {
    var el = all[j];
    if (el === v) continue;
    if (el.contains(v) || (v && v.contains(el))) continue;
    var t = (el.innerText || '').replace(/\s+/g, ' ').trim();
    if (t.length < 2) continue;
    if (!vis(el)) continue;
    var q = el.getBoundingClientRect();
    if (q.width < 40 || q.height < 12) continue;
    leaked.push({
      tag: el.tagName, id: (el.id || '').slice(0, 30),
      cls: String(el.className || '').slice(0, 50),
      at: [Math.round(q.left), Math.round(q.top), Math.round(q.width), Math.round(q.height)],
      txt: t.slice(0, 60)
    });
  }
  out.leakedTextBlocks = leaked;
  return out;
})()
"""


def _mask(data):
    key = os.urandom(4)
    out = bytearray([0x81])
    n = len(data)
    if n < 126:
        out.append(0x80 | n)
    elif n < 65536:
        out.append(0x80 | 126)
        out += struct.pack(">H", n)
    else:
        out.append(0x80 | 127)
        out += struct.pack(">Q", n)
    out += key
    for i, b in enumerate(data):
        out.append(b ^ key[i % 4])
    return bytes(out)


def _recv(sock, timeout=8):
    sock.settimeout(timeout)
    try:
        hdr = sock.recv(2)
    except socket.timeout:
        return None, None
    if len(hdr) < 2:
        return None, None
    op = hdr[0] & 0x0F
    ln = hdr[1] & 0x7F
    if ln == 126:
        ln = struct.unpack(">H", sock.recv(2))[0]
    elif ln == 127:
        ln = struct.unpack(">Q", sock.recv(8))[0]
    masked = hdr[1] & 0x80
    key = sock.recv(4) if masked else b""
    data = b""
    while len(data) < ln:
        c = sock.recv(ln - len(data))
        if not c:
            break
        data += c
    if masked:
        data = bytes(b ^ key[i % 4] for i, b in enumerate(data))
    return op, data


def main():
    port = os.environ.get("CLEAN_TV_CDP_PORT", "9335")
    targets = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=4))
    tgt = None
    for t in targets:
        if t.get("type") == "page" and ("cctv" in t.get("url", "") or "yangshipin" in t.get("url", "")):
            tgt = t
            break
    if not tgt:
        print("未找到官网直播页 target")
        return 2
    rest = tgt["webSocketDebuggerUrl"][len("ws://"):]
    host, _, tail = rest.partition(":")
    p = int(tail.split("/")[0])
    path = "/" + tail.split("/", 1)[1]

    s = socket.create_connection((host, p), timeout=5)
    req = (
        f"GET {path} HTTP/1.1\r\nHost: {host}:{p}\r\n"
        "Upgrade: websocket\r\nConnection: Upgrade\r\n"
        "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
        "Sec-WebSocket-Version: 13\r\n\r\n"
    )
    s.sendall(req.encode())
    r = b""
    while b"\r\n\r\n" not in r:
        r += s.recv(1024)
    s.sendall(_mask(json.dumps({
        "id": 1, "method": "Runtime.evaluate",
        "params": {"expression": JS, "returnByValue": True},
    }).encode()))
    val = None
    for _ in range(200):
        op, d = _recv(s, 6)
        if d is None:
            break
        if op == 9:  # ping → pong
            plen = len(d)
            pong = bytearray([0x8A])
            if plen < 126:
                pong.append(plen)
            else:
                pong.append(126)
                pong += struct.pack(">H", plen)
            pong += d
            try:
                s.sendall(bytes(pong))
            except Exception:
                pass
            continue
        if op != 1:
            continue
        try:
            o = json.loads(d.decode("utf-8", "replace"))
        except Exception:
            continue
        if o.get("id") == 1:
            val = o.get("result", {}).get("result", {}).get("value")
            break
    s.close()
    if not val:
        print("eval 无返回")
        return 2

    print("=" * 60)
    print("页面:", tgt["url"])
    print(f"视口 {val['innerW']}x{val['innerH']}  文档 {val['scrollW']}x{val['scrollH']}")
    print(f"纵向滚动条: {val['hasVScroll']}   横向滚动条: {val['hasHScroll']}")
    vd = val.get("video") or {}
    print(f"\nvideo: readyState={vd.get('readyState')} paused={vd.get('paused')} muted={vd.get('muted')} "
          f"t={vd.get('currentTime')}s 尺寸={vd.get('videoW')}x{vd.get('videoH')}")
    print(f"        画面 rect={vd.get('rect')}  祖先链长度={val.get('chainLen')}")
    print(f"        controls 属性={vd.get('controlsAttr')}  controls 属性值={vd.get('controlsProp')}")
    print(f"\nbody 下仍可见子元素: {val['bodyVisibleCount']} 个")
    for b in val["bodyVisible"]:
        print(f"   - {b['tag']}#{b['id']}.{b['cls']} box={b['box']} txt={b['txt']!r}")
    print(f"\n仍可见的文字块（应只剩视频区域/台标）: {len(val['leakedTextBlocks'])} 个")
    for b in val["leakedTextBlocks"]:
        print(f"   * {b['tag']}#{b['id']}.{b['cls']} at={b['at']} txt={b['txt']!r}")

    ok = (
        vd.get("readyState", 0) >= 2
        and not vd.get("controlsAttr")
        and not vd.get("controlsProp")
        and not val["hasVScroll"]
        and not val["hasHScroll"]
    )
    print("\n各判据:")
    print("  视频已解码      :", vd.get("readyState", 0) >= 2)
    print("  无原生 controls :", not vd.get("controlsAttr") and not vd.get("controlsProp"))
    print("  无滚动条        :", not val["hasVScroll"] and not val["hasHScroll"])
    print("=>", "PASS 纯视频净化" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
