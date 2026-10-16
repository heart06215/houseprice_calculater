"""Per-city maxima, tied months and incorrect publication labels."""

import copy
import unittest

from release_contract import validate_city_extrema


class CityPeakTests(unittest.TestCase):
    def city(self):
        return dict(
            city="甲城",
            current_price=80,
            max_price=200,
            min_price=80,
            peak_date="2022-03",
            peak_year=2022,
            drop_from_peak=-60.0,
            monthly=[
                dict(date="2023-01", price=80),
                dict(date="2021-01", price=100),
                dict(date="2022-03", price=200),
            ],
        )

    def test_peak_uses_own_city_and_month(self):
        first = self.city()
        second = dict(
            city="乙城",
            current_price=100,
            max_price=400,
            min_price=100,
            peak_date="2020-02",
            peak_year=2020,
            drop_from_peak=-75.0,
            monthly=[dict(date="2020-02", price=400), dict(date="2023-01", price=100)],
        )
        self.assertEqual(validate_city_extrema(first)["date"], "2022-03")
        self.assertEqual(validate_city_extrema(second)["price"], 400)
        first.update(
            max_price=second["max_price"], peak_date=second["peak_date"], peak_year=2020
        )
        with self.assertRaisesRegex(ValueError, "甲城: extrema mismatch"):
            validate_city_extrema(first)

    def test_same_price_in_two_cities_is_allowed(self):
        first = self.city()
        second = copy.deepcopy(first)
        second["city"] = "乙城"
        second["monthly"][2]["date"] = "2020-03"
        second.update(peak_date="2020-03", peak_year=2020)
        self.assertEqual(
            validate_city_extrema(first)["price"],
            validate_city_extrema(second)["price"],
        )
        self.assertNotEqual(
            validate_city_extrema(first)["date"], validate_city_extrema(second)["date"]
        )

    def test_ties_choose_first_month_and_keep_all_occurrences(self):
        city = self.city()
        city["monthly"].append(dict(date="2022-04", price=200))
        self.assertEqual(validate_city_extrema(city)["dates"], ["2022-03", "2022-04"])
        city["peak_date"] = "2022-04"
        with self.assertRaisesRegex(ValueError, "peak date mismatch"):
            validate_city_extrema(city)

    def test_wrong_date_year_and_drawdown_are_rejected(self):
        for field, bad in [
            ("peak_date", "2021-01"),
            ("peak_year", 2021),
            ("drop_from_peak", -20),
        ]:
            with self.subTest(field=field):
                city = self.city()
                city[field] = bad
                with self.assertRaises(ValueError):
                    validate_city_extrema(city)

    def test_missing_prices_have_no_peak(self):
        city = dict(
            city="空城",
            current_price=None,
            max_price=None,
            min_price=None,
            peak_date=None,
            peak_year=None,
            drop_from_peak=None,
            monthly=[dict(date="2022-01", price=None)],
        )
        self.assertEqual(validate_city_extrema(city)["dates"], [])
        city["peak_date"] = "2022-01"
        with self.assertRaises(ValueError):
            validate_city_extrema(city)


if __name__ == "__main__":
    unittest.main()
