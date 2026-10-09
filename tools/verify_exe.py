import os, sys
from PyInstaller.archive.readers import CArchiveReader

exe = sys.argv[1] if len(sys.argv) > 1 else 'dist/电视直播.exe'
print('size:', os.path.getsize(exe))
r = CArchiveReader(exe)
found = False
for k in r.toc.keys():
    kk = k.replace('\\', '/')
    if kk.endswith('assets/index.html'):
        d = r.extract(k)
        if isinstance(d, bytes):
            d = d.decode('utf-8', 'ignore')
        print('assets/index.html len', len(d))
        for key in ('bindDblClickFs', 'numBuf', 'pickBestLine', 'bindStallWatch', 'mpegtsCfg'):
            print('   ', ('OK  ' if key in d else 'MISS') + key)
        found = True
        break
if not found:
    print('index.html NOT FOUND in archive')
    for k in list(r.toc.keys())[:20]:
        print('  toc:', k)
