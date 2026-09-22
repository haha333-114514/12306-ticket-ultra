"""City routing and transfer-buffer regressions."""

import unittest
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from rail_path import Timetable


def fixture(trains):
    graph = Timetable.__new__(Timetable)
    graph.source = Path("fixture.zip")
    graph.names = {name: name for name in ("A1", "B1", "B2", "C1")}
    graph.stop_names = dict(graph.names)
    graph.coords = {}
    graph.city_of = {"A1": "A", "B1": "B", "B2": "B", "C1": "C"}
    graph.city_stations = {"A": {"A1"}, "B": {"B1", "B2"}, "C": {"C1"}}
    graph.city_core = {city: set(stations) for city, stations in graph.city_stations.items()}
    graph.trips = {}
    graph.labels = {}
    graph.departures = defaultdict(list)
    for name, stops in trains.items():
        graph.labels[name] = name
        graph.trips[name] = [(station, time, time) for station, time in stops]
        for index, (station, time) in enumerate(stops[:-1]):
            graph.departures[station].append((time, name, index))
    for departures in graph.departures.values():
        departures.sort()
    return graph


class CityPathTests(unittest.TestCase):
    def test_same_station_requires_twenty_minutes(self):
        graph = fixture({"G1": [("A1", 540), ("B1", 600)],
                         "G2": [("B1", 619), ("C1", 650)],
                         "G3": [("B1", 620), ("C1", 660)]})
        result = graph.find(["A", "B", "C"], datetime(2026, 9, 23, 9), "all")
        self.assertEqual([leg["train"] for leg in result["legs"]], ["G1", "G3"])

    def test_cross_station_requires_sixty_minutes(self):
        graph = fixture({"G1": [("A1", 540), ("B1", 600)],
                         "G2": [("B2", 659), ("C1", 670)],
                         "G3": [("B2", 660), ("C1", 675)]})
        result = graph.find(["A", "B", "C"], datetime(2026, 9, 23, 9), "all")
        self.assertEqual([leg["train"] for leg in result["legs"]], ["G1", "G3"])
        self.assertIn("跨站", result["legs"][1]["connection"])

    def test_city_local_train_cannot_bypass_cross_station_buffer(self):
        graph = fixture({"G1": [("A1", 540), ("B1", 600)],
                         "C1": [("B1", 601), ("B2", 610)],
                         "G2": [("B2", 630), ("C1", 650)],
                         "G3": [("B2", 660), ("C1", 670)]})
        result = graph.find(["A", "B", "C"], datetime(2026, 9, 23, 9), "all")
        self.assertEqual([leg["train"] for leg in result["legs"]], ["G1", "G3"])

    def test_distant_stations_need_more_than_sixty_minutes(self):
        graph = fixture({"G1": [("A1", 540), ("B1", 600)],
                         "G2": [("B2", 670), ("C1", 680)],
                         "G3": [("B2", 770), ("C1", 780)]})
        graph.coords = {"B1": (36.0, 120.0), "B2": (36.8, 120.0)}
        self.assertGreater(graph.transfer_minutes("B1", "B2"), 70)
        result = graph.find(["A", "B", "C"], datetime(2026, 9, 23, 9), "all")
        self.assertEqual([leg["train"] for leg in result["legs"]], ["G1", "G3"])

    def test_ideal_departure_changes_recommendation(self):
        graph = fixture({"G1": [("A1", 540), ("C1", 660)],
                         "G2": [("A1", 600), ("C1", 720)]})
        early = graph.find(["A", "C"], datetime(2026, 9, 23, 9), "all", "09:00")
        late = graph.find(["A", "C"], datetime(2026, 9, 23, 9), "all", "10:00")
        self.assertEqual(early["legs"][0]["train"], "G1")
        self.assertEqual(late["legs"][0]["train"], "G2")

    def test_excess_transfer_penalty_can_favor_slower_direct_train(self):
        graph = fixture({"G0": [("A1", 540), ("C1", 780)],
                         "G1": [("A1", 540), ("B1", 600)],
                         "G2": [("B1", 620), ("D1", 650)],
                         "G3": [("D1", 670), ("C1", 700)]})
        graph.names["D1"] = "D1"
        graph.stop_names["D1"] = "D1"
        graph.city_of["D1"] = "D"
        graph.city_stations["D"] = {"D1"}
        graph.city_core["D"] = {"D1"}
        result = graph.find(["A", "C"], datetime(2026, 9, 23, 9), "all", "09:00")
        self.assertEqual(result["legs"][0]["train"], "G0")
        self.assertEqual(result["transfers"], 0)
        self.assertTrue(any(item["transfers"] == 2 and item["scoreParts"]["excessTransfers"] == 1
                            for item in result["alternatives"]))

    def test_route_with_only_many_transfers_is_still_returned(self):
        graph = fixture({"G1": [("A1", 540), ("B1", 600)],
                         "G2": [("B1", 620), ("D1", 680)],
                         "G3": [("D1", 700), ("E1", 760)],
                         "G4": [("E1", 780), ("C1", 840)]})
        for name in ("D1", "E1"):
            graph.names[name] = name
            graph.stop_names[name] = name
            graph.city_of[name] = name[0]
            graph.city_stations[name[0]] = {name}
            graph.city_core[name[0]] = {name}
        result = graph.find(["A", "C"], datetime(2026, 9, 23, 9), "all", "09:00")
        self.assertEqual(result["transfers"], 3)
        self.assertEqual(result["scoreParts"]["excessTransfers"], 2)

    def test_waypoint_prefers_core_station_and_same_train_is_one_leg(self):
        graph = fixture({"G1": [("A1", 540), ("B2", 600), ("C1", 700)],
                         "G2": [("A1", 540), ("B1", 620), ("C1", 750)]})
        graph.city_core["B"] = {"B1"}
        result = graph.find(["A", "B", "C"], datetime(2026, 9, 23, 9), "all", "09:00")
        self.assertEqual(result["stationScope"], "core")
        self.assertEqual(len(result["legs"]), 1)
        self.assertEqual(result["legs"][0]["train"], "G2")
        self.assertEqual(result["legs"][0]["via"][0]["station"], "B1")
        self.assertEqual(result["transfers"], 0)

    def test_waypoint_departure_uses_core_station(self):
        graph = fixture({"G1": [("A1", 540), ("B1", 600)],
                         "G2": [("B2", 660), ("C1", 700)],
                         "G3": [("B1", 680), ("C1", 760)]})
        graph.city_core["B"] = {"B1"}
        result = graph.find(["A", "B", "C"], datetime(2026, 9, 23, 9), "all", "09:00")
        self.assertEqual(result["stationScope"], "core")
        self.assertEqual(result["legs"][1]["from"], "B1")

    def test_full_city_fallback_when_core_has_no_route(self):
        graph = fixture({"G1": [("A1", 540), ("B2", 600), ("C1", 700)]})
        graph.city_core["B"] = {"B1"}
        result = graph.find(["A", "B", "C"], datetime(2026, 9, 23, 9), "all", "09:00")
        self.assertEqual(result["stationScope"], "all")


if __name__ == "__main__":
    unittest.main()
