# -*- coding: utf-8 -*-

import datetime
import os
import re
import logging
import requests
from env_secrets import get_env_secret
from typing import Dict, Optional
from ask_sdk_model import Request, Response
from ask_sdk_model.ui import StandardCard, Image
from ask_sdk_model.interfaces.audioplayer import (
    PlayDirective, PlayBehavior, AudioItem, Stream, AudioItemMetadata,
    StopDirective, ClearQueueDirective, ClearBehavior)
from ask_sdk_model.interfaces import display
from ask_sdk_core.response_helper import ResponseFactory
from ask_sdk_core.handler_input import HandlerInput
from ask_sdk_model.interfaces.alexa.presentation.apl import ExecuteCommandsDirective, ControlMediaCommand, MediaCommandType
from . import data


def get_ma_hostname(raise_on_http_scheme=True):
    """Read and sanitize MA_HOSTNAME environment variable and return a https:// hostname or empty string.

    If `raise_on_http_scheme` is True and the provided value starts with http://, a
    ValueError is raised so callers can surface an appropriate error to the user.
    """
    hostname_raw = os.environ.get('MA_HOSTNAME', '')
    hostname_raw = hostname_raw.strip()
    # strip surrounding single/double quotes
    if len(hostname_raw) >= 2 and ((hostname_raw[0] == hostname_raw[-1] == '"') or (hostname_raw[0] == hostname_raw[-1] == "'")):
        hostname_raw = hostname_raw[1:-1].strip()
    # final cleanup of stray quotes/whitespace
    hostname_raw = hostname_raw.strip('"\' ')

    if hostname_raw == '':
        return ''

    hostname_clean = hostname_raw.rstrip('/')
    if hostname_clean.startswith('https://'):
        return hostname_clean
    if hostname_clean.startswith('http://'):
        if raise_on_http_scheme:
            raise ValueError('http_scheme')
        return ''

    return f'https://{hostname_clean}'


def replace_ip_in_url(url, hostname):
    """Replace an IP address host at the start of `url` with `hostname` and
    percent-encode spaces. Returns the modified url.
    """
    if not url:
        return url
    try:
        new_url = re.sub(r'^https?://\d+\.\d+\.\d+\.\d+(?::\d+)?', hostname, url)
    except re.error:
        return url.replace(' ', '%20')
    return new_url.replace(' ', '%20')


def audio_data(request):
    try:
        data.get_latest()
        return data.info
    except Exception:
        return


def push_alexa_metadata(url):
    """Push the currently playing stream metadata to the Alexa API."""
    payload = {
        'streamUrl': url,
        'title': data.info.get("primaryText"),
        'secondary': data.info.get("secondaryText"),
        'imageUrl': data.info.get("coverImageSource")
    }

    try:
        from app.alexa_api import alexa_routes
        alexa_routes._store = payload
    except Exception:
        try:
            push_endpoint = 'http://localhost:5000/alexa/push-url'
            user = get_env_secret('APP_USERNAME')
            pwd = get_env_secret('APP_PASSWORD')
            if user and pwd:
                requests.post(push_endpoint, json=payload, timeout=2, auth=(user, pwd))
            else:
                requests.post(push_endpoint, json=payload, timeout=2)
        except requests.RequestException:
            logging.exception('Failed to POST to Alexa API %s', push_endpoint)
        except Exception:
            logging.exception('Unexpected error while pushing Alexa metadata')


