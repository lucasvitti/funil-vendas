"""Per-camera counting state machine.

Feed it, each frame, the tracked people as (track_id, foot_point). It detects:
  - PASS  : a track crosses the tripwire line (counted once per person)
  - STOP  : a track dwells in the zone >= dwell_seconds, with its duration

Footfall (passers) ÷ stops = conversion. Each camera owns its own line + zone,
so a person is counted by exactly one camera (disjoint coverage by design).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .geometry import centroid, parse_tripwires, point_in_polygon, side_of_line


@dataclass
class _Track:
    prev_sides: dict = field(default_factory=dict)   # tripwire index -> last side sign
    last_cross: dict = field(default_factory=dict)   # tripwire index -> last crossing time
    in_zone: bool = False
    enter_t: float = 0.0
    counted_pass: bool = False
    last_seen: float = 0.0
    cls: int = -1         # latest detection class id for this track


@dataclass
class Event:
    type: str  # "pass" (counted footfall) | "cross" (any other crossing) | "stop"
    cam_id: str
    track_id: int
    dwell: float = 0.0
    direction: str = ""   # "in"/"out" for crossings, "stop" for stops (never blank)
    seg: int = -1         # tripwire segment crossed (-1 for stops)
    cls: int = -1         # detection class id (person/cat/...) — resolved to a name on log


class CameraCounter:
    def __init__(self, cam_id, geom, dwell_seconds=3.0, cooldown_s=2.0, grace_s=1.5,
                 cross_mode="inward", log_crossings=True):
        self.cam_id = cam_id
        self.tripwires = parse_tripwires(geom)   # list of (A, B) fence segments
        self.zone = [tuple(p) for p in geom.get("zone", [])]
        self.cross_mode = cross_mode             # "inward" (count entries only) | "any"
        self.log_crossings = log_crossings       # also emit a "cross" event for every crossing
        # inward sign per segment = which side of it the zone sits on (fallback +1)
        cz = centroid(self.zone)
        self.inward = [(1.0 if side_of_line(A, B, cz) >= 0 else -1.0) if cz else 1.0
                       for (A, B) in self.tripwires]
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
            events.append(Event("stop", self.cam_id, tid, dwell, direction="stop", cls=st.cls))

    def update(self, detections, now: float) -> list[Event]:
        """detections: iterable of (track_id, (foot_x, foot_y)). Returns events."""
        events: list[Event] = []
        seen = set()

        for det in detections:
            tid, p = det[0], det[1]
            cls = det[2] if len(det) > 2 else -1   # optional detection class id
            seen.add(tid)
            st = self.tracks.get(tid) or _Track()
            st.last_seen = now
            if cls >= 0:
                st.cls = cls

            # --- tripwire crossings: footfall counted once ("pass"); every other
            #     crossing recorded as "cross" (per-segment cooldown kills jitter) ---
            for i, (A, B) in enumerate(self.tripwires):
                s = side_of_line(A, B, p)
                prev = st.prev_sides.get(i)
                if (
                    prev is not None
                    and s != 0
                    and (s > 0) != (prev > 0)
                    and now - st.last_cross.get(i, -1e9) > self.cooldown_s
                ):
                    st.last_cross[i] = now
                    direction = "in" if ((s > 0) == (self.inward[i] > 0)) else "out"
                    footfall = (self.cross_mode != "inward" or direction == "in") and not st.counted_pass
                    if footfall:
                        st.counted_pass = True
                        self.passers += 1
                        events.append(Event("pass", self.cam_id, tid, direction=direction, seg=i, cls=st.cls))
                    elif self.log_crossings:
                        events.append(Event("cross", self.cam_id, tid, direction=direction, seg=i, cls=st.cls))
                if s != 0:
                    st.prev_sides[i] = s

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
