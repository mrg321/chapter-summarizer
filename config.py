#!/usr/bin/env python

__license__ = 'GPL v3'
__copyright__ = '2026, Comfy.n'
__docformat__ = 'restructuredtext en'

from calibre.utils.config import JSONConfig

PREFS = JSONConfig('plugins/ChapterSummarizer')
PREFS.defaults.update({
    'provider_name': 'NVIDIA Free',
    'model_name': '',
    'base_paragraphs': 2,
    'max_chunk_words': 1400,
    'include_highlight': True,
    'append_to_comments': False,
    'custom_append_column': '',
    'auto_add_to_data_files': False,
    'add_to_library': False,
    'add_to_library_format': 'epub',
    'summary_title_position': 'after',
    'summary_custom_tags': '',
    'summary_copy_source_cover': True,
    'chapter_cache_enabled': True,
    'cache_custom_dir': '',
    'target_language': 'same-as-source',
    'external_file_path': '',
    'verbose_ai_log': False,
    'debug_enabled': False,
    'ai_error_log': [],
    'request_interval_free': 1,
    'request_interval_default': 0,
    'anthropic_openai_compatible_override': False,
    'data_files_format': 'md',
    'html_output_style': 'auto',
    'html_custom_css': '',
    'epub_output_style': 'auto',
    'epub_custom_css': '',
    'summary_output_style': 'auto',
    'summary_custom_css': '',
    'multi_file_enabled': False,
    'selector_collapsed_paths': {},
    # Extra prompt additions (optional user-provided guidance)
    # Keys are stored as plain strings and appended to the user prompt only.
    'extra_prompt_flash_default': '',
    'extra_prompt_flash_fiction': '',
    'extra_prompt_flash_nonfiction': '',
    'extra_prompt_flash_other': '',
    'extra_prompt_full_default': '',
    'extra_prompt_full_fiction': '',
    'extra_prompt_full_nonfiction': '',
    'extra_prompt_full_other': '',
    # NVIDIA Free settings
    'show_nvidia_free_provider': False,
    'nvidia_free_proxy_url': 'https://nvidia-proxy.no-op',
    'nvidia_free_model': 'meta/llama-3.3-70b-instruct',
})


def pref(name, default=None):
    return PREFS.get(name, default)


def pref_bool(name, default=False):
    value = PREFS.get(name, default)
    if isinstance(value, bool):
        return value
    if value is None:
        return bool(default)
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {'1', 'true', 'yes', 'on'}:
        return True
    if text in {'0', 'false', 'no', 'off', ''}:
        return False
    return bool(value)


def set_pref(name, value):
    PREFS[name] = value
    commit = getattr(PREFS, 'commit', None)
    if callable(commit):
        commit()
