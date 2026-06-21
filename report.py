"""Summarize data/counts.sqlite: passers, stops, conversion, dwell-time stats.

    python report.py [data/counts.sqlite]
"""
from __future__ import annotations

import sqlite3
import statistics
import sys


def pct(values, q):
    s = sorted(values)
    if not s:
        return 0.0
    return s[min(len(s) - 1, int(q * len(s)))]


def main(db_path: str = "data/counts.sqlite") -> int:
    con = sqlite3.connect(db_path)
    rows = con.execute("SELECT cam, type, dwell, ts FROM events").fetchall()
    con.close()
    if not rows:
        print("no events yet — run count.py first")
        return 0

    ts_vals = [r[3] for r in rows if r[3]]
    if ts_vals:
        lo, hi = min(ts_vals), max(ts_vals)
        print(f"period  : {lo.replace('T', ' ')}  ->  {hi.replace('T', ' ')}")
        print(f"events  : {len(rows)} total")

    cams = sorted({r[0] for r in rows})
    for cam in [*cams, "ALL"]:
        sel = [r for r in rows if cam == "ALL" or r[0] == cam]
        passers = sum(1 for r in sel if r[1] == "pass")
        dwells = [r[2] for r in sel if r[1] == "stop"]
        conv = (len(dwells) / passers * 100) if passers else 0.0
        print(f"\n== {cam} ==")
        print(f"  passers : {passers}")
        print(f"  stops   : {len(dwells)}   ({conv:.0f}% conversion)")
        if dwells:
            print(f"  dwell s : avg {statistics.mean(dwells):.1f} | "
                  f"median {statistics.median(dwells):.1f} | "
                  f"p90 {pct(dwells, 0.9):.1f} | max {max(dwells):.1f}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "data/counts.sqlite"))
