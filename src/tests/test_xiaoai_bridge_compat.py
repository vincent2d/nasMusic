import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock


_PATH = Path(__file__).resolve().parents[2] / "tools/xiaoai_bridge/playback_compat.py"
_SPEC = importlib.util.spec_from_file_location("xiaoai_playback_compat", _PATH)
compat = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(compat)


def status(state=1, position=12000, duration=100000):
    return {"code": 0, "data": {"code": 0, "info": json.dumps({
        "status": state, "play_song_detail": {"position": position, "duration": duration},
    })}}


class XiaoaiPayloadTests(unittest.TestCase):
    def test_old_mina_envelope_and_new_flat_response_have_the_same_state(self):
        flat = {"status": 1, "play_song_detail": {"position": 12000, "duration": 100000}}
        self.assertEqual(compat.mina_payload(status()), flat)
        self.assertEqual(compat.mina_payload(flat), flat)

    def test_outer_inner_and_malformed_responses_do_not_become_stopped(self):
        for response in (None, {"code": -1}, {"code": 0, "data": {"code": -1}},
                         {"code": 0, "data": {"info": "not json"}}):
            with self.subTest(response=response), self.assertRaises(ValueError):
                compat.mina_payload(response)


class XiaoaiPlaybackTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.api = SimpleNamespace(player_get_status=mock.AsyncMock(return_value=status()),
                                   player_set_loop=mock.AsyncMock(return_value={"code": 0, "data": {"code": 0}}))
        self.renderer = SimpleNamespace(
            current_uri="http://192.168.1.2:53199/upnp/media/netease%3A123/song.mp3",
            _fnaudio_source_host="192.168.1.2", _media_generation=1,
            _lock=asyncio.Lock(), _user_stopped=False, _play_grace_until=0,
            _accumulated_time=80.0, _play_start_time=1.0, _track_duration=100.0,
            transport_state="PLAYING", transport_status="OK",
            _get_elapsed_time=mock.Mock(return_value=80.0), notify_state_change=mock.AsyncMock(),
            speaker=SimpleNamespace(auth=SimpleNamespace(ensure_login=mock.AsyncMock(), mina_service=self.api), device_id="speaker"),
        )
        self.app = SimpleNamespace(_fnaudio_original_poll=mock.AsyncMock())

    async def test_real_position_replaces_wall_clock_and_retains_seek_offset(self):
        self.renderer._fnaudio_source_offset = 30.0
        await compat.poll_renderer(self.app, "renderer", self.renderer)
        self.assertEqual(self.renderer._accumulated_time, 42)
        self.assertEqual(self.renderer._track_duration, 130)
        self.assertEqual(self.renderer.transport_state, "PLAYING")

    async def test_natural_end_with_zero_reset_position_keeps_last_position_and_stops_once(self):
        self.api.player_get_status.return_value = status(0, 0)
        self.renderer._get_elapsed_time.return_value = 100
        await compat.poll_renderer(self.app, "renderer", self.renderer)
        self.assertEqual(self.renderer.transport_state, "STOPPED")
        self.assertEqual(self.renderer.transport_status, "OK")
        self.assertEqual(self.renderer._accumulated_time, 100)

    async def test_early_stop_is_confirmed_then_reported_as_failure(self):
        self.api.player_get_status.return_value = status(0, 20000)
        await compat.poll_renderer(self.app, "renderer", self.renderer)
        self.assertEqual(self.renderer.transport_state, "TRANSITIONING")
        await compat.poll_renderer(self.app, "renderer", self.renderer)
        self.assertEqual(self.renderer.transport_state, "STOPPED")
        self.assertEqual(self.renderer.transport_status, "ERROR_OCCURRED")
        self.assertEqual(self.renderer._accumulated_time, 20)

    async def test_stalled_speaker_position_freezes_instead_of_advancing_clock_forever(self):
        with mock.patch.object(compat.time, "monotonic", return_value=100):
            await compat.poll_renderer(self.app, "renderer", self.renderer)
        with mock.patch.object(compat.time, "monotonic", return_value=115):
            await compat.poll_renderer(self.app, "renderer", self.renderer)
        self.assertEqual(self.renderer.transport_status, "ERROR_OCCURRED")
        self.assertEqual(self.renderer._play_start_time, 0)
        self.assertEqual(self.renderer._accumulated_time, 12)

    async def test_failed_status_read_freezes_and_a_later_real_sample_recovers(self):
        self.api.player_get_status.side_effect = OSError("offline")
        await compat.poll_renderer(self.app, "renderer", self.renderer)
        self.assertEqual(self.renderer.transport_status, "ERROR_OCCURRED")
        self.assertEqual(self.renderer._play_start_time, 0)
        self.api.player_get_status.side_effect = None
        await compat.poll_renderer(self.app, "renderer", self.renderer)
        self.assertEqual(self.renderer.transport_state, "PLAYING")
        self.assertEqual(self.renderer.transport_status, "OK")

    async def test_old_status_cannot_change_a_new_source_or_explicit_stop(self):
        async def replaced(_device):
            self.renderer._media_generation = 2
            return status(0)
        self.api.player_get_status.side_effect = replaced
        await compat.poll_renderer(self.app, "renderer", self.renderer)
        self.renderer.notify_state_change.assert_not_awaited()
        self.assertEqual(self.renderer.transport_state, "PLAYING")
        self.renderer._user_stopped = True
        self.api.player_get_status.side_effect = None
        await compat.poll_renderer(self.app, "renderer", self.renderer)
        self.renderer.notify_state_change.assert_not_awaited()

    async def test_unrelated_sources_keep_the_original_bridge_poller(self):
        self.renderer.current_uri = "https://example.com/music.mp3"
        await compat.poll_renderer(self.app, "renderer", self.renderer)
        self.app._fnaudio_original_poll.assert_awaited_once_with(self.app, "renderer", self.renderer)
        self.api.player_get_status.assert_not_awaited()

    async def test_speaker_single_play_mode_and_stale_generation_guard(self):
        await compat._play_once(self.renderer, 0)
        self.api.player_set_loop.assert_not_awaited()
        await compat._play_once(self.renderer, 1)
        self.api.player_set_loop.assert_awaited_once_with("speaker", 2)

    async def test_installed_hooks_preserve_other_controllers_and_disable_fn_audio_timer_end(self):
        del self.renderer._fnaudio_source_host
        original_check = mock.AsyncMock()
        self.renderer._check_play_status = original_check
        self.renderer.seek_url_func = mock.AsyncMock(return_value="http://bridge/media/seek")
        original_play = mock.AsyncMock(return_value=True)
        self.renderer.speaker.play_url = original_play
        self.renderer.speaker.get_volume = mock.AsyncMock(return_value=50)
        module = SimpleNamespace(Application=type("Application", (), {}), _poll_one_renderer=mock.AsyncMock())
        with mock.patch.object(compat.importlib, "import_module", return_value=module):
            compat.configure_renderer(self.renderer, "192.168.1.2")
            compat.configure_renderer(self.renderer, "192.168.1.2")
        self.assertIs(module._poll_one_renderer, compat.poll_renderer)
        await self.renderer._check_play_status()
        original_check.assert_not_awaited()
        self.assertTrue(await self.renderer.speaker.play_url("http://bridge/media/token"))
        await asyncio.sleep(0)
        self.api.player_set_loop.assert_awaited_once_with("speaker", 2)
        await self.renderer.seek_url_func(self.renderer.current_uri, 30, 100, "renderer")
        await self.renderer.speaker.play_url("http://bridge/media/seek")
        await asyncio.sleep(0)
        self.assertEqual(self.renderer._fnaudio_source_offset, 30)
        response = status()
        response["data"]["info"] = json.dumps({"status": 1, "volume": 20})
        self.api.player_get_status.return_value = response
        self.assertEqual(await self.renderer.speaker.get_volume(), 20)
        self.renderer.current_uri = "https://example.com/song.mp3"
        await self.renderer._check_play_status()
        original_check.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
