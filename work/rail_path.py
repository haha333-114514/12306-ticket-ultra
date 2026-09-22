"""Time-dependent railway path search over a packaged GTFS timetable."""

from __future__ import annotations

import bisect
import csv
import heapq
import io
import json
import math
import re
import zipfile
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path


def minutes(value: str) -> int:
    hour, minute, _ = map(int, value.split(":"))
    return hour * 60 + minute


class Timetable:
    def __init__(self, source: Path, city_map_path: Path | None = None):
        self.source = source
        self.names: dict[str, str] = {}
        self.coords: dict[str, tuple[float, float]] = {}
        self.trips: dict[str, list[tuple[str, int, int]]] = defaultdict(list)
        self.departures: dict[str, list[tuple[int, str, int]]] = defaultdict(list)
        with zipfile.ZipFile(source) as archive:
            def rows(name):
                return csv.DictReader(io.TextIOWrapper(archive.open(name), encoding="utf-8-sig"))

            for row in rows("stops.txt"):
                self.names[row["stop_name"].strip()] = row["stop_id"]
                try:
                    self.coords[row["stop_id"]] = (float(row["stop_lat"]), float(row["stop_lon"]))
                except (ValueError, KeyError):
                    pass
            labels = {row["trip_id"]: row["trip_short_name"].strip() for row in rows("trips.txt")
                      if not row["trip_id"].startswith("DUMMY_")}
            for row in rows("stop_times.txt"):
                trip = row["trip_id"]
                if trip in labels:
                    self.trips[trip].append((row["stop_id"], minutes(row["arrival_time"]),
                                             minutes(row["departure_time"])))
        self.labels = labels
        self.stop_names = {code: name for name, code in self.names.items()}
        station_cities = json.loads(city_map_path.read_text(encoding="utf-8")) if city_map_path else {}
        self.city_of = {code: station_cities.get(name, name) for name, code in self.names.items()}
        self.city_stations: dict[str, set[str]] = defaultdict(set)
        for trip, stops in self.trips.items():
            for index, (station, _, departure) in enumerate(stops[:-1]):
                self.departures[station].append((departure, trip, index))
                if self.stop_names[station] in station_cities:
                    self.city_stations[self.city_of[station]].add(station)
        for departures in self.departures.values():
            departures.sort()
        self.city_core: dict[str, set[str]] = {}
        for city, stations in self.city_stations.items():
            anchor = self.names.get(city)
            if anchor not in stations:
                anchor = max(stations, key=lambda station: len(self.departures[station]))
            busiest = max(len(self.departures[station]) for station in stations)
            nearby = [station for station in stations
                      if self.distance_km(anchor, station) <= 30
                      and (station == anchor or self.stop_names[station].startswith(city)
                           or len(self.departures[station]) >= max(30, busiest * 0.2))]
            nearby.sort(key=lambda station: (station == anchor,
                        len(self.departures[station])), reverse=True)
            self.city_core[city] = set(nearby[:3]) or {anchor}

    def distance_km(self, from_station: str, to_station: str) -> float:
        if from_station == to_station:
            return 0
        if from_station not in self.coords or to_station not in self.coords:
            return float("inf")
        lat1, lon1 = self.coords[from_station]
        lat2, lon2 = self.coords[to_station]
        latitude = math.radians(lat2 - lat1)
        longitude = math.radians(lon2 - lon1)
        arc = (math.sin(latitude / 2) ** 2 + math.cos(math.radians(lat1))
               * math.cos(math.radians(lat2)) * math.sin(longitude / 2) ** 2)
        return 12742 * math.asin(min(1, math.sqrt(arc)))

    def transfer_minutes(self, from_station: str, to_station: str) -> int:
        if from_station == to_station:
            return 20
        if from_station not in self.coords or to_station not in self.coords:
            return 60
        kilometers = self.distance_km(from_station, to_station)
        return max(60, math.ceil(kilometers / 40 * 60 + 20))

    def _find_earliest(self, city_names: list[str], start: datetime, mode: str,
                       earliest_min: int, max_transfers: int, core_only: bool = False,
                       latest_arrival: int | None = None) -> dict:
        if not 2 <= len(city_names) <= 6 or len(set(city_names)) != len(city_names):
            raise ValueError("请按顺序输入 2 至 6 个不同的城市。")
        if any(name not in self.city_stations for name in city_names):
            unknown = [name for name in city_names if name not in self.city_stations]
            raise ValueError("时刻表中没有这些城市：" + "、".join(unknown))
        if mode not in ("all", "high", "regular"):
            raise ValueError("列车类型无效。")
        wanted = city_names
        origin_stations = self.city_core[wanted[0]] if core_only else self.city_stations[wanted[0]]
        allowed = {trip for trip, label in self.labels.items()
                   if (mode == "all" or bool(re.match(r"^[GDC]", label)) == (mode == "high"))}
        start_min = earliest_min
        deadline = start.hour * 60 + start.minute + 72 * 60
        if latest_arrival is not None:
            deadline = min(deadline, latest_arrival)
        # A state is a train arrival. Board at the same station after 20 minutes,
        # or at another station in the same city after at least 60 minutes.
        # Staying on the same train needs no transfer buffer.
        states = [(start_min, next(iter(origin_stations)), None, -1, 0, -1, None, 0)]
        heap = [(start_min, 0)]
        expanded_station: set[tuple[str, int, int]] = set()
        final = None

        def board(from_state: int, target_station: str, earliest: int, progress: int) -> None:
            if (core_only and progress > 0 and progress < len(wanted) - 1
                    and self.city_of[target_station] == wanted[progress]
                    and target_station not in self.city_core[wanted[progress]]):
                return
            transfers = states[from_state][7] + (0 if states[from_state][5] == -1 else 1)
            if transfers > max_transfers:
                return
            departures = self.departures.get(target_station, ())
            origin_city = self.city_of[target_station]
            for day in range(-3, 5):
                low = bisect.bisect_left(departures, (earliest - day * 1440, "", -1))
                for departure, candidate, stop_index in departures[low:]:
                    absolute = departure + day * 1440
                    if absolute > min(deadline, earliest + 1440):
                        break
                    if candidate not in allowed or candidate == states[from_state][2]:
                        continue
                    stops = self.trips[candidate]
                    # A newly boarded train must leave the city; an intra-city
                    # train ride is not a shortcut around the 60-minute rule.
                    next_index = stop_index + 1
                    while next_index < len(stops) and self.city_of[stops[next_index][0]] == origin_city:
                        next_index += 1
                    if next_index == len(stops):
                        continue
                    next_station, arrival, _ = stops[next_index]
                    absolute_arrival = arrival + day * 1440
                    if absolute_arrival < absolute or absolute_arrival > deadline:
                        continue
                    new_id = len(states)
                    states.append((absolute_arrival, next_station, candidate, next_index,
                                   progress, from_state,
                                   (candidate, target_station, next_station, absolute, absolute_arrival),
                                   transfers))
                    heapq.heappush(heap, (absolute_arrival, new_id))

        while heap:
            now, state_id = heapq.heappop(heap)
            time_at, station, trip, index, progress, parent, edge, transfers = states[state_id]
            if now != time_at or now > deadline:
                continue
            city = self.city_of[station]
            if (progress < len(wanted) - 1 and city == wanted[progress + 1]
                    and (not core_only or station in self.city_core[city])):
                progress += 1
            if progress == len(wanted) - 1:
                final = state_id
                break
            if trip is not None:
                stops = self.trips[trip]
                if index + 1 < len(stops):
                    next_station, arrival, _ = stops[index + 1]
                    departure = stops[index][2]
                    day = (time_at - stops[index][1]) // 1440
                    arrival += day * 1440
                    departure += day * 1440
                    if arrival >= time_at and arrival <= deadline:
                        new_id = len(states)
                        states.append((arrival, next_station, trip, index + 1, progress,
                                       state_id, (trip, station, next_station, departure, arrival), transfers))
                        heapq.heappush(heap, (arrival, new_id))
            if parent == -1:
                for target in origin_stations:
                    board(state_id, target, now, progress)
            else:
                station_key = (station, progress, transfers)
                if station_key not in expanded_station:
                    expanded_station.add(station_key)
                    board(state_id, station, now + 20, progress)
                    for target in self.city_stations.get(city, ()):
                        if target != station:
                            board(state_id, target, now + self.transfer_minutes(station, target), progress)
        if final is None:
            return {}
        edges = []
        cursor = final
        while cursor > 0:
            state = states[cursor]
            if state[6]:
                edges.append(state[6])
            cursor = state[5]
        edges.reverse()
        legs = []
        midnight = start.replace(hour=0, minute=0, second=0, microsecond=0)
        seen_via = set()
        for trip, from_stop, to_stop, depart, arrive in edges:
            if (legs and legs[-1]["tripId"] == trip and legs[-1]["to"] == self.stop_names[from_stop]
                    ):
                legs[-1]["to"] = self.stop_names[to_stop]
                legs[-1]["toCity"] = self.city_of[to_stop]
                legs[-1]["arriveAt"] = (midnight + timedelta(minutes=arrive)).isoformat(timespec="minutes")
            else:
                legs.append({"tripId": trip, "train": self.labels[trip],
                             "from": self.stop_names[from_stop], "to": self.stop_names[to_stop],
                             "fromCity": self.city_of[from_stop], "toCity": self.city_of[to_stop],
                             "via": [],
                             "departAt": (midnight + timedelta(minutes=depart)).isoformat(timespec="minutes"),
                             "arriveAt": (midnight + timedelta(minutes=arrive)).isoformat(timespec="minutes")})
            via_city = self.city_of[to_stop]
            if via_city in wanted[1:-1] and via_city not in seen_via:
                legs[-1]["via"].append({"city": via_city, "station": self.stop_names[to_stop],
                                         "at": (midnight + timedelta(minutes=arrive)).isoformat(timespec="minutes")})
                seen_via.add(via_city)
        for index in range(1, len(legs)):
            previous, current = legs[index - 1], legs[index]
            if previous["tripId"] != current["tripId"]:
                buffer = self.transfer_minutes(self.names[previous["to"]], self.names[current["from"]])
                current["connection"] = f"跨站换乘（预留至少 {buffer} 分钟）" if previous["to"] != current["from"] else "同站换乘（至少 20 分钟）"
        return {"cities": city_names, "legs": legs, "stationScope": "core" if core_only else "all",
                "transfers": sum(legs[i]["tripId"] != legs[i - 1]["tripId"] for i in range(1, len(legs))),
                "departAt": legs[0]["departAt"],
                "arriveAt": (midnight + timedelta(minutes=states[final][0])).isoformat(timespec="minutes"),
                "durationMinutes": states[final][0] - edges[0][3],
                "source": self.source.name}

    def find(self, city_names: list[str], start: datetime, mode: str,
             preferred_time: str | None = None) -> dict:
        if preferred_time is None:
            preferred_time = start.strftime("%H:%M")
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", preferred_time):
            raise ValueError("理想发车时间无效。")
        ideal = int(preferred_time[:2]) * 60 + int(preferred_time[3:])
        earliest = start.hour * 60 + start.minute
        thresholds = {earliest}
        for offset in (-90, 0, 60, 120):
            threshold = ideal + offset
            if threshold >= earliest:
                thresholds.add(threshold)
        allowance = len(city_names) - 1
        candidates = {}
        for core_only in (True, False):
            for threshold in sorted(thresholds):
                arrival_bound = None
                for limit in sorted({max(0, allowance - 1), allowance, allowance + 1}, reverse=True):
                    result = self._find_earliest(city_names, start, mode, threshold, limit,
                                                 core_only, arrival_bound)
                    if result:
                        arrival = datetime.fromisoformat(result["arriveAt"])
                        midnight = start.replace(hour=0, minute=0, second=0, microsecond=0)
                        bound = int((arrival - midnight).total_seconds() / 60) + 360
                        arrival_bound = bound if arrival_bound is None else min(arrival_bound, bound)
                        key = tuple((leg["tripId"], leg["from"], leg["to"], leg["departAt"])
                                    for leg in result["legs"])
                        candidates[key] = result
                    elif arrival_bound is None:
                        break
                if not candidates:
                    break
            if candidates:
                break
        if not candidates:
            for limit in range(allowance + 2, 9):
                for core_only in (True, False):
                    result = self._find_earliest(city_names, start, mode, earliest, limit, core_only)
                    if result:
                        candidates[tuple((leg["tripId"], leg["departAt"]) for leg in result["legs"])] = result
                        break
                if candidates:
                    break
        if not candidates:
            raise ValueError("这份时刻表在出发后 72 小时内没有找到串联所有城市的方案。")
        durations = [item["durationMinutes"] for item in candidates.values()]
        shortest, longest = min(durations), max(durations)
        for result in candidates.values():
            departure = datetime.fromisoformat(result["departAt"])
            actual = departure.hour * 60 + departure.minute
            delta = (actual - ideal + 720) % 1440 - 720
            time_part = math.exp(-0.5 * (delta / 90) ** 2)
            duration_part = (1 if shortest == longest else
                             (longest - result["durationMinutes"]) / (longest - shortest))
            excess = max(0, result["transfers"] - allowance)
            penalty = 5 * result["transfers"] + 35 * excess
            result["score"] = round(max(0, 100 * (50 / 85 * time_part + 35 / 85 * duration_part) - penalty), 1)
            result["scoreParts"] = {"departure": round(100 * 50 / 85 * time_part, 1),
                                    "duration": round(100 * 35 / 85 * duration_part, 1),
                                    "transferPenalty": penalty, "excessTransfers": excess}
        ranked = sorted(candidates.values(), key=lambda item: (-item["score"], item["transfers"],
                                                               item["durationMinutes"], item["arriveAt"]))
        best = ranked[0]
        best["alternatives"] = [{key: value for key, value in item.items() if key != "alternatives"}
                                for item in ranked[1:4]]
        return best
