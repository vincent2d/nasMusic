import unittest
from unittest import mock

from dlna import control_point
from playback.core import PlaybackCore
from playback.plugins import CallbackOutput, DlnaPushOutput
import server


class DlnaVolumeTests(unittest.TestCase):
    def test_volume_uses_rendering_control_and_master_channel(self):
        device = {"control_url": "http://speaker/transport", "rendering_url": "http://speaker/volume"}
        with mock.patch.object(control_point, "soap_request", return_value=b"<Response/>") as soap:
            self.assertEqual(control_point.upnp_control(device, "volume", 28), (True, "ok"))
        self.assertEqual(soap.call_args.args[:3],
                         (device["rendering_url"], "urn:schemas-upnp-org:service:RenderingControl:1", "SetVolume"))
        self.assertIn("<Channel>Master</Channel><DesiredVolume>28</DesiredVolume>", soap.call_args.args[3])

    def test_volume_rejects_missing_services_invalid_values_and_soap_faults(self):
        with mock.patch.object(control_point, "soap_request") as soap:
            self.assertFalse(control_point.upnp_control({}, "volume", 20)[0])
            for value in (None, True, -1, 101, float("nan"), float("inf")):
                self.assertFalse(control_point.upnp_control({"rendering_url": "http://speaker/volume"}, "volume", value)[0])
            soap.assert_not_called()
        fault = b'<Envelope><Fault><errorDescription>Volume refused</errorDescription></Fault></Envelope>'
        with mock.patch.object(control_point, "soap_request", return_value=fault):
            self.assertFalse(control_point.upnp_control({"rendering_url": "http://speaker/volume"}, "volume", 20)[0])

    def test_volume_read_is_bounded_and_failure_is_not_a_fake_value(self):
        for value, expected in (("28", 28), ("0", 0), ("101", None), ("bad", None)):
            with mock.patch.object(control_point, "soap_request", return_value=f'<CurrentVolume>{value}</CurrentVolume>'.encode()):
                self.assertEqual(control_point.upnp_volume({"rendering_url": "http://speaker/volume"}), expected)
        self.assertIsNone(control_point.upnp_volume({}))

    def test_volume_request_cannot_change_a_different_selected_device(self):
        output = DlnaPushOutput(on_command=mock.Mock(return_value={"ok": True}))
        core = PlaybackCore()
        core.registry.register(output)
        core.set_target("speaker-a", "dlna_push")
        with mock.patch.object(server, "PLAYBACK_CORE", core):
            _, status = server.playback_control(mock.Mock(), {"target_id": "speaker-b", "action": "volume", "position": 28})
        self.assertEqual(status, 409)
        output._on_command.assert_not_called()

    def test_volume_api_forwards_valid_values_and_rejects_invalid_values(self):
        callback = mock.Mock(return_value={"ok": True})
        core = PlaybackCore()
        core.registry.register(DlnaPushOutput(on_command=callback))
        core.set_target("speaker", "dlna_push")
        with mock.patch.object(server, "PLAYBACK_CORE", core):
            for value in (True, None, -1, 101, float("nan")):
                _, status = server.playback_control(mock.Mock(), {"target_id":"speaker", "action":"volume", "position":value})
                self.assertEqual(status, 400)
            callback.assert_not_called()
            result, status = server.playback_control(mock.Mock(), {"target_id":"speaker", "action":"volume", "position":28})
        self.assertEqual(status, 200)
        self.assertTrue(result["ok"])
        self.assertEqual(callback.call_args.args[0].position, 28)

    def test_progress_polls_do_not_query_device_volume(self):
        with mock.patch.object(server, "find_upnp_device", return_value={"id":"speaker"}), mock.patch.object(
            server, "upnp_transport_state", return_value={"state":"PLAYING"}
        ), mock.patch.object(server, "upnp_position_info", return_value={}), mock.patch.object(
            server, "upnp_volume", return_value=28
        ) as volume:
            server.playback_status({"target_id":["speaker"]})
            volume.assert_not_called()
            response, status = server.playback_status({"target_id":["speaker"],"volume":["1"]})
        self.assertEqual(status, 200)
        self.assertEqual(response["volume"], 28)
        volume.assert_called_once()

    def test_mpd_volume_keeps_the_existing_bare_target_alias(self):
        for selected, requested in (("mpd", "mpd:usb"), ("mpd:usb", "mpd")):
            core = PlaybackCore()
            callback = mock.Mock(return_value={"ok":True})
            core.registry.register(CallbackOutput("mpd", "MPD", "mpd", {"volume":True}, on_command=callback))
            core.set_target(selected, "mpd")
            with mock.patch.object(server, "PLAYBACK_CORE", core):
                _, status = server.playback_control(mock.Mock(), {"target_id":requested,"action":"volume","position":28})
            self.assertEqual(status, 200)
            callback.assert_called_once()


if __name__ == "__main__":
    unittest.main()
