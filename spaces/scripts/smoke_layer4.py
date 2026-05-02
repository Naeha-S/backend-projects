from PIL import Image

from pipeline.layer4_provenance import check_provenance

im = Image.open("examples/fb_listing_screenshot.png").convert("RGB")
print(check_provenance(im))
