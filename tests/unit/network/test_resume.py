# SPDX-License-Identifier: AGPL-3.0-or-later
# pylint: disable=missing-module-docstring,missing-class-docstring,protected-access

import time

from unittest.mock import patch

from searx.network import client
from tests import SearxTestCase


class TestResumeWatchdog(SearxTestCase):
    def test_drift_is_zero_when_clocks_advance_together(self):
        # normal operation: wall and monotonic move in lock-step
        self.assertAlmostEqual(client._resume_drift(1000.0, 50.0, 1001.0, 51.0), 0.0)

    def test_drift_equals_suspend_duration_on_resume(self):
        # after a 600s suspend: wall jumped 601s (NTP), monotonic only the 1s tick
        self.assertAlmostEqual(client._resume_drift(1000.0, 50.0, 1601.0, 51.0), 600.0)

    def test_small_jumps_stay_below_threshold(self):
        # a sub-threshold NTP step or GC pause must not look like a resume
        drift = client._resume_drift(1000.0, 50.0, 1002.0, 51.0)
        self.assertLess(drift, client.RESUME_DRIFT_THRESHOLD)

    def test_real_suspend_exceeds_threshold(self):
        drift = client._resume_drift(1000.0, 50.0, 1601.0, 51.0)
        self.assertGreater(drift, client.RESUME_DRIFT_THRESHOLD)

    def test_reset_closes_all_pools(self):
        # the network loop is started in a daemon thread at import; wait for it
        for _ in range(100):
            if client.get_loop() is not None:
                break
            time.sleep(0.01)
        self.assertIsNotNone(client.get_loop())

        called = {}

        async def fake_aclose_all():
            called['hit'] = True

        with patch('searx.network.network.Network.aclose_all', side_effect=fake_aclose_all) as aclose_all:
            client.reset_networks_after_resume()
            aclose_all.assert_called_once()
        self.assertTrue(called.get('hit'))
