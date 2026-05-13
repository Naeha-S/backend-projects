from PIL import Image
import imagehash
r=Image.new('RGB',(64,64),color=(255,0,0))
g=Image.new('RGB',(64,64),color=(0,255,0))
hr=str(imagehash.phash(r))
hg=str(imagehash.phash(g))
print('red',hr)
print('green',hg)
print('dist', imagehash.phash(r)-imagehash.phash(g))
