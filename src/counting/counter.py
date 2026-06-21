"""Per-camera counting state machine.

Feed it, each frame, the tracked people as (track_id, foot_point). It detects:
  - PASS  : a track crosses the tripwire line (counted once per person)
  - STOP  : a track dwells in the zone >= dwell_seconds, with its duration

Footfall (passers) ÷ stops = conversion. Each camera owns its own line + zone,
so a person is counted by exactly one camera (disjoint coverage by design).
"""
from __future__ import annotations

from dataclasses import dataclass

from .geometry import point_in_polygon, side_of_line


@dataclass
class _Track:
    prev_side: float | None = None
    in_zone: bool = False
    enter_t: float = 0.0
    counted_pass: bool = False
    last_cross_t: float = -1e9
    last_seen: float = 0.0


@dataclass
class Event:
    type: str  # "pass" | "stop"
    cam_id: str
    track_id: int
    dwell: float = 0.0


class CameraCounter:
    def __init__(self, cam_id, geom, dwell_seconds=3.0, cooldown_s=2.0, grace_s=1.5):
        tw = geom.get("tripwire")
        self.cam_id = cam_id
        self.A = tuple(tw[0]) if tw else None
        self.B = tuple(tw[1]) if tw else None
        self.zone = [tuple(p) for p in geom.get("zone", [])]
        self.dwell_seconds = dwell_seconds
        self.cooldown_s = cooldown_s
        self.grace_s = grace_s
        self.tracks: dict[int, _Track] = {}
        self.passers = 0
        self.stops = 0
        self.dwell_times: list[float] = []

    def _finalize_stop(self, st: _Track, end_t: float, tid: int, events: list):
        dwell = end_t - st.enter_t
        if dwell >= self.dwell_seconds:
            self.stops += 1
            self.dwell_times.append(dwell)
            events.append(Event("stop", self.cam_id, tid, dwell))

    def update(self, detections, now: float) -> list[Event]:
        """detections: iterable of (track_id, (foot_x, foot_y)). Returns events."""
        events: list[Event] = []
        seen = set()

        for tid, p in detections:
            seen.add(tid)
            st = self.tracks.get(tid) or _Track()
            st.last_seen = now

            # --- tripwire crossing (count each person once) ---
            if self.A is not None:
                s = side_of_line(self.A, self.B, p)
                if (
                    st.prev_side is not None
                    and s != 0
                    and (s > 0) != (st.prev_side > 0)
                    and now - st.last_cross_t > self.cooldown_s
                ):
                    st.last_cross_t = now
                    if not st.counted_pass:
                        st.counted_pass = True
                        self.passers += 1
                        events.append(Event("pass", self.cam_id, tid))
                if s != 0:
                    st.prev_side = s

            # --- zone dwell ---
            if self.zone:
                inside = point_in_polygon(self.zone, p)
                if inside and not st.in_zone:
                    st.in_zone = True
                    st.enter_t = now
                elif not inside and st.in_zone:
                    st.in_zone = False
                    self._finalize_stop(st, now, tid, events)

            self.tracks[tid] = st

        # --- finalize tracks that vanished while still in the zone ---
        for tid in list(self.tracks):
            if tid in seen:
                continue
            st = self.tracks[tid]
            if now - st.last_seen > self.grace_s:
                if st.in_zone:
                    self._finalize_stop(st, st.last_seen, tid, events)
                del self.tracks[tid]

        return events

    @property
    def conversion(self) -> float:
        return (self.stops / self.passers) if self.passers else 0.0
