import pathlib, sys
t = pathlib.Path('app.py')
raw = t.read_bytes()
src = raw.decode('utf-8').replace('\r\n', '\n')
marker = '    css = """\n'
s = src.find(marker)
print('marker found at index:', s)
print('first 40 chars after marker:', repr(src[s:s+60]) if s != -1 else 'N/A')
