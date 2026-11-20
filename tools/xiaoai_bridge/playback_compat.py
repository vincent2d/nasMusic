"""fnAudio playback compatibility for the installed MiAirX bridge.

Mounted into MiAirX and installed by its existing DLNA HTTP compatibility
module. Other controllers retain MiAirX's original playback behavior.
"""

import asyncio
import importlib
import json
import logging
import math
import time
from urllib.parse import urlsplit

log = logging.getLogger(__name__)


def mina_payload(response):
    """Decode both flat MiNA results and older data.info JSON envelopes."""
    for _ in range(4):
        if not isinstance(response, dict):
            raise ValueError("Invalid Xiaomi playback response")
        if "code" in response and response["code"] != 0:
            raise ValueError("Xiaomi playback request failed (code=%s)" % response["code"])
        if "info" in response:
            response = response["info"]
            if isinstance(response, str):
                response = json.loads(response)
        elif isinstance(response.get("data"), dict):
            response = response["data"]
        else:
            return response
    raise ValueError("Invalid Xiaomi playback response envelope")


def managed_source(renderer):
    try:
        url = urlsplit(renderer.current_uri)
        return (url.hostname == renderer._fnaudio_source_host and url.port == 53199
                and url.path.startswith("/upnp/media/"))
    except (AttributeError, ValueError):
        return False


