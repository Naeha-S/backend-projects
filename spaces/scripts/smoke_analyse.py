import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

from PIL import Image
from pipeline.orchestrator import analyse


def main():
    ex = ROOT / "examples" / "chanel_bag.png"
    if not ex.exists():
        print("Missing", ex)
        return 1
    im = Image.open(ex).convert("RGB")
    t0 = time.perf_counter()
    r = analyse(im, progress=None)
    dt = time.perf_counter() - t0
    print("seconds", round(dt, 2))
    print("layer1", r["layer1"]["source_type"])
    print("layer2", r["layer2"]["brand"], r["layer2"]["caption"][:80])
    print("layer3", r["layer3"]["confidence_score"], r["layer3"]["signal_label"])
    print("layer4", r["layer4"]["provenance_status"])
    print("layer5", r["layer5"]["severity"], r["layer5"]["actions"][:2])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())