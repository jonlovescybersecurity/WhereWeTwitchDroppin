"""Regression checks for manual channel choice and watch-loop recovery."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from channel import Channel
from constants import ClientType
from exceptions import ExitRequest, MinerException, ReloadRequest
from exceptions import LoginException
from twitch import Twitch, _AuthState


class ReliabilityTests(unittest.IsolatedAsyncioTestCase):
    def test_miner_defaults_to_smart_tv_client(self):
        with patch("twitch.GUIManager"), patch("twitch.WebsocketPool"):
            miner = Twitch(SimpleNamespace())
        self.assertIs(miner._client_type, ClientType.SMARTBOX)

    async def test_device_login_uses_smart_tv_client(self):
        class Response:
            status = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def json(self):
                return {
                    "device_code": "device", "user_code": "ABC123", "interval": 1,
                    "verification_uri": "https://www.twitch.tv/activate", "expires_in": 1800,
                }

        class TokenResponse(Response):
            async def json(self):
                return {"access_token": "token"}

        calls = []

        def request(method, url, **kwargs):
            calls.append((method, url, kwargs))
            return Response() if url.endswith("/device") else TokenResponse()

        login_form = SimpleNamespace(ask_enter_code=AsyncMock())
        twitch = SimpleNamespace(_client_type=ClientType.SMARTBOX,
                                 gui=SimpleNamespace(login=login_form), request=request)
        auth = _AuthState(twitch)
        auth.device_id = "device-id"
        with patch("twitch.asyncio.sleep", new_callable=AsyncMock):
            self.assertEqual(await auth._oauth_login(), "token")
        self.assertEqual(calls[0][2]["data"]["client_id"], ClientType.SMARTBOX.CLIENT_ID)
        self.assertEqual(calls[1][2]["data"]["client_id"], ClientType.SMARTBOX.CLIENT_ID)
        login_form.ask_enter_code.assert_awaited_once()

    def test_channel_page_stays_on_public_web_client(self):
        channel = Channel.__new__(Channel)
        channel._login = "example"
        channel._twitch = SimpleNamespace(_client_type=ClientType.SMARTBOX)
        self.assertEqual(str(channel.url), "https://www.twitch.tv/example")

    async def test_device_login_reports_missing_code(self):
        class Response:
            status = 400

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def json(self):
                return {"error": "invalid client"}

        twitch = SimpleNamespace(_client_type=ClientType.SMARTBOX,
                                 gui=SimpleNamespace(login=Mock()), request=lambda *a, **kw: Response())
        auth = _AuthState(twitch)
        auth.device_id = "device-id"
        with self.assertRaisesRegex(LoginException, "rejected the device login"):
            await auth._oauth_login()

    async def test_missing_watch_endpoint_is_retryable(self):
        channel = Channel.__new__(Channel)
        channel._stream = SimpleNamespace(spade_payload={})
        channel._spade_url = None
        channel._twitch = SimpleNamespace(request=Mock())
        with patch.object(Channel, "get_spade_url", new_callable=AsyncMock) as endpoint:
            endpoint.side_effect = MinerException("URL missing")
            self.assertFalse(await channel.send_watch())
        self.assertIsNone(channel._spade_url)
        channel._twitch.request.assert_not_called()

    async def test_shutdown_and_reload_escape_watch_endpoint_handler(self):
        for signal in (ExitRequest, ReloadRequest):
            with self.subTest(signal=signal):
                channel = Channel.__new__(Channel)
                channel._stream = SimpleNamespace(spade_payload={})
                channel._spade_url = None
                with patch.object(Channel, "get_spade_url", new_callable=AsyncMock) as endpoint:
                    endpoint.side_effect = signal()
                    with self.assertRaises(signal):
                        await channel.send_watch()

    async def test_malformed_current_drop_does_not_end_watch_task(self):
        for payload in ({}, [], {"dropID": "known"},
                        {"dropID": "known", "currentMinutesWatched": "12"}):
            with self.subTest(payload=payload):
                miner = Twitch.__new__(Twitch)
                channel = SimpleNamespace(
                    name="streamer", online=True, id=123,
                    send_watch=AsyncMock(return_value=True),
                )
                miner.watching_channel = SimpleNamespace(
                    get=AsyncMock(side_effect=[channel, asyncio.CancelledError()]),
                    get_with_default=lambda default: channel,
                )
                miner._watching_restart = asyncio.Event()
                miner._watch_sleep = AsyncMock()
                miner.gui = SimpleNamespace(progress=SimpleNamespace(minute_almost_done=lambda: True))
                drop = SimpleNamespace(can_earn=lambda channel: True, update_minutes=Mock())
                miner._drops = {"known": drop}
                miner.gql_request = AsyncMock(return_value={
                    "data": {"currentUser": {"dropCurrentSession": payload}}
                })
                with self.assertRaises(asyncio.CancelledError):
                    await miner._watch_loop()
                drop.update_minutes.assert_not_called()

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