def _seconds(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value / 1000 if math.isfinite(value) and 0 <= value <= 86400000 else None


async def _read_status(speaker):
    await speaker.auth.ensure_login()
    return mina_payload(await speaker.auth.mina_service.player_get_status(speaker.device_id))


async def _play_once(renderer, generation):
    try:
        if generation != renderer._media_generation or not managed_source(renderer):
            return
        speaker = renderer.speaker
        # Xiaomi mode 2 plays this one-item source once. fnAudio owns repeat
        # and queue advancement; a one-item Xiaomi loop repeats the old audio.
        result = await speaker.auth.mina_service.player_set_loop(speaker.device_id, 2)
        mina_payload(result)
    except Exception as exc:
        log.warning("fnAudio single-play mode failed: %s", exc)


async def poll_renderer(app, udn, renderer):
    if not managed_source(renderer):
        return await app._fnaudio_original_poll(app, udn, renderer)
    generation, uri = renderer._media_generation, renderer.current_uri
    started = renderer._play_start_time
    try:
        status = await asyncio.wait_for(_read_status(renderer.speaker), timeout=10)
        state = status.get("status")
        if isinstance(state, bool) or state not in (0, 1, 2):
            raise ValueError("Xiaomi playback state is missing")
        failure = None
    except Exception as exc:
        status, state, failure = {}, None, str(exc)

    async with renderer._lock:
        if (generation != renderer._media_generation or uri != renderer.current_uri
                or started != renderer._play_start_time):
            return
        # An in-flight cloud read must never undo an explicit pause/stop or
        # the asynchronous Play handshake for a newly selected source.
        if renderer._user_stopped or (renderer.transport_state == "TRANSITIONING"
                                     and not getattr(renderer, "_fnaudio_stops", 0)):
            return
        if time.time() < renderer._play_grace_until:
            return

        if failure is not None:
            renderer._accumulated_time = renderer._get_elapsed_time()
            renderer._play_start_time = 0.0
            renderer.transport_state = "STOPPED"
            renderer.transport_status = "ERROR_OCCURRED"
            log.warning("fnAudio speaker status unavailable: %s", failure)
        else:
            detail = status.get("play_song_detail") or {}
            position = _seconds(detail.get("position")) if isinstance(detail, dict) else None
            duration = _seconds(detail.get("duration")) if isinstance(detail, dict) else None
            offset = getattr(renderer, "_fnaudio_source_offset", 0.0)
            elapsed = renderer._get_elapsed_time()
            if duration is not None and duration > 0:
                renderer._track_duration = offset + duration
            if position is not None and (state == 1 or position > 0):
                elapsed = offset + position
            renderer._accumulated_time = elapsed
            renderer._play_start_time = time.time() if state == 1 else 0.0
            renderer.transport_status = "OK"
            if state == 1:
                renderer.transport_state = "PLAYING"
                renderer._fnaudio_stops = 0
                last = getattr(renderer, "_fnaudio_last_progress", None)
                now = time.monotonic()
                if position is None:
                    renderer.transport_state = "STOPPED"
                    renderer.transport_status = "ERROR_OCCURRED"
                    renderer._play_start_time = 0.0
                elif last is None or position != last[0]:
                    renderer._fnaudio_last_progress = (position, now)
                elif now - last[1] >= 15:
                    renderer.transport_state = "STOPPED"
                    renderer.transport_status = "ERROR_OCCURRED"
                    renderer._play_start_time = 0.0
                    log.warning("fnAudio speaker position stalled at %.1f seconds", elapsed)
            elif state == 2:
                renderer.transport_state = "PAUSED_PLAYBACK"
                renderer._fnaudio_stops = 0
            else:
                renderer._fnaudio_stops = getattr(renderer, "_fnaudio_stops", 0) + 1
                near_end = renderer._track_duration > 0 and elapsed >= renderer._track_duration - 2
                if renderer._fnaudio_stops < 2 and not near_end:
                    # Confirm a stopped sample on the next five-second poll.
                    # Freeze position while waiting instead of simulating play.
                    renderer.transport_state = "TRANSITIONING"
                else:
                    renderer.transport_state = "STOPPED"
                    if not renderer._track_duration or elapsed < renderer._track_duration - 6:
                        renderer.transport_status = "ERROR_OCCURRED"
                        log.warning("fnAudio source stopped before completion (%.1f/%.1f)",
                                    elapsed, renderer._track_duration)
    await renderer.notify_state_change()


def configure_renderer(renderer, hostname):
    """Install the compatibility hooks once for each registered renderer."""
    if hasattr(renderer, "_fnaudio_source_host"):
        return
    renderer._fnaudio_source_host = hostname
    original_check = renderer._check_play_status
    original_play = renderer.speaker.play_url
    original_seek_url = getattr(renderer, "seek_url_func", None)
    original_volume = getattr(renderer.speaker, "get_volume", None)

    async def get_volume():
        if not managed_source(renderer):
            return await original_volume()
        status = await _read_status(renderer.speaker)
        volume = status.get("volume")
        if isinstance(volume, bool) or not isinstance(volume, int) or not 0 <= volume <= 100:
            raise ValueError("Xiaomi volume is missing")
        renderer.speaker._last_volume = volume
        return volume

    async def seek_url(original_url, seconds, duration, udn=""):
        generation = renderer._media_generation
        result = await original_seek_url(original_url, seconds, duration, udn)
        if result and generation == renderer._media_generation and original_url == renderer.current_uri:
            renderer._fnaudio_pending_offset = seconds
        return result

    async def check_play_status():
        if not managed_source(renderer):
            await original_check()
        # Managed fnAudio sources finish on confirmed speaker state, never on
        # elapsed wall time followed by MiAirX's ten-second NextURI wait.

    async def play_url(url):
        managed = managed_source(renderer)
        generation = renderer._media_generation
        if managed:
            pending = getattr(renderer, "_fnaudio_pending_offset", None)
            renderer._fnaudio_source_offset = pending if pending is not None else renderer._accumulated_time
            renderer._fnaudio_pending_offset = None
            renderer._fnaudio_stops = 0
            renderer._fnaudio_last_progress = None
        success = await original_play(url)
        if success and managed and generation == renderer._media_generation:
            asyncio.create_task(_play_once(renderer, generation))
        return success

    renderer._check_play_status = check_play_status
    renderer.speaker.play_url = play_url
    if original_seek_url is not None:
        renderer.seek_url_func = seek_url
    if original_volume is not None:
        renderer.speaker.get_volume = get_volume
    module = importlib.import_module("miairx.app")
    if not hasattr(module.Application, "_fnaudio_original_poll"):
        # Application instances use the original unbound function for sources
        # outside fnAudio; the installed global poller dispatches per renderer.
        module.Application._fnaudio_original_poll = staticmethod(module._poll_one_renderer)
        module._poll_one_renderer = poll_renderer
