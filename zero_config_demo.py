#!/usr/bin/env python
"""Managed zero-config demo providers for Chapter Summarizer."""

from __future__ import annotations

__license__ = 'GPL v3'
__copyright__ = '2026, Comfy.n'
__docformat__ = 'restructuredtext en'

import threading
from collections.abc import Iterable, Iterator, Sequence
from typing import Any
from urllib.error import HTTPError, URLError

from calibre.ai import ChatMessage, ChatResponse
from calibre.ai.openai_compatible.backend import as_chat_responses, chat_request, get_available_models as get_openai_compatible_models
from calibre.ai.utils import chat_with_error_handler, read_streaming_response

try:
    load_translations()
except NameError:
    pass


ZERO_CONFIG_DEMO_PROVIDER_A = 'Zero-Config Demo (Server A)'
ZERO_CONFIG_DEMO_PROVIDER_B = 'Zero-Config Demo (Server B)'

_REQUEST_TIMEOUT = 120
_RETRYABLE_STATUS_CODES = {408, 409, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524}

_PROVIDER_DEFINITIONS: dict[str, dict[str, Any]] = {
    ZERO_CONFIG_DEMO_PROVIDER_A: {
        'api_url': 'https://api.kilo.ai/api/gateway',
        'models': (
            'openrouter/free',
            'cohere/north-mini-code:free',
            'nex-agi/nex-n2-pro:free',
            'poolside/laguna-xs.2:free',
            'kilo-auto/free',
        ),
    },
    ZERO_CONFIG_DEMO_PROVIDER_B: {
        'api_url': 'https://oai.endpoints.kepler.ai.cloud.ovh.net/v1',
        'models': (
            'Llama-3.1-8B-Instruct',
            'Mistral-Nemo-Instruct-2407',
            'Qwen3-Coder-30B-A3B-Instruct',
            'gpt-oss-20b',
            'gpt-oss-120b',
        ),
    },
}

_ROTATION_STATE = {
    name: {'next_index': 0, 'lock': threading.Lock()}
    for name in _PROVIDER_DEFINITIONS
}


def is_zero_config_demo_provider(provider_name: str) -> bool:
    return str(provider_name or '').strip() in _PROVIDER_DEFINITIONS


def managed_models_for_provider(provider_name: str) -> tuple[str, ...]:
    info = _PROVIDER_DEFINITIONS.get(str(provider_name or '').strip()) or {}
    return tuple(info.get('models') or ())


def api_url_for_provider(provider_name: str) -> str:
    info = _PROVIDER_DEFINITIONS.get(str(provider_name or '').strip()) or {}
    return str(info.get('api_url') or '').strip()


def planned_model_for_provider(provider_name: str) -> str:
    name = str(provider_name or '').strip()
    models = managed_models_for_provider(name)
    if not models:
        return ''
    state = _ROTATION_STATE[name]
    with state['lock']:
        return models[state['next_index'] % len(models)]


def reset_rotation_state(provider_name: str = '') -> None:
    targets = (str(provider_name or '').strip(),) if provider_name else tuple(_ROTATION_STATE)
    for name in targets:
        state = _ROTATION_STATE.get(name)
        if state is None:
            continue
        with state['lock']:
            state['next_index'] = 0


def fetch_remote_model_ids(provider_name: str) -> tuple[str, ...]:
    api_url = api_url_for_provider(provider_name)
    if not api_url:
        return ()
    models = get_openai_compatible_models(use_api_url=api_url, use_api_key='', use_headers=())
    return tuple(models)


def _request_model_order(provider_name: str) -> tuple[str, ...]:
    name = str(provider_name or '').strip()
    models = managed_models_for_provider(name)
    if not models:
        return ()
    state = _ROTATION_STATE[name]
    with state['lock']:
        start_index = state['next_index'] % len(models)
    return models[start_index:] + models[:start_index]


def _advance_rotation(provider_name: str, model_id: str) -> None:
    name = str(provider_name or '').strip()
    models = managed_models_for_provider(name)
    if not models or model_id not in models:
        return
    state = _ROTATION_STATE[name]
    with state['lock']:
        state['next_index'] = (models.index(model_id) + 1) % len(models)


def _should_rotate_on_error(err: Exception) -> bool:
    if isinstance(err, HTTPError):
        return err.code in _RETRYABLE_STATUS_CODES or 400 <= err.code < 600
    if isinstance(err, (URLError, TimeoutError, OSError)):
        return True
    text = str(err or '').strip().lower()
    if not text:
        return False
    markers = (
        '429',
        'bad gateway',
        'connection reset',
        'empty response',
        'no response returned',
        'rate limit',
        'remote end closed',
        'service unavailable',
        'temporarily unavailable',
        'timed out',
        'timeout',
        'too many requests',
    )
    return any(marker in text for marker in markers)


