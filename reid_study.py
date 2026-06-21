"""Study the re-ID distance distribution to choose a threshold.

Track IDs are free labels: vectors with the SAME track id (within a session) are the
SAME person; DIFFERENT track ids are (almost always) different people. So we compute:
  - intra-track distances  -> same-person distribution
  - inter-track distances  -> different-person distribution
The gap between them is your match threshold. Pull data/reid.sqlite from the Pi first:
    scp pi-cam:~/counter_vision/data/reid.sqlite .
    python reid_study.py reid.sqlite
"""
from __future__ import annotations

import sqlite3
import sys

import numpy as np


def _cos(a, b):
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    return 2.0 if na == 0 or nb == 0 else 1.0 - float(np.dot(a, b) / (na * nb))


def _pcts(vals, ps=(5, 25, 50, 75, 90, 95)):
    if not vals:
        return "  (none)"
    a = np.sort(np.array(vals))
    return "  ".join(f"p{p}={np.percentile(a, p):.3f}" for p in ps)


def main(db_path="reid.sqlite"):
    con = sqlite3.connect(db_path)
    rows = con.execute("SELECT track, vec, mode FROM vectors").fetchall()
    con.close()
    if not rows:
        print("no vectors yet — run count.py with reid.enabled first")
        return 0

    modes = {r[2] for r in rows}
    tracks = [r[0] for r in rows]
    vecs = [np.frombuffer(r[1], dtype=np.float32) for r in rows]
    n = len(vecs)
    print(f"{n} vectors, {len(set(tracks))} distinct tracks, mode(s)={sorted(modes)}\n")

    intra, inter = [], []
    for i in range(n):
        for j in range(i + 1, n):
            d = _cos(vecs[i], vecs[j])
            (intra if tracks[i] == tracks[j] else inter).append(d)

    print(f"SAME person (intra-track, n={len(intra)}):")
    print(_pcts(intra))
    print(f"\nDIFFERENT people (inter-track, n={len(inter)}):")
    print(_pcts(inter))

    if intra and inter:
        # suggest a threshold: midpoint between same-person p90 and different-person p10
        same_hi = np.percentile(intra, 90)
        diff_lo = np.percentile(inter, 10)
        thr = (same_hi + diff_lo) / 2
        sep = diff_lo - same_hi
        print(f"\nsame p90 = {same_hi:.3f} | diff p10 = {diff_lo:.3f}")
        print(f">>> suggested match_threshold ~ {thr:.3f}"
              + ("  (clean separation)" if sep > 0 else "  (OVERLAP — histogram may be too weak; consider a learned embedding)"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "reid.sqlite"))
