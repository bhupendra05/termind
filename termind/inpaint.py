"""Neural inpainting (LaMa) — generative object removal that reconstructs what was BEHIND.

Classical inpainting (cv2) smears surrounding pixels — fine for logos on flat areas, ugly on
photos. LaMa is a generative model that invents plausible content for the hole. We run the
ONNX port locally via onnxruntime (already used by rembg). The ~200MB model is downloaded
once to ~/.termind/models/ and never phones home again.
"""
from __future__ import annotations

import os
import urllib.request

LAMA_URL = "https://huggingface.co/Carve/LaMa-ONNX/resolve/main/lama_fp32.onnx"
SIZE = 512  # the ONNX export takes fixed 512x512 inputs


def model_path() -> str:
    root = os.path.join(os.environ.get("TERMIND_HOME", os.path.expanduser("~/.termind")),
                        "models")
    os.makedirs(root, exist_ok=True)
    return os.path.join(root, "lama_fp32.onnx")


def model_ready() -> bool:
    p = model_path()
    return os.path.isfile(p) and os.path.getsize(p) > 50_000_000


def download_model(progress=None) -> str:
    """One-time ~200MB download (HuggingFace). Returns the local path."""
    p = model_path()
    if model_ready():
        return p
    tmp = p + ".part"

    def hook(blocks, bsize, total):
        if progress and total > 0:
            progress(min(100, blocks * bsize * 100 // total))

    urllib.request.urlretrieve(LAMA_URL, tmp, reporthook=hook)  # noqa: S310
    os.replace(tmp, p)
    return p


_session = None


def _ort():
    global _session
    if _session is None:
        import onnxruntime as ort
        _session = ort.InferenceSession(model_path(), providers=["CPUExecutionProvider"])
    return _session


def crop_window(w: int, h: int, bbox_px):
    """A square context window around the hole (2x its size, clamped into the image)."""
    x1, y1, x2, y2 = bbox_px
    side = int(max(x2 - x1, y2 - y1) * 2)
    side = max(side, min(SIZE, min(w, h)))          # at least model-res context if possible
    side = min(side, w, h)
    cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
    left = min(max(0, cx - side // 2), w - side)
    top = min(max(0, cy - side // 2), h - side)
    return left, top, left + side, top + side


def inpaint_bbox(img, bbox_pct):
    """Remove the bbox region (percent coords) from a PIL image via LaMa. Returns PIL RGB."""
    import numpy as np
    from PIL import Image

    rgb = img.convert("RGB")
    w, h = rgb.size
    bbox_px = (int(bbox_pct[0] / 100 * w), int(bbox_pct[1] / 100 * h),
               int(bbox_pct[2] / 100 * w), int(bbox_pct[3] / 100 * h))
    cl, ct, cr, cb = crop_window(w, h, bbox_px)
    crop = rgb.crop((cl, ct, cr, cb)).resize((SIZE, SIZE), Image.LANCZOS)
    scale = SIZE / (cr - cl)

    # dilate the hole: resize anti-aliasing smears object edges OUTSIDE an exact mask,
    # and LaMa would propagate those fringes back in. ~3% margin removes them entirely.
    pad = max(8, int((cr - cl) * 0.03))
    pbox = (max(cl, bbox_px[0] - pad), max(ct, bbox_px[1] - pad),
            min(cr, bbox_px[2] + pad), min(cb, bbox_px[3] + pad))
    mask = np.zeros((SIZE, SIZE), np.float32)
    mx1 = max(0, int((pbox[0] - cl) * scale))
    my1 = max(0, int((pbox[1] - ct) * scale))
    mx2 = min(SIZE, int((pbox[2] - cl) * scale))
    my2 = min(SIZE, int((pbox[3] - ct) * scale))
    mask[my1:my2, mx1:mx2] = 1.0

    arr = np.asarray(crop, np.float32).transpose(2, 0, 1)[None] / 255.0
    out = _ort().run(None, {"image": arr, "mask": mask[None, None]})[0][0]
    if out.max() <= 1.5:                            # some exports emit 0-1, some 0-255
        out = out * 255.0
    out_img = Image.fromarray(out.transpose(1, 2, 0).clip(0, 255).astype("uint8"))

    # paste the reconstructed (dilated) hole back at original resolution
    out_full = out_img.resize((cr - cl, cb - ct), Image.LANCZOS)
    hole = out_full.crop((pbox[0] - cl, pbox[1] - ct, pbox[2] - cl, pbox[3] - ct))
    result = rgb.copy()
    result.paste(hole, (pbox[0], pbox[1]))
    return result
