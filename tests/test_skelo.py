#!/usr/bin/env python3
"""
Unit and integration tests for Skelo-Linux toolkit.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import skelo_common
from skelo_common import (
    INVALID_COORD_THRESHOLD, get_bounds, is_meaningful_element,
    _norm_win_id, classify_region
)
from list_windows import _clean_x11_app_name, _window_matches
from skelo_action import _is_ascii_simple
from skelo import Desktop, Window, Element


class TestSkeloCommon(unittest.TestCase):
    def test_invalid_coord_sentinel(self):
        self.assertLess(INVALID_COORD_THRESHOLD, 0)
        self.assertEqual(INVALID_COORD_THRESHOLD, -1_000_000)

    def test_is_meaningful_element_rejects_sentinel_coords(self):
        sentinel_bounds = {"x": -2147483648, "y": -2147483648, "width": 1, "height": 1}
        meaningful = is_meaningful_element(
            elem_type="button", role="push button", label="Hidden Button",
            value=None, bounds=sentinel_bounds, clickable=True,
            focused=False, selected=False
        )
        self.assertFalse(meaningful, "Sentinel coordinates should never be marked meaningful")

    def test_is_meaningful_element_accepts_valid_bounds(self):
        valid_bounds = {"x": 100, "y": 200, "width": 80, "height": 30}
        meaningful = is_meaningful_element(
            elem_type="button", role="push button", label="OK",
            value=None, bounds=valid_bounds, clickable=True,
            focused=False, selected=False
        )
        self.assertTrue(meaningful)

    def test_norm_win_id(self):
        self.assertEqual(_norm_win_id("0x02800003"), _norm_win_id("0x2800003"))
        self.assertEqual(_norm_win_id("0x10"), 16)
        self.assertEqual(_norm_win_id("16"), 16)
        self.assertIsNone(_norm_win_id("invalid"))

    def test_classify_region(self):
        win_bounds = {"x": 0, "y": 0, "width": 900, "height": 900}
        top_left = {"x": 50, "y": 50, "width": 100, "height": 100}
        center = {"x": 400, "y": 400, "width": 100, "height": 100}
        bottom_right = {"x": 750, "y": 750, "width": 100, "height": 100}

        self.assertEqual(classify_region(top_left, win_bounds), "top-left")
        self.assertEqual(classify_region(center, win_bounds), "center")
        self.assertEqual(classify_region(bottom_right, win_bounds), "bottom-right")


class TestListWindows(unittest.TestCase):
    def test_clean_x11_app_name(self):
        self.assertEqual(_clean_x11_app_name("brave-browser"), "Brave Browser")
        self.assertEqual(_clean_x11_app_name("Brave-browser"), "Brave Browser")
        self.assertEqual(_clean_x11_app_name("google-chrome"), "Google Chrome")
        self.assertEqual(_clean_x11_app_name("spotify"), "Spotify")
        self.assertEqual(_clean_x11_app_name("vscodium"), "VSCodium")
        self.assertEqual(_clean_x11_app_name("codium"), "VSCodium")

    def test_window_matches(self):
        w_atspi = {
            "app_name": "Brave Browser",
            "process_id": 4919,
            "window_title": "SKELO | Shrikirti Akki - Brave",
            "bounds": {"x": 29, "y": 126, "width": 1709, "height": 986}
        }
        w_x11_flatpak = {
            "app_name": "Brave Browser",
            "process_id": 2, # flatpak namespace PID
            "window_title": "SKELO | Shrikirti Akki - Brave",
            "bounds": {"x": 29, "y": 126, "width": 1709, "height": 986}
        }
        self.assertTrue(_window_matches(w_atspi, w_x11_flatpak), "Should match identical title and bounds despite PID difference")


class TestSkeloAction(unittest.TestCase):
    def test_ascii_simple_detection(self):
        self.assertTrue(_is_ascii_simple("hello world 123"))
        self.assertTrue(_is_ascii_simple("https://github.com"))
        self.assertFalse(_is_ascii_simple("multiline\ntext"))
        self.assertFalse(_is_ascii_simple("unicode: café, résumé, 日本語, 🚀"))


class TestDesktopSDK(unittest.TestCase):
    def test_desktop_instantiation(self):
        d = Desktop()
        self.assertIsNotNone(d)
        res = d.resolution
        self.assertIsInstance(res, dict)
        self.assertIn("width", res)
        self.assertIn("height", res)

    def test_list_windows_sdk(self):
        d = Desktop()
        wins = d.list_windows()
        self.assertIsInstance(wins, list)
        if wins:
            self.assertIsInstance(wins[0], Window)
            self.assertTrue(hasattr(wins[0], "click"))
            self.assertTrue(hasattr(wins[0], "type"))
            self.assertTrue(hasattr(wins[0], "inspect"))


if __name__ == "__main__":
    unittest.main()
