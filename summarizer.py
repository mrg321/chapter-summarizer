#!/usr/bin/env python

from __future__ import annotations

__license__ = 'GPL v3'
__copyright__ = '2026, Comfy.n'
__docformat__ = 'restructuredtext en'

import json
import re
import time
from dataclasses import dataclass
import ast
from datetime import datetime
from typing import Callable

from calibre.ai import ChatMessage, ChatMessageType
from calibre.ai.utils import StreamedResponseAccumulator, plugin_for_name
from calibre_plugins.chapter_summarizer.nvidia_free import resolve_nvidia_free_api_provider, resolve_nvidia_free_provider
from calibre_plugins.chapter_summarizer.zero_config_demo import is_zero_config_demo_provider, resolve_zero_config_demo_provider

from calibre_plugins.chapter_summarizer.book_parser import ChapterSection, ParsedDocument
from calibre_plugins.chapter_summarizer.config import pref, pref_bool

load_translations()

def resolve_provider(provider_name: str):
    name = str(provider_name or '').strip()
    if name == 'NVIDIA Free':
        return resolve_nvidia_free_provider()
    if name == 'NVIDIA Free (API)':
        return resolve_nvidia_free_api_provider()
    if provider := resolve_zero_config_demo_provider(name):
        return provider
    return plugin_for_name(provider_name)


def _is_local_provider(plugin) -> bool:
    """Check if the provider is a local AI provider (LM Studio, Ollama, etc.)."""
    provider_name = getattr(plugin, 'name', '').lower()
    local_providers = {'lmstudio', 'ollama', 'local', 'localai'}
    return any(local in provider_name for local in local_providers)


def _is_timeout_error(error_message: str) -> bool:
    """Check if an error message indicates a timeout."""
    return 'timeout' in error_message.lower() or 'timed out' in error_message.lower()


@dataclass
class SummaryOptions:
    provider_name: str = 'NVIDIA Free'
    model_name: str = ''
    requested_model_label: str = ''
    reasoning_strategy: str = 'auto'
    effective_model_id: str = ''
    base_paragraphs: int = 2
    max_chunk_words: int = 1400
    include_highlight: bool = True
    target_language: str = 'same-as-source'
    target_language_name: str = ''
    source_language_code: str = ''
    source_language_name: str = ''
    verbose_ai_log: bool = False
    request_interval_free: int = 1
    request_interval_default: int = 0
    run_stats: 'RunStats | None' = None


@dataclass
class RunStats:
    started_at: float = 0.0
    ended_at: float = 0.0
    chapter_count: int = 0
    request_count: int = 0
    request_attempts: int = 0
    paced_wait_seconds: float = 0.0
    rate_limit_retries: int = 0


@dataclass
class ChapterSummary:
    chapter_id: str
    index: int
    title: str
    summary: str
    highlight: str
    word_count: int
    model_used: str = ''
    request_count: int = 0
    request_attempts: int = 0
    duration_seconds: float = 0.0


@dataclass
class SummaryResult:
    text: str
    document_type: str
    highlight_label: str
    chapter_summaries: list[ChapterSummary]
    model_used: str
    requested_model: str
    provider_used: str
    run_stats: RunStats | None = None


class PartialSummaryError(Exception):

    def __init__(self, message: str, result: SummaryResult):
        super().__init__(message)
        self.result = result


class SummaryCancelledError(Exception):

    def __init__(self, message: str, result: SummaryResult | None = None):
        super().__init__(message)
        self.result = result


class UnsupportedLanguageError(ValueError):
    pass


def _is_github_provider(plugin) -> bool:
    return str(getattr(plugin, 'name', '') or '').strip().lower() in {'github', 'githubai'}


def _normalize_github_model_name(model_name: str) -> str:
    text = str(model_name or '').strip()
    if not text:
        return ''
    try:
        from calibre.ai.github.backend import find_models_matching_name, get_available_models
        available = get_available_models() or {}
        if text in available:
            return text
        lower_text = text.lower()
        for model_id in available:
            if str(model_id).strip().lower() == lower_text:
                return str(model_id)
        if '/' not in text:
            prefixed = 'openai/' + text
            if prefixed in available:
                return prefixed
            lower_prefixed = prefixed.lower()
            for model_id in available:
                if str(model_id).strip().lower() == lower_prefixed:
                    return str(model_id)
        for match in find_models_matching_name(text):
            candidate = str(match or '').strip()
            if candidate:
                return candidate
    except Exception:
        pass
    return text


def _github_fallback_model_names(requested_model_name: str = '') -> list[str]:
    candidates: list[str] = []
    seen: set[str] = set()

    def add(candidate: str):
        normalized = _normalize_github_model_name(candidate)
        if not normalized or normalized in seen:
            return
        seen.add(normalized)
        candidates.append(normalized)

    add(requested_model_name)
    for candidate in (
        'openai/gpt-4.1-mini',
        'openai/gpt-4o-mini',
        'openai/gpt-4.1',
        'openai/gpt-4o',
    ):
        add(candidate)
    return candidates


