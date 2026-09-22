import unittest
from datetime import date, datetime, timedelta
from unittest.mock import patch

import ticket_server


class FakePage:
    def goto(self, url, **kwargs):
        self.url = url

    def locator(self, selector):
        return self

    @property
    def first(self):
        return self

    def wait_for(self, **kwargs):
        return None


class FakeBrowser:
    def __init__(self):
        self.page_instance = FakePage()

    def new_page(self, **kwargs):
        return self.page_instance

    def close(self):
        return None


class FakePlaywright:
    def __enter__(self):
        self.chromium = self
        return self

    def __exit__(self, *args):
        return None

    def launch(self, **kwargs):
        return FakeBrowser()


def fake_rows(page, *_args, **_kwargs):
    departure, arrival, _ = page.url.split("|")
    if (departure, arrival) not in {("A", "C"), ("C", "B"), ("B", "A")}:
        return []
    return [{"train": "G101", "from": departure, "to": arrival,
             "depart": "09:00", "arrive": "10:00", "duration": "01:00",
             "seats": {"second": {"status": "有", "price": "20"}}}]


class RoutePlannerTest(unittest.TestCase):
    def test_leg_times_and_station_frequency(self):
        day = date.today().isoformat()
        def choice(train, origin, destination, depart_hour):
            departure = datetime.fromisoformat(f"{day}T{depart_hour:02d}:00")
            return ({"train": train, "from": origin, "to": destination}, None,
                    departure, departure + timedelta(hours=1))

        choices = [choice("G1", "A-East", "B-West", 9),
                   choice("G2", "A-East", "B-West", 9),
                   choice("G3", "A-South", "B", 9),
                   choice("G4", "A-South", "B", 16)]
        morning = ticket_server.rank_leg_choices(choices, "09:00")
        evening = ticket_server.rank_leg_choices(choices, "16:00")

        self.assertIn(morning[0][0][0]["train"], ("G1", "G2"))
        self.assertEqual(morning[0][2], 2)
        self.assertEqual(evening[0][0][0]["train"], "G4")

    def test_price_does_not_outweigh_duration(self):
        departure = datetime.fromisoformat(f"{date.today().isoformat()}T09:00")
        choices = [
            ({"train": "G-SLOW", "from": "A", "to": "B"}, 20, departure, departure + timedelta(hours=3)),
            ({"train": "G-FAST", "from": "A", "to": "B"}, 200, departure, departure + timedelta(hours=1)),
        ]
        ranked = ticket_server.rank_leg_choices(choices, "09:00")
        self.assertEqual(ranked[0][0][0]["train"], "G-FAST")

    def test_reorders_cities_when_input_order_has_no_train(self):
        with patch.object(ticket_server, "sync_playwright", return_value=FakePlaywright()), \
             patch.object(ticket_server, "resolve_location", side_effect=lambda name: {
                 "query": name, "stations": frozenset((name,)), "isCity": True,
             }), \
             patch.object(ticket_server, "CITY_STATIONS", {name: (name,) for name in "ABC"}), \
             patch.object(ticket_server, "station_codes", return_value={name: name for name in "ABC"}), \
             patch.object(ticket_server, "official_url", side_effect=lambda a, b, day, _: f"{a}|{b}|{day}"), \
             patch.object(ticket_server, "read_visible_rows", side_effect=fake_rows):
            result = ticket_server.plan_route("A", ["B", "C"], date.today().isoformat(), 1, "second", ["09:00", "09:00", "09:00"])

        self.assertTrue(result["complete"])
        self.assertEqual([(leg["from"], leg["to"]) for leg in result["legs"]], [("A", "C"), ("C", "B"), ("B", "A")])
        self.assertEqual(result["checkedPairs"], 4)
        self.assertEqual(result["totalFare"], 60)

    def test_missing_return_train_rejects_route(self):
        def outbound_only(page, *args, **kwargs):
            if page.url.startswith("B|A|"):
                return []
            return fake_rows(page, *args, **kwargs)

        with patch.object(ticket_server, "sync_playwright", return_value=FakePlaywright()), \
             patch.object(ticket_server, "resolve_location", side_effect=lambda name: {
                 "query": name, "stations": frozenset((name,)), "isCity": True,
             }), \
             patch.object(ticket_server, "CITY_STATIONS", {name: (name,) for name in "ABC"}), \
             patch.object(ticket_server, "station_codes", return_value={name: name for name in "ABC"}), \
             patch.object(ticket_server, "official_url", side_effect=lambda a, b, day, _: f"{a}|{b}|{day}"), \
             patch.object(ticket_server, "read_visible_rows", side_effect=outbound_only):
            result = ticket_server.plan_route("A", ["B", "C"], date.today().isoformat(), 1, "second", ["09:00", "09:00", "09:00"])

        self.assertFalse(result["complete"])
        self.assertEqual(result["legs"], [])


if __name__ == "__main__":
    unittest.main()