class ZeroConfigDemoProvider:

    name = ''
    _api_url = ''
    _models: Sequence[str] = ()

    @property
    def is_ready_for_use(self) -> bool:
        return True

    def human_readable_model_name(self, model_id: str) -> str:
        return str(model_id or '').strip()

    def get_available_models(self) -> list[str]:
        return list(self._models)

    def text_chat(self, messages: Iterable[ChatMessage], use_model: str = '') -> Iterator[ChatResponse]:
        yield from chat_with_error_handler(self._text_chat_impl(messages, use_model))

    def _text_chat_impl(self, messages: Iterable[ChatMessage], use_model: str = '') -> Iterator[ChatResponse]:
        del use_model
        message_list = tuple(messages)
        tried_models = _request_model_order(self.name)
        last_error: Exception | None = None
        for model_id in tried_models:
            emitted_any = False
            seen_metadata = False
            try:
                data = {
                    'model': model_id,
                    'messages': [{'role': m.type.value, 'content': m.query} for m in message_list],
                    'stream': True,
                    'temperature': 0.7,
                }
                rq = chat_request(data, url_override=self._api_url, use_api_key='', use_headers=())
                for chunk in read_streaming_response(rq, self.name, timeout=_REQUEST_TIMEOUT):
                    for response in as_chat_responses(chunk, model_id):
                        patched = response._replace(
                            provider=self.name if (response.has_metadata or response.exception) else response.provider,
                            plugin_name=self.name,
                        )
                        if patched.has_metadata:
                            seen_metadata = True
                        if patched.exception:
                            yield patched
                            return
                        if patched.content or patched.reasoning:
                            emitted_any = True
                        yield patched
                if not emitted_any:
                    raise ValueError(_('The demo provider returned an empty response.'))
                _advance_rotation(self.name, model_id)
                if not seen_metadata:
                    yield ChatResponse(has_metadata=True, provider=self.name, model=model_id, plugin_name=self.name)
                return
            except Exception as err:
                last_error = err
                if emitted_any or not _should_rotate_on_error(err):
                    raise
                continue
        if tried_models:
            _advance_rotation(self.name, tried_models[0])
        raise last_error or ValueError(_('The zero-config demo provider is temporarily unavailable.'))


class ZeroConfigDemoServerAProvider(ZeroConfigDemoProvider):
    name = ZERO_CONFIG_DEMO_PROVIDER_A
    _api_url = api_url_for_provider(ZERO_CONFIG_DEMO_PROVIDER_A)
    _models = managed_models_for_provider(ZERO_CONFIG_DEMO_PROVIDER_A)


class ZeroConfigDemoServerBProvider(ZeroConfigDemoProvider):
    name = ZERO_CONFIG_DEMO_PROVIDER_B
    _api_url = api_url_for_provider(ZERO_CONFIG_DEMO_PROVIDER_B)
    _models = managed_models_for_provider(ZERO_CONFIG_DEMO_PROVIDER_B)


def resolve_zero_config_demo_provider(provider_name: str) -> ZeroConfigDemoProvider | None:
    name = str(provider_name or '').strip()
    if name == ZERO_CONFIG_DEMO_PROVIDER_A:
        return ZeroConfigDemoServerAProvider()
    if name == ZERO_CONFIG_DEMO_PROVIDER_B:
        return ZeroConfigDemoServerBProvider()
    return None


def find_tests():
    import unittest

    class TestZeroConfigDemoProviders(unittest.TestCase):

        def setUp(self):
            reset_rotation_state()

        def test_curated_models_server_a(self):
            self.assertEqual(managed_models_for_provider(ZERO_CONFIG_DEMO_PROVIDER_A), (
                'openrouter/free',
                'cohere/north-mini-code:free',
                'nex-agi/nex-n2-pro:free',
                'poolside/laguna-xs.2:free',
                'kilo-auto/free',
            ))

        def test_curated_models_server_b(self):
            self.assertEqual(managed_models_for_provider(ZERO_CONFIG_DEMO_PROVIDER_B), (
                'Llama-3.1-8B-Instruct',
                'Mistral-Nemo-Instruct-2407',
                'Qwen3-Coder-30B-A3B-Instruct',
                'gpt-oss-20b',
                'gpt-oss-120b',
            ))

        def test_rotation_advances_planned_model(self):
            self.assertEqual(planned_model_for_provider(ZERO_CONFIG_DEMO_PROVIDER_A), 'openrouter/free')
            _advance_rotation(ZERO_CONFIG_DEMO_PROVIDER_A, 'openrouter/free')
            self.assertEqual(planned_model_for_provider(ZERO_CONFIG_DEMO_PROVIDER_A), 'cohere/north-mini-code:free')

        def test_request_order_uses_rotation_state(self):
            order = _request_model_order(ZERO_CONFIG_DEMO_PROVIDER_A)
            self.assertEqual(order[0], 'openrouter/free')
            _advance_rotation(ZERO_CONFIG_DEMO_PROVIDER_A, 'openrouter/free')
            order = _request_model_order(ZERO_CONFIG_DEMO_PROVIDER_A)
            self.assertEqual(order[0], 'cohere/north-mini-code:free')

    return unittest.defaultTestLoader.loadTestsFromTestCase(TestZeroConfigDemoProviders)
