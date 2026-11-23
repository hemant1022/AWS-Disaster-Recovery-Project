"""Tests for the parsing helpers that fail silently when they break.

Run with:  python -m unittest discover -s scripts -p "test_*.py"

The first real drill run was defeated by a parser that looked for
newState.state while CloudWatch emits newState.stateValue. The alarm fired on
time; the harness never saw it. These tests pin the shape.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from drlib import alarm_state_from_history_item  # noqa: E402


class AlarmHistoryParsingTests(unittest.TestCase):
    def test_current_state_value_shape(self) -> None:
        item = {"HistoryData": json.dumps({"newState": {"stateValue": "ALARM"}})}
        self.assertEqual(alarm_state_from_history_item(item), "ALARM")

    def test_legacy_state_shape(self) -> None:
        item = {"HistoryData": json.dumps({"newState": {"state": "OK"}})}
        self.assertEqual(alarm_state_from_history_item(item), "OK")

    def test_missing_new_state(self) -> None:
        item = {"HistoryData": json.dumps({"oldState": {"stateValue": "OK"}})}
        self.assertIsNone(alarm_state_from_history_item(item))

    def test_invalid_json_is_not_fatal(self) -> None:
        self.assertIsNone(alarm_state_from_history_item({"HistoryData": "not json"}))

    def test_missing_history_data(self) -> None:
        self.assertIsNone(alarm_state_from_history_item({}))


if __name__ == "__main__":
    unittest.main()