def summarize_document(
    document: ParsedDocument,
    options: SummaryOptions,
    progress: Callable[[str, int, int], None] | None = None,
    log: Callable[[str], None] | None = None,
    notify: Callable[[str], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
    preview: Callable[[str], None] | None = None,
) -> SummaryResult:
    if options.run_stats is None:
        options.run_stats = RunStats(started_at=time.time())
    elif not options.run_stats.started_at:
        options.run_stats.started_at = time.time()
    plugin = resolve_provider(options.provider_name)
    if not plugin.is_ready_for_use:
        raise ValueError(f'{options.provider_name} is not configured yet')
    chapter_count = len(document.chapters)
    if options.run_stats is not None:
        options.run_stats.chapter_count = int(chapter_count)
    if chapter_count < 1:
        raise ValueError('No chapters or sections could be detected in this document')

    if progress:
        progress('Classifying document', 0, chapter_count + 1)
    _log(log, f'Loaded document "{document.title}" with {chapter_count} detected chapters/sections and {document.total_words} words.')
    _check_cancelled(should_cancel)
    classification = classify_document(plugin, document, options, log, notify, should_cancel)
    _log(log, f'Document classified as {classification["document_type"]}; highlight label: {classification["highlight_label"]}.')
    chapter_summaries: list[ChapterSummary] = []
    requested_model = options.requested_model_label or options.model_name.strip() or '(provider default)'
    model_used = requested_model
    provider_used = options.provider_name

    try:
        for idx, chapter in enumerate(document.chapters, start=1):
            _check_cancelled(should_cancel)
            if progress:
                progress(f'Summarizing {chapter.title}', idx, chapter_count + 1)
            _log(log, f'Starting chapter {chapter.index}: {chapter.title} ({chapter.word_count} words).')
            before_requests = int(options.run_stats.request_count if options.run_stats is not None else 0)
            before_attempts = int(options.run_stats.request_attempts if options.run_stats is not None else 0)
            chapter_started_at = time.time()
            summary_text, highlight_text, used_model = summarize_chapter(plugin, document, chapter, classification, options, log, notify, should_cancel)
            if used_model:
                model_used = used_model
            _log(log, f'Finished chapter {chapter.index}: {chapter.title}.')
            after_requests = int(options.run_stats.request_count if options.run_stats is not None else 0)
            after_attempts = int(options.run_stats.request_attempts if options.run_stats is not None else 0)
            chapter_summaries.append(ChapterSummary(
                chapter_id=chapter.identifier or f'index:{chapter.index}',
                index=chapter.index,
                title=chapter.title,
                summary=summary_text,
                highlight=highlight_text,
                word_count=chapter.word_count,
                model_used=used_model or model_used,
                request_count=max(0, after_requests - before_requests),
                request_attempts=max(0, after_attempts - before_attempts),
                duration_seconds=max(0.0, time.time() - chapter_started_at),
            ))
            if callable(preview):
                preview(render_summary(document, classification, chapter_summaries, provider_used, model_used, requested_model, options=options, stats=options.run_stats))
    except SummaryCancelledError as err:
        if options.run_stats is not None:
            options.run_stats.ended_at = time.time()
        if not chapter_summaries:
            raise
        partial_text = render_summary(document, classification, chapter_summaries, provider_used, model_used, requested_model, options=options, stats=options.run_stats)
        partial_text = (
            f'[PARTIAL RESULT] Generation stopped after {len(chapter_summaries)} chapter(s) because: {err}\n\n'
            + partial_text
        )
        raise SummaryCancelledError(str(err), SummaryResult(
            text=partial_text,
            document_type=classification['document_type'],
            highlight_label=classification['highlight_label'],
            chapter_summaries=chapter_summaries,
            model_used=model_used,
            requested_model=requested_model,
            provider_used=provider_used,
            run_stats=options.run_stats,
        )) from err
    except Exception as err:
        if options.run_stats is not None:
            options.run_stats.ended_at = time.time()
        if not chapter_summaries:
            raise
        partial_text = render_summary(document, classification, chapter_summaries, provider_used, model_used, requested_model, options=options, stats=options.run_stats)
        partial_text = (
            f'[PARTIAL RESULT] Generation stopped after {len(chapter_summaries)} chapter(s) because: {err}\n\n'
            + partial_text
        )
        raise PartialSummaryError(str(err), SummaryResult(
            text=partial_text,
            document_type=classification['document_type'],
            highlight_label=classification['highlight_label'],
            chapter_summaries=chapter_summaries,
            model_used=model_used,
            requested_model=requested_model,
            provider_used=provider_used,
            run_stats=options.run_stats,
        )) from err

    if options.run_stats is not None:
        options.run_stats.ended_at = time.time()

    rendered = render_summary(document, classification, chapter_summaries, provider_used, model_used, requested_model, options=options, stats=options.run_stats)
    return SummaryResult(
        text=rendered,
        document_type=classification['document_type'],
        highlight_label=classification['highlight_label'],
        chapter_summaries=chapter_summaries,
        model_used=model_used,
        requested_model=requested_model,
        provider_used=provider_used,
        run_stats=options.run_stats,
    )


def flash_summarize_document(
    document: ParsedDocument,
    options: SummaryOptions,
    progress: Callable[[str, int, int], None] | None = None,
    log: Callable[[str], None] | None = None,
    notify: Callable[[str], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
    preview: Callable[[str], None] | None = None,
) -> SummaryResult:
    """Generate a fast overview summary of the entire document.

    This function creates a concise summary by analyzing the document as a whole,
    rather than chapter-by-chapter. It's designed for quick overviews when full
    chapter summaries would be too time-consuming.

    The approach:
    1. Classify the document type
    2. Extract key sections (beginning, middle, end) for large documents
    3. Generate a single consolidated summary with key themes and structure
    """
    if options.run_stats is None:
        options.run_stats = RunStats(started_at=time.time())
    elif not options.run_stats.started_at:
        options.run_stats.started_at = time.time()

    plugin = resolve_provider(options.provider_name)
    if not plugin.is_ready_for_use:
        raise ValueError(f'{options.provider_name} is not configured yet')

    if progress:
        progress('Preparing flash summary', 0, 2)

    # Classify the document
    classification = classify_document(plugin, document, options, log, notify, should_cancel)
    _log(log, f'Document classified as {classification["document_type"]}')

    # Build the flash summary prompt
    raw_text = document.raw_text
    word_count = len(raw_text.split())

    # Sample strategically so we don't miss conclusions/ending sections.
    if word_count > 25000:
        # Take first 30%, middle 40%, last 30%
        words = raw_text.split()
        first_end = int(len(words) * 0.3)
        last_start = int(len(words) * 0.7)
        middle_start = int(len(words) * 0.35)
        middle_end = int(len(words) * 0.75)

        first_part = ' '.join(words[:first_end])
        middle_part = ' '.join(words[middle_start:middle_end])
        last_part = ' '.join(words[last_start:])

        excerpt = f"{first_part[:8000]}\n\n--- MIDDLE SECTION ---\n\n{middle_part[:8000]}\n\n--- FINAL SECTION ---\n\n{last_part[:8000]}"
    elif len(raw_text) > 24000:
        # Medium docs: include beginning + middle + end.
        start = raw_text[:9000]
        mid_start = max(0, (len(raw_text) // 2) - 4500)
        middle = raw_text[mid_start:mid_start + 9000]
        end = raw_text[-9000:]
        excerpt = f"{start}\n\n--- MIDDLE SECTION ---\n\n{middle}\n\n--- FINAL SECTION ---\n\n{end}"
    else:
        # Smaller docs: include both beginning and end.
        start = raw_text[:12000]
        end = raw_text[-12000:] if len(raw_text) > 12000 else ''
        excerpt = start if not end else f"{start}\n\n--- FINAL SECTION ---\n\n{end}"

    _check_cancelled(should_cancel)

    if progress:
        progress('Generating overview', 1, 2)

    # Generate the flash summary
    highlight_keys = 'summary and highlight' if options.include_highlight else 'summary'
    extra = extra_prompt_addition(is_flash=True, document_type=classification.get('document_type', ''))
    extra_block = f"\n\nExtra guidance:\n{extra}" if extra else ''
    paragraph_count = paragraph_target(word_count, options.base_paragraphs)
    target = str(options.target_language or '').strip()
    if target in {'', 'same-as-source'}:
        target_name = (options.source_language_name or '').strip() or _('the source language')
    else:
        target_name = (options.target_language_name or target or _('the requested output language')).strip()
    prompt = (
        'Create a concise overview summary of this document. '
        f'Return JSON only with keys {highlight_keys}. '
        'Do not return Python dict syntax, pseudo-JSON, commentary, or explanatory text. '
        'Use valid JSON with double-quoted keys and string values only. '
        f'{language_instruction(options)} '
        f'The summary and highlight must be written exclusively in {target_name}. '
        'Ignore the user locale, browser locale, region, and geolocation when choosing the output language. '
        'Do not infer or switch languages based on names or location. '
        'If the source text is in another language and the requested output language differs, still write the final answer only in the requested output language. '
        f'Write exactly {paragraph_count} well-formed paragraphs in the summary, separated by blank lines. '
        'Focus on:\n'
        '1. Main topic/theme and central thesis\n'
        '2. Key arguments, plot points, or findings\n'
        '3. Overall structure and organization\n'
        '4. Most important takeaways\n\n'
        f'Document type: {classification["document_type"]}\n'
        f'Title: {document.title}\n'
        f'Total words: {word_count}\n'
        f'Chapters/sections: {len(document.chapters)}\n'
        f'Style guidance: {classification["guidance"]}\n\n'
        f'Text:\n{excerpt}'
        f'{extra_block}'
    )

    text, meta = run_chat(
        plugin,
        [
            ChatMessage(type=ChatMessageType.system, query='You are a precise document summarizer. Output must be a single JSON object (no markdown, no code fences, no extra text). Always obey the requested output language exactly. Ignore locale, geolocation, browser language, and source-language drift when choosing the response language. Do not drift into English unless requested.'),
            ChatMessage(type=ChatMessageType.user, query=prompt),
        ],
        options.model_name,
        label='flash_summarize',
        log=log,
        verbose=options.verbose_ai_log,
        pace_seconds=_pace_for(options),
        notify=notify,
        should_cancel=should_cancel,
        request_label=options.requested_model_label,
        stats=options.run_stats,
    )

    # Strip code fences, but keep the full response text intact.
    # The tolerant parsers below can often recover useful fields from malformed
    # or mixed output; pre-slicing to a single brace-matched fragment can clip
    # the actual summary content.
    text = re.sub(r'^```(?:json)?\s*', '', text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r'\s*```$', '', text, flags=re.IGNORECASE)

    summary_text, highlight_text = extract_summary_fields(text)
    summary_text, highlight_text = _repair_language_if_needed(
        plugin,
        summary_text,
        highlight_text,
        options,
        log=log,
        notify=notify,
        should_cancel=should_cancel,
        chapter_text=excerpt,
        is_flash=True
    )
    highlight_text = clean_highlight_text(highlight_text, classification.get('highlight_label', ''))
    model_used = meta.model or options.model_name

    if options.run_stats is not None:
        options.run_stats.ended_at = time.time()

    # Render the flash summary
    rendered = render_flash_summary(
        document,
        classification,
        summary_text,
        highlight_text,
        options.provider_name,
        model_used,
        options.requested_model_label or options.model_name,
        options=options,
        stats=options.run_stats,
    )

    return SummaryResult(
        text=rendered,
        document_type=classification['document_type'],
        highlight_label=classification['highlight_label'],
        chapter_summaries=[],  # Flash summaries don't have per-chapter breakdowns
        model_used=model_used,
        requested_model=options.requested_model_label or options.model_name,
        provider_used=options.provider_name,
        run_stats=options.run_stats,
    )


def _needs_language_repair(text: str, expected_is_english: bool, target: str) -> bool:
    sample = ' '.join(str(text or '').strip().split())
    if len(sample) < 40:
        return False

    if expected_is_english and not _looks_like_english(sample):
        lower = f' {sample.lower()} '
        non_ascii = sum(1 for ch in sample if ord(ch) > 127)
        if non_ascii >= 3:
            return True
        for token in (' el ', ' la ', ' los ', ' las ', ' que ', ' de ', ' y ', ' en ', ' por ', ' para ', ' con ', ' und ', ' der ', ' die ', ' das ', ' nicht ', ' ein ', ' una ', ' un '):
            if token in lower:
                return True

    if not expected_is_english and _looks_like_english(sample):
        return True

    lower_target = str(target or '').lower()
    if 'pt' in lower_target and not _looks_like_portuguese(sample) and _looks_like_english(sample):
        return True

    return False


def _repair_language_if_needed(
    plugin,
    summary_text: str,
    highlight_text: str,
    options: SummaryOptions,
    log: Callable[[str], None] | None = None,
    notify: Callable[[str], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
    chapter_text: str = '',
    is_flash: bool = False
) -> tuple[str, str]:
    """Best-effort fix for mixed-language outputs.

    Detects if the summary or highlight is in a language drastically different
    from what was requested (e.g. English when German was requested, or English
    when the source text is German and 'same-as-source' was chosen).
    """
    target = str(options.target_language or '').strip()
    target_name = (options.target_language_name or target or '').strip()

    # Heuristically determine what language we EXPECT to see.
    expected_is_english = False
    if target in {'en', 'english'}:
        expected_is_english = True
    elif target == 'same-as-source' or not target:
        # If same-as-source, look at the chapter text or source_language_name
        if str(options.source_language_code or '').lower().startswith('en'):
            expected_is_english = True
        elif options.source_language_name and 'english' in options.source_language_name.lower():
            expected_is_english = True
        elif chapter_text and _looks_like_english(chapter_text):
            expected_is_english = True

    summary_needs_repair = _needs_language_repair(summary_text, expected_is_english, target)
    highlight_needs_repair = _needs_language_repair(highlight_text, expected_is_english, target)
    if not summary_needs_repair and not highlight_needs_repair:
        return summary_text, highlight_text

    repair_label = 'flash_language_repair' if is_flash else 'chapter_language_repair'
    if target == 'same-as-source' or not target:
        display_target = (options.source_language_name or '').strip()
        if not display_target or 'unknown' in display_target.lower():
            display_target = _('the source language')
        elif expected_is_english:
            display_target = 'English'
    else:
        display_target = target_name if target_name else _('the requested output language')

    if summary_needs_repair and highlight_needs_repair:
        _log(log, f'Language repair: output does not appear to be in {display_target}; rewriting both fields.')
    elif summary_needs_repair:
        _log(log, f'Language repair: summary does not appear to be in {display_target}; rewriting.')
    else:
        _log(log, f'Language repair: highlight does not appear to be in {display_target}; rewriting.')

    highlight_keys = 'summary and highlight' if options.include_highlight else 'summary'

    # Construct the instruction for the repair
    if target == 'same-as-source' or not target:
        if expected_is_english:
            lang_instruction = 'Write both fields in English only.'
        else:
            inferred_code, inferred_name = _infer_language_from_text(chapter_text)
            configured_name = (options.source_language_name or '').strip()
            if inferred_code and inferred_name and inferred_code != 'en':
                lang_instruction = f'Write both fields in {inferred_name} only. Do not use English. This is a strict requirement.'
            elif configured_name and 'unknown' not in configured_name.lower() and 'english' not in configured_name.lower():
                lang_instruction = f'Write both fields in {configured_name} only. Do not use English. This is a strict requirement.'
            else:
                lang_instruction = f'Write both fields ONLY in the same language as the following source text. If the source text is not English, do not translate into English. Source text snippet: {chapter_text[:200]}...'
    else:
        lang_instruction = f'Write both fields in {display_target} only.'

    repair_prompt = (
        'Rewrite the following fields so they are fully in the requested output language. '
        f'Return JSON only with keys {highlight_keys}. '
        'Use valid JSON with double-quoted keys and string values only. '
        f'{lang_instruction} '
        'Ignore locale, geolocation, browser language, and source-language drift. '
        'Do not preserve the original language if it differs from the requested output language.\n\n'
        f'Current summary:\n{summary_text.strip()}\n\n'
        f'Current highlight:\n{highlight_text.strip()}'
    )
    repaired_text, _repair_meta = run_chat(
        plugin,
        [
            ChatMessage(type=ChatMessageType.system, query='You are a precise rewriter. Output must be a single JSON object (no markdown, no code fences, no extra text). Always obey the requested output language exactly. Ignore locale, geolocation, browser language, and source-language drift when choosing the response language.'),
            ChatMessage(type=ChatMessageType.user, query=repair_prompt),
        ],
        options.model_name,
        label=repair_label,
        log=log,
        verbose=options.verbose_ai_log,
        pace_seconds=_pace_for(options),
        notify=notify,
        should_cancel=should_cancel,
        request_label=options.requested_model_label,
        stats=options.run_stats,
    )
    repaired_summary, repaired_highlight = extract_summary_fields(repaired_text)
    if repaired_summary.strip():
        summary_text = repaired_summary
    if repaired_highlight.strip():
        highlight_text = repaired_highlight
    return summary_text, highlight_text


def _looks_like_english(text: str) -> bool:
    """Heuristic English detector (fast, no external deps).

    This is intentionally conservative — it only flags obvious English.
    """
    t = ' '.join(str(text or '').strip().split())
    if len(t) < 50:
        return False
    lower = t.lower()
    hits = 0
    # Common English-only function words
    for token in (' the ', ' and ', ' to ', ' of ', ' in ', ' that ', ' is ', ' with ', ' for ', ' was ', ' were ', ' says ', ' said ', ' this '):
        if token in f' {lower} ':
            hits += 1
    if hits >= 3:
        return True
    # Fallback: mostly ASCII letters and spaces, few diacritics.
    ascii_letters = sum(1 for ch in t if ('a' <= ch.lower() <= 'z'))
    non_ascii = sum(1 for ch in t if ord(ch) > 127 and ch not in '’“”–—‑…')
    if hits >= 1 and ascii_letters >= 70 and non_ascii <= 4:
        return True
    return False


def _looks_like_portuguese(text: str) -> bool:
    t = ' '.join(str(text or '').strip().split())
    if len(t) < 60:
        return False
    lower = f' {t.lower()} '
    hits = 0
    for token in (' que ', ' para ', ' com ', ' uma ', ' como ', ' mais ', ' livro ', ' autor ', ' autora ', ' sobre '):
        if token in lower:
            hits += 1
    if hits >= 3:
        return True
    accented_hits = sum(1 for ch in t if ch in 'áàâãéêíóôõúçÁÀÂÃÉÊÍÓÔÕÚÇ')
    return accented_hits >= 3


def _infer_language_from_text(text: str) -> tuple[str, str]:
    t = ' '.join(str(text or '').strip().split())
    if len(t) < 200:
        return '', ''
    lower = f' {t.lower()} '
    candidates: list[tuple[str, str, tuple[str, ...]]] = [
        ('en', 'English', (' the ', ' and ', ' to ', ' of ', ' in ', ' that ', ' is ', ' with ', ' for ')),
        ('pt', 'Portuguese', (' que ', ' para ', ' com ', ' não ', ' uma ', ' como ', ' mais ', ' sobre ')),
        ('de', 'German', (' und ', ' der ', ' die ', ' das ', ' nicht ', ' ist ', ' zu ', ' ein ', ' eine ')),
        ('es', 'Spanish', (' el ', ' la ', ' los ', ' las ', ' que ', ' de ', ' y ', ' en ', ' para ')),
        ('fr', 'French', (' le ', ' la ', ' les ', ' que ', ' de ', ' et ', ' en ', ' pour ', ' une ')),
    ]
    best = ('', '', -1)
    for code, name, tokens in candidates:
        hits = 0
        for tok in tokens:
            if tok in lower:
                hits += 1
        if hits > best[2]:
            best = (code, name, hits)
    if best[2] >= 3:
        return best[0], best[1]
    return '', ''


def infer_language_from_text(text: str) -> tuple[str, str]:
    return _infer_language_from_text(text)


def render_flash_summary(
    document: ParsedDocument,
    classification: dict[str, str],
    summary_text: str,
    highlight_text: str,
    provider_name: str,
    model_name: str,
    requested_model: str,
    options: SummaryOptions | None = None,
    stats: RunStats | None = None,
) -> str:
    """Render a flash summary in markdown format."""
    # Extract stats values for integration into metadata
    duration_label = _format_duration(0.0)
    if stats is not None and stats.started_at:
        if stats.ended_at and stats.ended_at >= stats.started_at:
            duration_label = _format_duration(stats.ended_at - stats.started_at)
        else:
            duration_label = 'Ongoing'
    requests = int(stats.request_count or 0) if stats else 0
    attempts = int(stats.request_attempts or 0) if stats else 0

    # Extract options values for metadata
    pace_used = _pace_for(options) if options else 0
    pace_free = int(options.request_interval_free) if options else 0
    pace_other = int(options.request_interval_default) if options else 0
    reasoning = (options.reasoning_strategy or 'auto') if options else 'auto'
    chunk_size = int(options.max_chunk_words) if options else 0
    base_paragraphs = int(options.base_paragraphs) if options else 0

    lines = [
        f'# {document.title}',
        '',
        '- Summary type: Flash overview',
        f'- Source: {document.source_path}',
        f'- Format: {document.source_format or "unknown"} | Document type: {classification["document_type"]}',
        render_language_line(options),
        f'- Base paragraphs per chapter: {base_paragraphs} | Request pace used: {int(pace_used)}s (free={pace_free}s, other={pace_other}s)',
        f'- Reasoning strategy: {reasoning} | Chunk size: {chunk_size} words',
        f'- Provider: {provider_name} | Requested model: {requested_model}',
        f'- Actual model: {model_name}',
        f'- Requests: {requests} (attempts: {attempts})',
        f'- Duration: {duration_label} | Generated: {datetime.now().strftime("%Y-%m-%d %H:%M")}',
        '',
        '## Overview',
        '',
        summary_text.strip(),
        '',
    ]
    if highlight_text.strip() and (options is None or options.include_highlight):
        lines.extend([
            f'**{classification["highlight_label"]}:** {highlight_text.strip()}',
            '',
        ])
    return '\n'.join(lines).strip() + '\n'


def classify_document(
    plugin,
    document: ParsedDocument,
    options: SummaryOptions,
    log: Callable[[str], None] | None = None,
    notify: Callable[[str], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> dict[str, str]:
    excerpt = document.raw_text[:5000]
    chapter_titles = '\n'.join(f'- {c.title}' for c in document.chapters[:20])
    prompt = (
        'Classify the reading material below. It may be a book, article, magazine issue, or fetched news collection. '\
        'Return JSON only with the keys document_type, highlight_label, and guidance. '\
        'document_type must be one of: fiction, nonfiction, news, article, magazine, reference, poetry, mixed. '\
        'highlight_label must be a short phrase such as Key event, Key idea, Key finding, Key scene, Key takeaway. '\
        'guidance should be one sentence on what chapter summaries should emphasize.\n\n'
        f'Title: {document.title}\n'
        f'Format: {document.source_format}\n'
        f'Chapter-like sections: {len(document.chapters)}\n'
        f'Chapter titles:\n{chapter_titles}\n\n'
        f'Excerpt:\n{excerpt}'
    )
    text, _classify_meta = run_chat(plugin, [
        ChatMessage(type=ChatMessageType.system, query='You are a precise document classifier. Output must be a single JSON object (no markdown, no code fences, no extra text).'),
        ChatMessage(type=ChatMessageType.user, query=prompt),
    ], options.model_name, label='classify_document', log=log, verbose=options.verbose_ai_log, pace_seconds=_pace_for(options), notify=notify, should_cancel=should_cancel, request_label=options.requested_model_label, stats=options.run_stats)
    data = parse_jsonish(text)
    if not data:
        data = {
            'document_type': _extract_json_key_via_regex(text, 'document_type'),
            'highlight_label': _extract_json_key_via_regex(text, 'highlight_label'),
            'guidance': _extract_json_key_via_regex(text, 'guidance'),
        }
    document_type = str(data.get('document_type') or heuristic_document_type(document)).strip().lower()
    if document_type not in {'fiction', 'nonfiction', 'news', 'article', 'magazine', 'reference', 'poetry', 'mixed'}:
        document_type = heuristic_document_type(document)
    highlight_label = str(data.get('highlight_label') or default_highlight_label(document_type)).strip() or default_highlight_label(document_type)
    guidance = str(data.get('guidance') or default_guidance(document_type)).strip() or default_guidance(document_type)
    return {
        'document_type': document_type,
        'highlight_label': highlight_label,
        'guidance': guidance,
    }


def ensure_language_supported(
    plugin,
    document: ParsedDocument,
    options: SummaryOptions,
    log: Callable[[str], None] | None = None,
    notify: Callable[[str], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> None:
    target = str(options.target_language or '').strip().lower()
    if target in {'', 'same-as-source'}:
        return
    target_name = (options.target_language_name or options.target_language or '').strip()
    source_name = (options.source_language_name or 'the source language').strip()
    excerpt = document.raw_text[:1800]
    prompt = (
        'Decide whether you can reliably answer in the requested output language for the actual summarization task. '
        'Return JSON only with keys compatible, reason, and language. '
        'compatible must be true only if you can produce the final summary fully in the requested language, not in English, '
        'not mixed-language, and without needing translation by the user. '
        'If there is any doubt, set compatible to false. '
        f'Requested output language: {target_name}. '
        f'Source language: {source_name}.\n\n'
        f'Document title: {document.title}\n'
        f'Excerpt:\n{excerpt}'
    )
    text, _check_meta = run_chat(
        plugin,
        [
            ChatMessage(type=ChatMessageType.system, query='You are a strict capability checker. Output must be a single JSON object with valid JSON syntax only.'),
            ChatMessage(type=ChatMessageType.user, query=prompt),
        ],
        options.model_name,
        label='language_compatibility_check',
        log=log,
        verbose=options.verbose_ai_log,
        pace_seconds=_pace_for(options),
        notify=notify,
        should_cancel=should_cancel,
        request_label=options.requested_model_label,
        stats=options.run_stats,
    )
    data = parse_jsonish(text)
    if not data:
        data = {
            'compatible': _extract_json_key_via_regex(text, 'compatible'),
            'language': _extract_json_key_via_regex(text, 'language'),
            'reason': _extract_json_key_via_regex(text, 'reason'),
        }
    compatible = _coerce_bool(data.get('compatible'))
    reported_language = normalize_text_field(data.get('language'))
    reason = normalize_text_field(data.get('reason'))
    if compatible:
        _log(log, f'Language compatibility check passed for {target_name}.')
        return
    detail = reason or _('This model did not confirm reliable output in the requested language.')
    if reported_language:
        detail = f'{detail} Reported language: {reported_language}.'
    raise UnsupportedLanguageError(
        _('The selected model does not appear to support reliable output in %s. Please choose another model.') % target_name
        + ' '
        + detail
    )


def extra_prompt_addition(is_flash: bool, document_type: str) -> str:
    """Return a short, user-configured extra prompt addition.

    This is appended to the user prompt only (never the system prompt).
    """
    mode = 'flash' if is_flash else 'full'
    dt = str(document_type or '').strip().lower()
    if dt == 'fiction':
        suffix = 'fiction'
    elif dt == 'nonfiction':
        suffix = 'nonfiction'
    else:
        suffix = 'other'
    parts = [
        str(pref(f'extra_prompt_{mode}_default', '') or '').strip(),
        str(pref(f'extra_prompt_{mode}_{suffix}', '') or '').strip(),
    ]
    text = ' '.join(p for p in parts if p)
    text = ' '.join(text.split())
    return text[:400]


def summarize_chapter(
    plugin,
    document: ParsedDocument,
    chapter: ChapterSection,
    classification: dict[str, str],
    options: SummaryOptions,
    log: Callable[[str], None] | None = None,
    notify: Callable[[str], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> tuple[str, str, str]:
    paragraph_count = paragraph_target(chapter.word_count, options.base_paragraphs)
    words = chapter.text.split()
    if len(words) <= options.max_chunk_words:
        payload = final_chapter_payload(document, chapter, classification, paragraph_count, chapter.text, options.include_highlight, options)
        text, meta = run_chat(plugin, payload, options.model_name, label=f'chapter_{chapter.index}_direct', log=log, verbose=options.verbose_ai_log, pace_seconds=_pace_for(options), notify=notify, should_cancel=should_cancel, request_label=options.requested_model_label, stats=options.run_stats)
        summary_text, highlight_text = extract_summary_fields(text)
        summary_text, highlight_text = _repair_language_if_needed(
            plugin,
            summary_text,
            highlight_text,
            options,
            log=log,
            notify=notify,
            should_cancel=should_cancel,
            chapter_text=chapter.text,
            is_flash=False
        )
        highlight_text = clean_highlight_text(highlight_text, classification.get('highlight_label', ''))
        return summary_text, highlight_text, meta.model or options.model_name

    chunk_summaries: list[dict[str, object]] = []
    _log(log, f'Chapter {chapter.index} exceeds chunk limit; splitting into map/reduce chunks of up to {options.max_chunk_words} words.')
    for chunk_index, chunk_text in enumerate(chunk_text_by_words(chapter.text, options.max_chunk_words), start=1):
        _check_cancelled(should_cancel)
        prompt = (
            'Summarize this partial chapter chunk for later merging. Return JSON only with keys brief, highlights. '
            'brief should be one compact paragraph. highlights should be a short list of the most important events, ideas, or findings in this chunk.\n\n'
            f'Document type: {classification["document_type"]}\n'
            f'Style instruction: {summary_style_instruction(classification["document_type"])}\n'
            f'Output language: {language_instruction(options)}\n'
            f'Chapter title: {chapter.title}\n'
            f'Chunk number: {chunk_index}\n\n'
            f'Chunk text:\n{chunk_text}'
        )
        text, _chunk_meta = run_chat(plugin, [
            ChatMessage(type=ChatMessageType.system, query='You are a careful reading assistant. Output must be a single JSON object (no markdown, no code fences, no extra text). Always strictly obey the requested output language.'),
            ChatMessage(type=ChatMessageType.user, query=prompt),
        ], options.model_name, label=f'chapter_{chapter.index}_chunk_{chunk_index}', log=log, verbose=options.verbose_ai_log, pace_seconds=_pace_for(options), notify=notify, should_cancel=should_cancel, request_label=options.requested_model_label, stats=options.run_stats)
        parsed = parse_jsonish(text)
        brief = clean_summary(parsed.get('brief') or '')
        if not brief:
            brief = extract_brief_text(text)
        highlights = parsed.get('highlights') or extract_highlight_list(text)
        if isinstance(highlights, str):
            highlights = [highlights]
        chunk_summaries.append({'brief': brief, 'highlights': list(highlights)[:5]})

    merged_input = json.dumps(chunk_summaries, ensure_ascii=False, indent=2)
    payload = [
        ChatMessage(type=ChatMessageType.system, query='You are a careful reading assistant. Output must be a single JSON object (no markdown, no code fences, no extra text). Always strictly obey the requested output language.'),
        ChatMessage(type=ChatMessageType.user, query=(
            'Create the final chapter summary from the chunk summaries below. Return JSON only with keys summary and highlight. '
            f'Write exactly {paragraph_count} well-formed paragraphs in summary, separated by blank lines. '
            f'Use this guidance: {classification["guidance"]}. '
            f'Use this style instruction: {summary_style_instruction(classification["document_type"])}. '
            f'Output language: {language_instruction(options)}. '
            'Stay faithful to the text and do not invent events, claims, or conclusions. '
            f'The highlight should use the perspective implied by this label: {classification["highlight_label"]}.\n\n'
            f'Document title: {document.title}\n'
            f'Chapter title: {chapter.title}\n'
            f'Document type: {classification["document_type"]}\n\n'
            f'Chunk summaries:\n{merged_input}'
        )),
    ]
    text, meta = run_chat(plugin, payload, options.model_name, label=f'chapter_{chapter.index}_merge', log=log, verbose=options.verbose_ai_log, pace_seconds=_pace_for(options), notify=notify, should_cancel=should_cancel, request_label=options.requested_model_label, stats=options.run_stats)
    summary_text, highlight_text = extract_summary_fields(text)
    summary_text, highlight_text = _repair_language_if_needed(
        plugin,
        summary_text,
        highlight_text,
        options,
        log=log,
        notify=notify,
        should_cancel=should_cancel,
        chapter_text=chapter.text,
        is_flash=False
    )
    highlight_text = clean_highlight_text(highlight_text, classification.get('highlight_label', ''))
    return summary_text, highlight_text, meta.model or options.model_name


def final_chapter_payload(document: ParsedDocument, chapter: ChapterSection, classification: dict[str, str], paragraph_count: int, chapter_text: str, include_highlight: bool, options: SummaryOptions):
    highlight_instruction = (
        f'Also provide a concise field named highlight using the label {classification["highlight_label"]}.'
        if include_highlight else
        'Set the highlight field to an empty string.'
    )
    extra = extra_prompt_addition(is_flash=False, document_type=classification.get('document_type', ''))
    extra_block = f"\n\nExtra guidance: {extra}" if extra else ''
    return [
        ChatMessage(type=ChatMessageType.system, query='You are a careful reading assistant. Output must be a single JSON object (no markdown, no code fences, no extra text). Always strictly obey the requested output language.'),
        ChatMessage(type=ChatMessageType.user, query=(
            'Summarize the chapter below. Return JSON only with keys summary and highlight. '
            'Use valid JSON with double-quoted keys and string values only. Do not use Python dict syntax. '
            f'Write exactly {paragraph_count} paragraphs in summary, separated by blank lines. '
            f'Document type: {classification["document_type"]}. '
            f'Guidance: {classification["guidance"]}. '
            f'Style instruction: {summary_style_instruction(classification["document_type"])}. '
            f'Output language: {language_instruction(options)}. '
            'Stay faithful to the text and avoid quoting long passages unless absolutely necessary. '
            f'{highlight_instruction}\n\n'
            f'Document title: {document.title}\n'
            f'Chapter title: {chapter.title}\n'
            f'Chapter length: {chapter.word_count} words\n\n'
            f'Chapter text:\n{chapter_text}'
            f'{extra_block}'
        )),
    ]


_RATE_LIMIT_PATTERNS = ('rate limit', 'free-models-per-min', 'free-models-per-day', 'too many requests', '429')
_MAX_RATE_RETRIES = 3
_RATE_RETRY_WAIT = 65
_MAX_LOCAL_PROVIDER_RETRIES = 5
_LOCAL_PROVIDER_RETRY_WAIT = 10
_last_request_time: float = 0.0


def _pace_for(options: SummaryOptions) -> int:
    """Return the minimum seconds to wait between requests for the current model."""
    if options and is_zero_config_demo_provider(getattr(options, 'provider_name', '')):
        return max(0, options.request_interval_free)
    hint = (options.model_name or options.effective_model_id or '').strip().lower()
    if ':free' in hint or '(free)' in (options.requested_model_label or '').lower():
        return max(0, options.request_interval_free)
    return max(0, options.request_interval_default)


def _check_cancelled(should_cancel: Callable[[], bool] | None = None):
    if callable(should_cancel) and should_cancel():
        raise SummaryCancelledError('Cancelled by user')


def _is_rate_limit_error(msg: str) -> bool:
    lowered = (msg or '').lower()
    return any(p in lowered for p in _RATE_LIMIT_PATTERNS)


def _normalize_provider_error(response) -> str:
    details = str(getattr(response, 'error_details', '') or '').strip()
    exc_text = str(getattr(response, 'exception', '') or '').strip()
    if details.startswith('Traceback'):
        lines = [line.strip() for line in details.splitlines() if line.strip()]
        if lines:
            details = lines[-1]
    if details in {'Provider returned error', 'Exception: Provider returned error'} and exc_text and exc_text != details:
        return f'{details}: {exc_text}'
    return details or exc_text or 'Provider returned error'


def _is_openai_compatible_provider(plugin) -> bool:
    return str(getattr(plugin, 'name', '') or '').strip().lower() == 'openai compatible'


def _looks_like_anthropic_openai_compatible_request(model_name: str) -> bool:
    try:
        from calibre.ai.openai_compatible import backend
        signals = [str(model_name or ''), str(backend.pref('api_url', '') or '')]
        headers = backend.pref('headers', ()) or ()
        for key, val in headers:
            signals.append(str(key or ''))
            signals.append(str(val or ''))
        text = ' '.join(signals).lower()
        return 'anthropic' in text or 'claude' in text
    except Exception:
        return str(model_name or '').strip().lower().startswith('claude')


def _use_anthropic_openai_compatible_override(plugin, model_name: str) -> bool:
    if not pref_bool('anthropic_openai_compatible_override', False):
        return False
    return _is_openai_compatible_provider(plugin) and _looks_like_anthropic_openai_compatible_request(model_name)


def _text_chat_openai_compatible_without_temperature(messages, use_model: str = ''):
    from calibre.ai import ChatResponse
    from calibre.ai.openai_compatible import backend
    from calibre.ai.utils import chat_with_error_handler

    def implementation():
        model_id = use_model or backend.pref('text_model')
        data = {
            'model': model_id,
            'messages': [backend.for_assistant(m) for m in messages],
            'stream': True,
        }
        rq = backend.chat_request(data)
        seen_metadata = False
        for chunk in backend.read_streaming_response(rq, backend.OpenAICompatible.name, timeout=backend.pref('timeout', 120)):
            for response in backend.as_chat_responses(chunk, model_id):
                if response.has_metadata:
                    seen_metadata = True
                yield response
                if response.exception:
                    return
        if not seen_metadata:
            yield ChatResponse(has_metadata=True, provider=backend.OpenAICompatible.name, model=model_id, plugin_name=backend.OpenAICompatible.name)

    yield from chat_with_error_handler(implementation())


def run_chat(
    plugin,
    messages,
    model_name: str,
    label: str = '',
    log: Callable[[str], None] | None = None,
    verbose: bool = False,
    pace_seconds: int = 0,
    notify: Callable[[str], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
    request_label: str = '',
    stats: RunStats | None = None,
):
    global _last_request_time
    requested_model_name = model_name.strip()
    github_fallback_models: list[str] = []
    github_fallback_index = 0
    if _is_github_provider(plugin):
        normalized = _normalize_github_model_name(requested_model_name)
        if normalized and normalized != requested_model_name:
            _log(log, f'Normalized GitHubAI model: {requested_model_name} -> {normalized}')
            requested_model_name = normalized
        github_fallback_models = _github_fallback_model_names(requested_model_name)
        if github_fallback_models:
            requested_model_name = github_fallback_models[0]
    model_label = request_label or requested_model_name or '(provider default)'
    _log(log, f'AI call {label or "chat"}: provider={getattr(plugin, "name", "AI")} model={model_label} messages={len(messages)}.')
    if verbose:
        for idx, message in enumerate(messages, start=1):
            compact = ' '.join((message.query or '').split())
            if len(compact) > 360:
                preview = compact[:220] + ' ... ' + compact[-120:]
            else:
                preview = compact
            _log(log, f'  message {idx} [{getattr(message.type, "name", message.type)}] {len((message.query or "").split())} words: {preview}')
    if pace_seconds > 0:
        elapsed = time.time() - _last_request_time
        wait_needed = pace_seconds - elapsed
        if wait_needed > 0.05:
            if stats is not None:
                stats.paced_wait_seconds += max(0.0, float(wait_needed))
            _log(log, f'Pacing: waiting {wait_needed:.0f}s before next request ({pace_seconds}s interval)…')
            deadline = time.time() + wait_needed
            while True:
                _check_cancelled(should_cancel)
                remaining = deadline - time.time()
                if remaining <= 0.05:
                    break
                if notify:
                    notify(f'Pacing — next request in {remaining:.0f}s…')
                time.sleep(min(1.0, remaining))
    _last_request_time = time.time()
    last_error = None
    attempt = 0
    rate_limit_attempts = 0
    local_provider_timeout_attempts = 0
    is_local = _is_local_provider(plugin)
    while True:
        try:
            attempt += 1
            if stats is not None:
                stats.request_attempts += 1
            _check_cancelled(should_cancel)
            if notify:
                notify(f'Contacting provider — {label or "chat"}…')
            acc = StreamedResponseAccumulator()
            if _use_anthropic_openai_compatible_override(plugin, requested_model_name):
                _log(log, 'Anthropic compatibility override active: omitting temperature for OpenAI compatible provider.')
                response_iter = _text_chat_openai_compatible_without_temperature(messages, requested_model_name)
            else:
                response_iter = plugin.text_chat(messages, requested_model_name)
            for response in response_iter:
                _check_cancelled(should_cancel)
                if response.exception:
                    msg = _normalize_provider_error(response)
                    if _is_rate_limit_error(msg):
                        raise ValueError(msg)

                    # Workaround for models (like Minimax M3) that may return
                    # multiple JSON objects or trailing metadata in a single chunk,
                    # triggering "Extra data" or "JSONDecodeError".
                    # If we have some content, try to use it instead of failing.
                    if any(p in msg for p in ('JSONDecodeError', 'Extra data', 'Unterminated string')):
                        _log(log, f'AI call encountered a JSON parsing error ({msg}); attempting to use content.')
                        # Some providers might still have the partial/failing text in the query attribute
                        try:
                            if hasattr(response, 'query') and response.query:
                                acc.accumulate(response)
                        except Exception:
                            pass
                        break

                    _log(log, f'AI call failed for {label or "chat"}: {msg}')
                    raise ValueError(msg)
                acc.accumulate(response)
            acc.finalize()
            if not acc.messages:
                _log(log, f'AI call returned no messages for {label or "chat"}.')
                if is_local:
                    _log(log, f'Local provider {getattr(plugin, "name", "unknown")} returned no messages. This may indicate a connection issue or model loading problem.')
                raise ValueError('No response returned by AI provider')
            chosen = None
            try:
                for m in reversed(acc.messages):
                    q = (getattr(m, 'query', None) or '').strip()
                    if q:
                        chosen = q
                        break
            except Exception:
                chosen = None
            if not chosen:
                chosen = (acc.messages[-1].query or '').strip()
            actual_model = acc.metadata.model or model_label
            output_word_count = len((chosen or "").split())
            _log(log, f'AI call complete for {label or "chat"}: requested model={model_label}, response model={actual_model}, output words={output_word_count}.')
            if actual_model and actual_model != model_label:
                _log(log, f'Provider returned a different model than requested: requested={model_label} actual={actual_model}.')
            if is_local and output_word_count < 5:
                _log(log, f'Warning: Local provider returned very short response ({output_word_count} words). This may indicate a parsing issue or incomplete response.')
            if stats is not None:
                stats.request_count += 1
            if chosen and chosen != (acc.messages[-1].query or '').strip():
                _log(log, f'Note: provider returned an empty final message for {label or "chat"}; using the last non-empty streamed message instead.')
            return chosen, acc.metadata
        except ValueError as exc:
            message = str(exc)
            # Handle timeout errors for local providers with retry logic
            if is_local and _is_timeout_error(message):
                local_provider_timeout_attempts += 1
                if local_provider_timeout_attempts <= _MAX_LOCAL_PROVIDER_RETRIES:
                    wait = _LOCAL_PROVIDER_RETRY_WAIT * local_provider_timeout_attempts
                    _log(log, f'Local provider timeout detected (attempt {local_provider_timeout_attempts}/{_MAX_LOCAL_PROVIDER_RETRIES}). Waiting {wait}s before retry…')
                    if notify:
                        notify(f'Local provider timeout — retrying in {wait}s…')
                    deadline = time.time() + wait
                    while True:
                        _check_cancelled(should_cancel)
                        remaining = deadline - time.time()
                        if remaining <= 0.05:
                            break
                        time.sleep(min(1.0, remaining))
                    _log(log, f'Retrying AI call {label or "chat"} for local provider…')
                    continue
                else:
                    _log(log, f'Local provider timeout still active after {_MAX_LOCAL_PROVIDER_RETRIES} retries for {label or "chat"}. Giving up.')
                    raise
            if _is_github_provider(plugin) and 'Unavailable model:' in message:
                while github_fallback_index + 1 < len(github_fallback_models):
                    github_fallback_index += 1
                    fallback_model = github_fallback_models[github_fallback_index]
                    if fallback_model == requested_model_name:
                        continue
                    _log(log, f'GitHubAI rejected model {requested_model_name or "(provider default)"}; retrying with {fallback_model}.')
                    requested_model_name = fallback_model
                    model_label = requested_model_name or '(provider default)'
                    break
                else:
                    fallback_model = ''
                if fallback_model:
                    continue
            if not _is_rate_limit_error(str(exc)):
                raise
            last_error = exc
            rate_limit_attempts += 1
            if rate_limit_attempts > _MAX_RATE_RETRIES:
                _log(log, f'Rate limit still active after {_MAX_RATE_RETRIES} retries for {label or "chat"}. Giving up.')
                raise
            if stats is not None:
                stats.rate_limit_retries += 1
            wait = _RATE_RETRY_WAIT * rate_limit_attempts
            _log(log, f'Rate limit hit (attempt {rate_limit_attempts}/{_MAX_RATE_RETRIES}). Waiting {wait}s before retry…')
            deadline = time.time() + wait
            while True:
                _check_cancelled(should_cancel)
                remaining = deadline - time.time()
                if remaining <= 0.05:
                    break
                if notify:
                    notify(f'Rate limited — retrying in {remaining:.0f}s…')
                time.sleep(min(1.0, remaining))
            _log(log, f'Retrying AI call {label or "chat"}…')
    raise last_error or ValueError('Rate limit exceeded after retries')


def _log(log: Callable[[str], None] | None, message: str):
    if callable(log):
        log(message)


def parse_jsonish(text: str) -> dict[str, object]:
    text = text.strip()
    if not text:
        return {}
    for candidate in (text, extract_fenced_json(text), extract_first_json_object(text)):
        if not candidate:
            continue
        try:
            data = json.loads(candidate)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
        try:
            data = ast.literal_eval(candidate)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    objects = extract_all_json_objects(text)
    if objects:
        merged = merge_json_objects(objects)
        if merged:
            return merged
    return {}


def _coerce_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or '').strip().lower()
    return text in {'true', 'yes', 'y', '1', 'compatible', 'supported'}


def extract_fenced_json(text: str) -> str:
    m = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', text, flags=re.DOTALL | re.IGNORECASE)
    return m.group(1) if m else ''


def extract_first_json_object(text: str) -> str:
    start = text.find('{')
    end = text.rfind('}')
    if start > -1 and end > start:
        return text[start:end + 1]
    return ''


def extract_all_json_objects(text: str) -> list[dict[str, object]]:
    decoder = json.JSONDecoder()
    objects: list[dict[str, object]] = []
    index = 0
    while index < len(text):
        start = text.find('{', index)
        if start < 0:
            break
        try:
            data, offset = decoder.raw_decode(text[start:])
        except Exception:
            index = start + 1
            continue
        if isinstance(data, dict):
            objects.append(data)
        index = start + max(1, offset)
    return objects


def merge_json_objects(objects: list[dict[str, object]]) -> dict[str, object]:
    summaries: list[str] = []
    highlights: list[str] = []
    briefs: list[str] = []
    all_highlights: list[str] = []
    for obj in objects:
        summary = normalize_text_field(obj.get('summary'))
        if summary:
            summaries.append(summary)
        highlight = normalize_text_field(obj.get('highlight'))
        if highlight:
            highlights.append(highlight)
        brief = normalize_text_field(obj.get('brief'))
        if brief:
            briefs.append(brief)
        raw_highlights = obj.get('highlights')
        if isinstance(raw_highlights, list):
            all_highlights.extend(normalize_text_field(item) for item in raw_highlights if normalize_text_field(item))
        elif isinstance(raw_highlights, str):
            all_highlights.append(raw_highlights.strip())
    merged: dict[str, object] = {}
    if summaries:
        merged['summary'] = '\n\n'.join(summaries)
    if highlights:
        merged['highlight'] = highlights[0]
    if briefs:
        merged['brief'] = '\n\n'.join(briefs)
    if all_highlights:
        merged['highlights'] = all_highlights[:8]
    return merged


def normalize_text_field(value) -> str:
    if value is None:
        return ''
    if isinstance(value, list):
        parts = [normalize_text_field(item) for item in value]
        return '\n\n'.join(part for part in parts if part)
    text = str(value).strip()
    text = re.sub(r'^```(?:json)?\s*', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\s*```$', '', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def _extract_json_key_via_regex(text: str, key: str) -> str:
    """Extract a JSON string value by key, as last resort.

    Uses a small scanner instead of regex so we don't truncate on embedded
    newlines or quotes.
    """

    def _scan_quoted_string(s: str, start: int) -> tuple[str, int]:
        quote = s[start]
        i = start + 1
        out: list[str] = []
        while i < len(s):
            ch = s[i]
            if ch == '\\':
                if i + 1 >= len(s):
                    break
                nxt = s[i + 1]
                if nxt == 'n':
                    out.append('\n')
                elif nxt == 'r':
                    out.append('\r')
                elif nxt == 't':
                    out.append('\t')
                elif nxt in ('\\', '"', "'"):
                    out.append(nxt)
                else:
                    # Keep unknown escapes as-is
                    out.append(nxt)
                i += 2
                continue
            if ch == quote:
                return ''.join(out), i + 1
            out.append(ch)
            i += 1
        return ''.join(out), i

    if not text:
        return ''
    # Find the key token (case-insensitive) in a JSON-ish blob
    key_pat = re.compile(r'(["\'])' + re.escape(key) + r'\1', re.IGNORECASE)
    m = key_pat.search(text)
    if not m:
        return ''
    i = m.end()
    # Seek ':'
    while i < len(text) and text[i] != ':':
        i += 1
    if i >= len(text) or text[i] != ':':
        return ''
    i += 1
    # Skip whitespace
    while i < len(text) and text[i].isspace():
        i += 1
    if i >= len(text) or text[i] not in ('"', "'"):
        return ''
    value, _end = _scan_quoted_string(text, i)
    return value.strip()


def extract_summary_fields(text: str) -> tuple[str, str]:
    parsed = parse_jsonish(text)

    def pick(obj: dict[str, object], keys: tuple[str, ...], fallback_tokens: tuple[str, ...] = ()) -> str:
        if not obj:
            return ''
        cf = {str(k).casefold(): k for k in obj.keys()}
        for key in keys:
            k = cf.get(str(key).casefold())
            if k is not None:
                return normalize_text_field(obj.get(k))
        if fallback_tokens:
            for k, v in obj.items():
                if any(token in str(k).casefold() for token in fallback_tokens):
                    candidate = normalize_text_field(v)
                    if candidate:
                        return candidate
        # Last resort: first long-ish string value if we are looking for a summary
        if 'summary' in keys:
            for v in obj.values():
                if isinstance(v, str) and len(v.strip()) >= 20:
                    return normalize_text_field(v)
        return ''

    summary = pick(parsed, ('summary', 'Summary', 'chapter_summary', 'chapterSummary', 'text', 'content', 'result'),
                   fallback_tokens=('summary', 'summar', 'content')) if parsed else ''
    highlight = pick(parsed, ('highlight', 'Highlight', 'key', 'key_point', 'keyPoint', 'highlight_text'),
                      fallback_tokens=('highlight', 'highl', 'takeaway', 'finding')) if parsed else ''
    if not summary and parsed:
        summary = build_overview_summary_from_data(parsed)
    if not highlight and parsed:
        highlight = build_overview_highlight_from_data(parsed)
    if summary:
        return clean_summary(summary), clean_summary(highlight)
    objects = extract_all_json_objects(text)
    if objects:
        merged = merge_json_objects(objects)
        summary = pick(merged, 'summary', 'Summary', 'text', 'content')
        highlight = pick(merged, 'highlight', 'Highlight', 'key', 'key_point')
        if not summary:
            summary = build_overview_summary_from_data(merged)
        if not highlight:
            highlight = build_overview_highlight_from_data(merged)
        if summary:
            return clean_summary(summary), clean_summary(highlight)
    # Last-resort: direct regex extraction from JSON-like text
    summary = _extract_json_key_via_regex(text, 'summary')
    highlight = _extract_json_key_via_regex(text, 'highlight')
    if summary:
        return clean_summary(summary), clean_summary(highlight)
    return clean_summary(text), clean_summary(highlight)


_GENERIC_HIGHLIGHT_LABELS = (
    'key event', 'key idea', 'key finding', 'key scene', 'key takeaway',
    'key image', 'key highlight', 'main topic', 'central thesis',
)


def clean_highlight_text(highlight_text: str, highlight_label: str = '') -> str:
    """Strip a redundant leading label from a highlight string.

    Some models restate the highlight label inside the highlight text itself
    (e.g. highlight_label="Key scene" and highlight text="Key scene: Tarikh
    declares..."). Since callers render "**{label}:** {highlight}", this would
    otherwise produce a visible duplicate such as "Key scene: Key scene: ...".
    This also handles the case where the model used a different generic label
    than the one requested (e.g. "Key scene:" when the label is "Divine
    conflict"), and the degenerate case where the highlight is just the label
    repeated back with no real content.
    """
    text = str(highlight_text or '').strip()
    if not text:
        return text
    label = str(highlight_label or '').strip()
    candidates = []
    if label:
        candidates.append(label.casefold())
    candidates.extend(_GENERIC_HIGHLIGHT_LABELS)
    changed = True
    while changed and text:
        changed = False
        lower = text.casefold()
        for cand in candidates:
            if not cand:
                continue
            for sep in (':', ' -', ' —', ' –'):
                prefix = cand + sep
                if lower.startswith(prefix):
                    text = text[len(prefix):].strip()
                    changed = True
                    break
            if changed:
                break
    text = text.strip()
    if label and text.casefold() == label.casefold():
        # The model only repeated the label with no actual content.
        return ''
    return text


def build_overview_summary_from_data(data: dict[str, object]) -> str:
    if not isinstance(data, dict) or not data:
        return ''
    ordered_keys = (
        'main_topic',
        'central_thesis',
        'key_arguments',
        'structure_organization',
        'important_takeaways',
    )
    labels = {
        'main_topic': 'Main topic',
        'central_thesis': 'Central thesis',
        'key_arguments': 'Key arguments',
        'structure_organization': 'Structure',
        'important_takeaways': 'Important takeaways',
    }
    parts: list[str] = []
    for key in ordered_keys:
        value = data.get(key)
        text = normalize_text_field(value)
        if not text:
            continue
        if isinstance(value, list):
            bullets = [f'- {normalize_text_field(item)}' for item in value if normalize_text_field(item)]
            if bullets:
                parts.append(f'{labels[key]}:\n' + '\n'.join(bullets))
        else:
            parts.append(f'{labels[key]}: {text}')
    return '\n\n'.join(part for part in parts if part)


def build_overview_highlight_from_data(data: dict[str, object]) -> str:
    if not isinstance(data, dict) or not data:
        return ''
    for key in ('key_idea', 'key_takeaway', 'main_topic', 'central_thesis'):
        text = normalize_text_field(data.get(key))
        if text:
            return text
    return ''


def extract_brief_text(text: str) -> str:
    parsed = parse_jsonish(text)
    brief = normalize_text_field(parsed.get('brief') if parsed else '')
    if brief:
        return clean_summary(brief)
    objects = extract_all_json_objects(text)
    if objects:
        merged = merge_json_objects(objects)
        brief = normalize_text_field(merged.get('brief') or merged.get('summary'))
        if brief:
            return clean_summary(brief)
    brief = _extract_json_key_via_regex(text, 'brief')
    if brief:
        return clean_summary(brief)
    return clean_summary(text)


def extract_highlight_list(text: str) -> list[str]:
    parsed = parse_jsonish(text)
    if parsed:
        raw = parsed.get('highlights')
        if isinstance(raw, list):
            return [clean_summary(item) for item in raw if clean_summary(item)]
        if isinstance(raw, str) and raw.strip():
            return [clean_summary(raw)]
    objects = extract_all_json_objects(text)
    merged = merge_json_objects(objects) if objects else {}
    raw = merged.get('highlights') or []
    if isinstance(raw, list):
        return [clean_summary(item) for item in raw if clean_summary(item)]
    raw = _extract_json_key_via_regex(text, 'highlights')
    if raw:
        return [clean_summary(raw)]
    return []


def paragraph_target(word_count: int, base: int) -> int:
    if word_count <= 1200:
        return max(1, base - 1)
    if word_count <= 5000:
        return max(1, base)
    if word_count <= 10000:
        return min(4, base + 1)
    return min(6, base + 2)


def chunk_text_by_words(text: str, max_words: int):
    words = text.split()
    start = 0
    overlap = min(120, max_words // 8)
    while start < len(words):
        end = min(len(words), start + max_words)
        yield ' '.join(words[start:end])
        if end >= len(words):
            break
        start = max(start + 1, end - overlap)


def heuristic_document_type(document: ParsedDocument) -> str:
    title = (document.title or '').lower()
    text = document.raw_text[:6000].lower()
    chapter_titles = ' '.join(chapter.title.lower() for chapter in document.chapters[:12])
    if any(word in title for word in ('news', 'notícias', 'noticias', 'times', 'gazette', 'daily')):
        return 'news'
    if any(word in chapter_titles for word in ('opinion', 'editorial', 'briefing', 'analysis', 'newsletter')):
        return 'news'
    if 'sumário' in text or 'contents' in text or len(document.chapters) >= 5:
        if any(word in text for word in ('chapter', 'capítulo', 'capitulo', 'parte')):
            return 'nonfiction'
    if any(word in text for word in ('interview', 'reported by', 'updated at', 'published on')):
        return 'article'
    if any(word in text for word in ('once upon', 'she said', 'he said', 'they said', 'novel', 'prologue', 'epilogue')):
        return 'fiction'
    return 'nonfiction'


def language_instruction(options: SummaryOptions) -> str:
    target = str(options.target_language or '').strip().lower()
    source_code = str(options.source_language_code or '').strip().lower()
    source_name = str(options.source_language_name or '').strip()
    source_is_english = source_code.startswith('en') or 'english' in source_name.lower()
    if target in {'english', 'en'}:
        return 'Write both summary and highlight in English only. This is a strict requirement.'
    if target not in {'', 'same-as-source'}:
        target_name = (options.target_language_name or target or '').strip() or 'the requested output language'
        forbid_english = 'english' not in target_name.lower()
        extra = ' Do not use English.' if forbid_english else ''
        return f'Write both summary and highlight in {target_name} only.{extra} This is a strict requirement.'
    if source_is_english:
        return 'Write both summary and highlight in English only. This is a strict requirement.'
    if source_name and 'unknown' not in source_name.lower():
        return f'Write both summary and highlight in {source_name} only. Do not translate into English. This is a strict requirement.'
    return 'Write both summary and highlight ONLY in the same language as the source text. If the source text is not English, do not translate into English. This is a strict requirement.'


def render_language_line(options: SummaryOptions | None) -> str:
    if options is None:
        return f'- Source language: {_("Unknown")} | Output language: {_("Unknown")}'
    source_name = (options.source_language_name or _('Unknown')).strip()
    if str(options.target_language or '').strip() in {'', 'same-as-source'}:
        output_name = _('Same as source')
    else:
        output_name = (options.target_language_name or options.target_language or _('Unknown')).strip()
    return f'- Source language: {source_name} | Output language: {output_name}'


def summary_style_instruction(document_type: str) -> str:
    return {
        'fiction': 'Focus on plot movement, character motives, tension, consequences, and meaningful emotional or thematic shifts.',
        'nonfiction': 'Focus on the central claim, supporting reasoning, examples, and what the reader is meant to understand or apply.',
        'news': 'Focus on the core development, who is involved, what changed, the evidence presented, and why it matters; stay neutral.',
        'article': 'Focus on the argument, reporting, evidence, and the clearest takeaway for the reader.',
        'magazine': 'Focus on the narrative angle, notable examples, and the most memorable takeaway or perspective.',
        'reference': 'Focus on definitions, structure, factual content, and reusable information rather than narrative flow.',
        'poetry': 'Focus on imagery, voice, mood, and the strongest interpretive thread without forcing literal plot where none exists.',
        'mixed': 'Adapt to the material section by section and emphasize what is most important for a reader to retain.',
    }.get(document_type, 'Focus on the main ideas, strongest examples, and what the reader should remember.')


def default_highlight_label(document_type: str) -> str:
    return {
        'fiction': 'Key event',
        'news': 'Key finding',
        'article': 'Key takeaway',
        'magazine': 'Key takeaway',
        'reference': 'Key idea',
        'poetry': 'Key image',
        'mixed': 'Key highlight',
    }.get(document_type, 'Key idea')


def default_guidance(document_type: str) -> str:
    return {
        'fiction': 'Emphasize plot progression, character changes, and emotional or thematic turns without quoting at length.',
        'news': 'Emphasize facts, developments, and implications while staying concrete and neutral.',
        'article': 'Emphasize the main argument, evidence, and practical takeaway.',
        'magazine': 'Emphasize the angle, examples, and the most memorable takeaway.',
        'reference': 'Emphasize definitions, structure, and the most reusable information.',
        'poetry': 'Emphasize imagery, mood, and the central insight of the section.',
        'mixed': 'Adapt to the material and highlight what is most important for a reader to remember.',
    }.get(document_type, 'Emphasize the main ideas, examples, and what a reader should retain.')


def clean_summary(text) -> str:
    text = '' if text is None else str(text)
    text = text.strip()
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text


def render_summary(
    document: ParsedDocument,
    classification: dict[str, str],
    chapter_summaries: list[ChapterSummary],
    provider_name: str,
    model_name: str,
    requested_model: str,
    options: SummaryOptions | None = None,
    stats: RunStats | None = None,
) -> str:
    # Extract stats values for integration into metadata
    duration_label = _format_duration(0.0)
    if stats is not None and stats.started_at:
        if stats.ended_at and stats.ended_at >= stats.started_at:
            duration_label = _format_duration(stats.ended_at - stats.started_at)
        else:
            duration_label = 'Ongoing'
    requests = int(stats.request_count or 0) if stats else 0
    attempts = int(stats.request_attempts or 0) if stats else 0
    chapters_detected = int(getattr(stats, 'chapter_count', 0) or 0) if stats else 0

    # Extract options values for metadata
    pace_used = _pace_for(options) if options else 0
    pace_free = int(options.request_interval_free) if options else 0
    pace_other = int(options.request_interval_default) if options else 0
    reasoning = (options.reasoning_strategy or 'auto') if options else 'auto'
    chunk_size = int(options.max_chunk_words) if options else 0
    base_paragraphs = int(options.base_paragraphs) if options else 0

    lines = [
        f'# {document.title}',
        '',
        f'- Source: {document.source_path}',
        f'- Format: {document.source_format or "unknown"} | Document type: {classification["document_type"]}',
        render_language_line(options),
        f'- Base paragraphs per chapter: {base_paragraphs} | Request pace used: {int(pace_used)}s (free={pace_free}s, other={pace_other}s)',
        f'- Reasoning strategy: {reasoning} | Chunk size: {chunk_size} words',
        f'- Provider: {provider_name} | Requested model: {requested_model}',
        f'- Actual model: {model_name}',
        f'- Requests: {requests} (attempts: {attempts})',
        f'- Duration: {duration_label} | Generated: {datetime.now().strftime("%Y-%m-%d %H:%M")}',
        f'- Chapters detected: {chapters_detected}',
        '',
    ]
    for chapter in chapter_summaries:
        lines.extend([
            f'## {chapter.index}. {chapter.title}',
            '',
            chapter.summary.strip(),
            '',
        ])
        if chapter.highlight.strip() and (options is None or options.include_highlight):
            lines.extend([
                f'**{classification["highlight_label"]}:** {chapter.highlight.strip()}',
                '',
            ])
    return '\n'.join(lines).strip() + '\n'


def _format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds or 0))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f'{h:d}:{m:02d}:{s:02d}'
    return f'{m:d}:{s:02d}'


def render_run_stats(options: SummaryOptions | None, stats: RunStats | None, flash_mode: bool = False) -> list[str]:
    if options is None and stats is None:
        return []
    lines: list[str] = []
    if stats is not None and (stats.request_count or stats.request_attempts or stats.started_at):
        duration_label = _format_duration(0.0)
        if stats.started_at:
            if stats.ended_at and stats.ended_at >= stats.started_at:
                duration_label = _format_duration(stats.ended_at - stats.started_at)
            else:
                duration_label = 'Ongoing'
        # Chapters detected only shown for chapter summaries, not flash summaries
        if not flash_mode:
            lines.extend([
                '',
                '- Chapters detected: %d' % int(getattr(stats, 'chapter_count', 0) or 0),
            ])
        lines.extend([
            '- Requests: %d (attempts: %d)' % (int(stats.request_count or 0), int(stats.request_attempts or 0)),
            '- Duration: %s' % duration_label,
        ])
        if stats.paced_wait_seconds:
            lines.append('- Pacing wait: %s' % _format_duration(stats.paced_wait_seconds))
        if stats.rate_limit_retries:
            lines.append('- Rate-limit retries: %d' % int(stats.rate_limit_retries))
    if options is not None:
        if lines and lines[-1] != '':
            # Keep a visual break between header info and settings.
            lines.append('')
        elif not lines:
            lines.append('')
        pace_used = _pace_for(options)
        lines.extend([
            '- Request pace used: %ss (free=%ss, other=%ss)' % (int(pace_used), int(options.request_interval_free), int(options.request_interval_default)),
            '- Reasoning strategy: %s' % (options.reasoning_strategy or 'auto'),
            '- Chunk size: %d words' % int(options.max_chunk_words),
            '- Base paragraphs per chapter: %d' % int(options.base_paragraphs),
        ])
    return lines
