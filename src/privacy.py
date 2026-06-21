"""Face privacy (LGPD): obscure people's heads in any frame we save.

We reuse the existing PERSON boxes — the head is the top `head_frac` of each box —
so there's NO extra face-detection model and negligible compute (a couple of small
resizes per person). Because it's box-driven, it covers heads even when no face is
visible (back of head, profile, distance), so there are no missed-face gaps.

Assumes upright people (head at the top of the box) — true for a standing counter.

modes:
  pixelate : mosaic the head region (default; nicer, still cheap)
  box      : solid black rectangle (cheapest)
  blur     : gaussian blur
  off      : do nothing
"""
from __future__ import annotations

import cv2


def obscure_heads(image, boxes, mode="pixelate", head_frac=0.28, blocks=12):
    """Obscure the head region of each person box, in place. Returns image.

    boxes: iterable of (x1, y1, x2, y2) in pixel coords.
    """
    if mode == "off" or boxes is None or len(boxes) == 0:
        return image
    h, w = image.shape[:2]
    for box in boxes:
        x1, y1, x2, y2 = (int(v) for v in box)
        x1, y1 = max(0, x1), max(0, y1)
        x2, y2 = min(w, x2), min(h, y2)
        if x2 <= x1 or y2 <= y1:
            continue
        head_h = max(1, int((y2 - y1) * head_frac))
        hy2 = min(y2, y1 + head_h)
        roi = image[y1:hy2, x1:x2]
        if roi.size == 0:
            continue
        if mode == "box":
            image[y1:hy2, x1:x2] = 0
        elif mode == "blur":
            k = max(3, (min(roi.shape[:2]) // 2) | 1)  # odd kernel
            image[y1:hy2, x1:x2] = cv2.GaussianBlur(roi, (k, k), 0)
        else:  # pixelate
            rh, rw = roi.shape[:2]
            bw, bh = max(1, min(blocks, rw)), max(1, min(blocks, rh))
            small = cv2.resize(roi, (bw, bh), interpolation=cv2.INTER_LINEAR)
            image[y1:hy2, x1:x2] = cv2.resize(
                small, (rw, rh), interpolation=cv2.INTER_NEAREST
            )
    return image
