"""Regression checks for manual channel choice and watch-loop recovery."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from twitch import Twitch


class ReliabilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_switch_wakes_a_waiting_watch_loop(self):
        miner = Twitch.__new__(Twitch)
        miner._watching_restart = asyncio.Event()
        waiter = asyncio.create_task(miner._watch_sleep(60))
        await asyncio.sleep(0)
        miner._watching_restart.set()
        await asyncio.wait_for(waiter, 1)

    async def test_failed_watch_never_estimates_progress(self):
        miner = Twitch.__new__(Twitch)
        channel = SimpleNamespace(name="streamer", online=True, send_watch=AsyncMock(return_value=False))
        miner.watching_channel = SimpleNamespace(get=AsyncMock(side_effect=[channel, asyncio.CancelledError()]))
        miner._watching_restart = asyncio.Event()
        miner._watch_sleep = AsyncMock()
        miner.gui = SimpleNamespace(progress=SimpleNamespace(minute_almost_done=Mock()))
        miner.gql_request = AsyncMock()
        miner.get_active_campaign = Mock()
        with self.assertRaises(asyncio.CancelledError):
            await miner._watch_loop()
        miner.gui.progress.minute_almost_done.assert_not_called()
        miner.gql_request.assert_not_called()
        miner.get_active_campaign.assert_not_called()

    def test_manual_choice_stays_until_ineligible(self):
        miner = Twitch.__new__(Twitch)
        selected = SimpleNamespace(name="chosen")
        alternative = SimpleNamespace(name="other")
        miner.gui = SimpleNamespace(channels=SimpleNamespace(get_selection=lambda: selected))
        miner.watching_channel = SimpleNamespace(get_with_default=lambda default: selected)
        miner.can_watch = lambda channel: True
        miner.get_priority = lambda channel: 0 if channel is alternative else 1
        self.assertFalse(miner.should_switch(alternative))
        miner.can_watch = lambda channel: channel is alternative
        self.assertTrue(miner.should_switch(alternative))


if __name__ == "__main__":
    unittest.main()