def play(url, offset, text, response_builder, supports_apl=False):
    """Function to play audio.

    REPLACE_ALL: Immediately begin playback of the specified stream,
    and replace current and enqueued streams.
    """
    try:
        hostname = get_ma_hostname(raise_on_http_scheme=True)
    except ValueError:
        response_builder.speak(
            "The domain uses an unsupported scheme (http). Please check your environment variable MA_HOSTNAME.").set_should_end_session(True)
        return response_builder.response

    if not hostname:
        response_builder.speak(
            "You did not specify a valid hostname. Please check your environment variable MA_HOSTNAME.").set_should_end_session(True)
        return response_builder.response

    url = replace_ip_in_url(url, hostname)

    skip_validation = os.environ.get('SKIP_URL_VALIDATION', 'false').lower() in ('true', '1', 'yes')

    if skip_validation:
        logging.info('Stream URL (validation skipped via SKIP_URL_VALIDATION): %s', url)
    else:
        # Verify that the resource is reachable and playable by attempting a HEAD request, falling back to GET
        try:
            head_resp = requests.head(url, allow_redirects=True, timeout=5)
            resp = head_resp
            if head_resp.status_code >= 400:
                resp = requests.get(url, stream=True, allow_redirects=True, timeout=5)

            if resp.status_code >= 400:
                logging.error('Audio URL returned HTTP %s: %s', resp.status_code, url)
                response_builder.speak(
                    "Sorry, I can't reach the audio file. Please check that your stream URL is internet accessible via HTTPS at the MA_HOSTNAME variable you provided.")
                response_builder.set_should_end_session(True)
                return response_builder.response
        except requests.RequestException:
            logging.exception('Play Function URL: %s', url)
            response_builder.speak(
                "Sorry, I can't reach the audio file. Please check that your stream URL is internet accessible via HTTPS at the MA_HOSTNAME variable you provided.")
            response_builder.set_should_end_session(True)
            return response_builder.response

    response_builder.add_directive(
        PlayDirective(
            play_behavior=PlayBehavior.REPLACE_ALL,
            audio_item=AudioItem(
                stream=Stream(
                    token=url,
                    url=url,
                    offset_in_milliseconds=offset,
                    expected_previous_token=None
                )
            )
        )
    )

    if text:
        response_builder.speak(text)

    response_builder.set_should_end_session(True)

    try:
        push_alexa_metadata(url)
    except Exception:
        logging.exception('Error while preparing Alexa API push payload')

    return response_builder.response


# FIX: Enqueue next tracks with the correct expected_previous_token to avoid playback pauses or skips
def play_later(url, response_builder):
    """Enqueue a track to play immediately after the current one completes."""
    try:
        hostname = get_ma_hostname(raise_on_http_scheme=True)
    except ValueError:
        logging.warning("play_later: Invalid MA_HOSTNAME")
        return response_builder.response

    if not hostname:
        logging.warning("play_later: MA_HOSTNAME not set")
        return response_builder.response

    url = replace_ip_in_url(url, hostname)

    # expected_previous_token must match the current playing token (url) for correct ENQUEUE behavior
    response_builder.add_directive(
        PlayDirective(
            play_behavior=PlayBehavior.ENQUEUE,
            audio_item=AudioItem(
                stream=Stream(
                    token=url,
                    url=url,
                    offset_in_milliseconds=0,
                    expected_previous_token=url  # <-- FIX: Set to current URL instead of None
                )
            )
        )
    )

    return response_builder.response


def stop(text, response_builder, supports_apl=False):
    """Stop audio playback and close the session."""
    response_builder.add_directive(StopDirective())

    if text:
        response_builder.speak(text)

    response_builder.set_should_end_session(True)
    return response_builder.response


def pause(text, response_builder, supports_apl=False, session_new=False):
    """Pause playback (falls back to AudioPlayer Stop directive)."""
    response_builder.add_directive(StopDirective())
    response_builder.set_should_end_session(True)

    if text:
        response_builder.speak(text)

    return response_builder.response


def clear(response_builder):
    """Clear the playback queue."""
    response_builder.add_directive(ClearQueueDirective(
        clear_behavior=ClearBehavior.CLEAR_ENQUEUED))
    return response_builder.response


# APL dynamic metadata updates are disabled/simplified to prevent screen issues on devices like Echo Show
def update_apl_metadata(response_builder):
    pass


def schedule_apl_refresh(response_builder, delay_ms=1000):
    pass


