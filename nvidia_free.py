#!/usr/bin/env python3
"""NVIDIA backend for Chapter Summarizer.

Uses NVIDIA's OpenAI-compatible API. No API key required for basic usage.
"""

from __future__ import annotations

__license__ = 'GPL v3'
__copyright__ = '2026, Comfy.n'
__docformat__ = 'restructuredtext en'

import html as html_lib
import json
import os
import re
import threading
import time
from collections.abc import Iterable, Iterator, Sequence
from typing import Any
from urllib.parse import quote
from urllib.request import Request, urlopen

from calibre.ai import ChatMessage, ChatMessageType, ChatResponse
from calibre.ai.utils import chat_with_error_handler, read_streaming_response
from calibre.constants import cache_dir
from calibre.utils.browser import Browser

try:
    load_translations()
except NameError:
    pass

from calibre_plugins.chapter_summarizer.config import pref, set_pref


NVIDIA_FREE_PROXY_URL = 'https://nvidia-proxy.no-op'
NVIDIA_FREE_DEFAULT_MODEL = 'nvidia/nemotron-3-super-120b-a12b'

NVIDIA_FREE_API_URL = 'https://integrate.api.nvidia.com'
NVIDIA_FREE_API_DEFAULT_MODEL = NVIDIA_FREE_DEFAULT_MODEL

_MODELS_RICH_CACHE: list[dict[str, Any]] | None = None
_MODELS_RICH_CACHE_TIME = 0

_MODELS_IDS_CACHE: list[str] | None = None
_MODELS_IDS_CACHE_TIME = 0
_MODELS_CACHE_TTL = 86400  # 24 hours

# Build metadata cache (session-scoped)
_BUILD_METADATA_CACHE: dict[str, dict[str, Any]] | None = None
_BUILD_METADATA_CACHE_TIME = 0
_BUILD_MODEL_URL = 'https://build.nvidia.com/models'
# The HTML listing at the URL above is behind an AWS WAF JavaScript challenge and
# cannot be fetched from a plain HTTP client, so live metadata is read from the
# per-model ``.md`` mirrors instead. See _fetch_model_markdown_metadata.
_BUILD_USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
)
_BUILD_REQUEST_TIMEOUT = 12
_BUILD_MAX_WORKERS = 12
_NGC_CATALOG_URL = 'https://api.ngc.nvidia.com/v2/search/catalog/resources'
_NGC_CATALOG_ORG = 'qc69jvmznzxy'
_NGC_CATALOG_LABEL = 'nim_type_preview'

_BUILD_WARMUP_STATE = 0
_BUILD_WARMUP_LOCK = threading.Lock()

# Parsed form of the bundled snapshot. Parsing is pure disk + CPU work with no
# network access, but it is pointless to redo it on every dialog open.
_SNAPSHOT_CACHE: dict[str, dict[str, Any]] | None = None


# Identity used for the per-plugin config folder and for locating the plugin zip
# when extracting bundled resources.
PLUGIN_CONFIG_DIRNAME = 'ChapterSummarizer'
PLUGIN_ZIP_NAMES = ('ChapterSummarizer.zip', 'Chapter Summarizer.zip', 'chapter_summarizer.zip')

# Bump whenever resources/nvidia_build_snapshot.json changes. The snapshot is
# extracted into the plugin's config directory on first use; without this stamp
# that copy would shadow the bundled file forever and users would keep seeing an
# outdated catalogue after upgrading.
SNAPSHOT_VERSION = 3


def is_debug_enabled() -> bool:
    try:
        import os
        return bool(os.environ.get('CHAPTER_SUMMARIZER_DEBUG_ENABLED'))
    except Exception:
        return False


def debug_log(message: str) -> None:
    if not is_debug_enabled():
        return
    try:
        print(f'[Chapter Summarizer][NVIDIA] {message}')
    except Exception:
        pass


def _load_bundled_build_snapshot() -> dict[str, dict[str, Any]]:
    """Load bundled NVIDIA Build metadata snapshot.

    This provides a good initial UX (Free filter + Newest/Oldest sorting) even if
    live scraping fails or is slow.
    """
    debug_log('Loading bundled snapshot')
    global _SNAPSHOT_CACHE
    if _SNAPSHOT_CACHE is not None:
        return _SNAPSHOT_CACHE

    from calibre.utils.config import config_dir
    import zipfile

    # Setup destination path (must live under config_dir/plugins/<plugin>)
    plugin_config_dir = os.path.join(config_dir, 'plugins', PLUGIN_CONFIG_DIRNAME)
    snapshot_path = os.path.join(plugin_config_dir, 'nvidia_build_snapshot.json')

    debug_log(f'Config dir is: {config_dir}')
    debug_log(f'Plugin config dir will be: {plugin_config_dir}')
    debug_log(f'Snapshot path will be: {snapshot_path}')

    # Reuse the extracted copy only when it was written by this version of the
    # code; a stale copy from an earlier release must be replaced.
    data = None
    if os.path.exists(snapshot_path):
        debug_log(f'Found extracted snapshot at {snapshot_path}')
        try:
            with open(snapshot_path, 'r', encoding='utf-8') as f:
                candidate = json.load(f)
            version = candidate.get('version') if isinstance(candidate, dict) else None
            if version == SNAPSHOT_VERSION:
                data = candidate
                debug_log('Extracted snapshot version matches; using it')
            else:
                debug_log(f'Extracted snapshot version {version!r} != {SNAPSHOT_VERSION}; re-extracting')
        except Exception as e:
            debug_log(f'Failed to load snapshot from config dir: {e}')

    if data is None:
        # Extract snapshot from plugin resources to config dir
        debug_log(f'Snapshot not found, extracting from plugin resources')
        try:
            # Create plugin config directory
            os.makedirs(plugin_config_dir, exist_ok=True)
            debug_log(f'Created plugin config directory: {plugin_config_dir}')

            # Get plugin directory
            plugin_dir = os.path.dirname(os.path.abspath(__file__))
            debug_log(f'Plugin dir is: {plugin_dir}')

            # Try direct copy from plugin installation directory (for unzipped dev mode)
            source_path = os.path.join(plugin_dir, 'resources', 'nvidia_build_snapshot.json')
            debug_log(f'Checking for snapshot at: {source_path}')

            if os.path.exists(source_path):
                import shutil
                shutil.copy2(source_path, snapshot_path)
                debug_log(f'Copied snapshot from plugin dir to config dir')
                with open(snapshot_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                debug_log(f'Snapshot loaded successfully after extraction')
            else:
                # Try extracting from plugin ZIP (normal/portable installs)
                debug_log(f'Snapshot not found in plugin dir, trying ZIP extraction')
                zip_candidates = [
                    os.path.join(config_dir, 'plugins', name) for name in PLUGIN_ZIP_NAMES
                ] + [plugin_dir + '.zip']
                debug_log(f'Will try zips: {zip_candidates}')
                for zip_path in zip_candidates:
                    debug_log(f'Checking for plugin ZIP at: {zip_path}')
                    if os.path.exists(zip_path):
                        debug_log(f'Found plugin ZIP at: {zip_path}')
                        try:
                            with zipfile.ZipFile(zip_path, 'r') as zf:
                                all_names = zf.namelist()
                                debug_log(f'ZIP contains {len(all_names)} files')
                                snapshot_in_zip = 'resources/nvidia_build_snapshot.json'
                                if snapshot_in_zip in all_names:
                                    debug_log(f'Found {snapshot_in_zip} in ZIP')
                                    with zf.open(snapshot_in_zip) as src:
                                        with open(snapshot_path, 'wb') as dst:
                                            dst.write(src.read())
                                    debug_log(f'Extracted snapshot from ZIP to {snapshot_path}')
                                    with open(snapshot_path, 'r', encoding='utf-8') as f:
                                        data = json.load(f)
                                    debug_log(f'Snapshot loaded successfully after ZIP extraction')
                                    break
                                else:
                                    debug_log(f'{snapshot_in_zip} not found in ZIP, available files: {all_names[:10]}')
                        except Exception as e:
                            debug_log(f'Failed to extract from ZIP {zip_path}: {e}')
                            continue
                else:
                    debug_log(f'Snapshot not found in any ZIP file')
                    return {}
        except Exception as e:
            debug_log(f'Failed to extract snapshot from plugin resources: {e}')
            return {}

    models = data.get('models') if isinstance(data, dict) else None
    if not isinstance(models, dict):
        debug_log(f'No models dict in snapshot, got: {type(models)}')
        return {}
    debug_log(f'Bundled snapshot has {len(models)} models')

    out: dict[str, dict[str, Any]] = {}
    for model_id, meta in models.items():
        if not isinstance(meta, dict):
            continue
        mid = str(model_id or '').strip().lower()
        if not mid:
            continue
        use_cases = meta.get('use_cases')
        out[mid] = {
            'id': mid,
            'name': str(meta.get('display_name') or meta.get('name') or mid).strip() or mid,
            'publisher': str(meta.get('publisher') or '').strip(),
            'publisher_slug': str(meta.get('publisher') or '').strip().lower(),
            'build_slug': str(mid.split('/', 1)[1] if '/' in mid else '').strip(),
            'build_url': str(meta.get('build_url') or meta.get('source_url') or '').strip(),
            'is_free': bool(meta.get('free_endpoint')),
            'has_partner_endpoint': bool(meta.get('partner_endpoint')),
            'is_downloadable': bool(meta.get('download_available')),
            'use_cases': [str(x).strip() for x in use_cases if str(x).strip()] if isinstance(use_cases, (list, tuple)) else [],
            'updated_label': '',
            'updated_date': str(meta.get('updated_date') or ''),
            'created': str(meta.get('created') or ''),
            'description': str(meta.get('description') or '').strip(),
        }
    debug_log(f'Loaded bundled snapshot entries={len(out)}')
    _SNAPSHOT_CACHE = out
    return out


def _merge_build_metadata_dicts(old: dict[str, dict[str, Any]], new: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    if not old:
        return dict(new or {})
    if not new:
        return dict(old)
    merged: dict[str, dict[str, Any]] = dict(old)
    for mid, meta in new.items():
        if not isinstance(meta, dict):
            continue
        prev = merged.get(mid) or {}
        out = dict(prev)
        for k, v in meta.items():
            if k == 'use_cases':
                if isinstance(prev.get(k), list) or isinstance(v, list):
                    out[k] = [str(x).strip() for x in (prev.get(k) or []) + (v or []) if str(x).strip()]
            elif v not in (None, '', []):
                out[k] = v
        merged[mid] = out
    return merged


def _write_build_snapshot(snapshot: dict[str, Any]) -> bool:
    """Atomically persist the current public NVIDIA Build preview catalogue."""
    from calibre.utils.config import config_dir

    plugin_dir = os.path.dirname(os.path.abspath(__file__))
    paths = [
        os.path.join(config_dir, 'plugins', PLUGIN_CONFIG_DIRNAME, 'nvidia_build_snapshot.json'),
        os.path.join(plugin_dir, 'resources', 'nvidia_build_snapshot.json'),
    ]
    encoded = json.dumps(snapshot, ensure_ascii=False, indent=1, sort_keys=True).encode('utf-8')
    success = False
    for path in paths:
        temp_path = f'{path}.new'
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(temp_path, 'wb') as f:
                f.write(encoded)
            os.replace(temp_path, path)
            debug_log(f'Updated Build snapshot: {path}')
            success = True
        except Exception as err:
            debug_log(f'Failed to update Build snapshot {path}: {err}')
            try:
                os.remove(temp_path)
            except Exception:
                pass
    return success


def _refresh_build_snapshot() -> int:
    """Fetch and persist NVIDIA's current free preview endpoint catalogue."""
    query = {
        'query': f'orgName:"{_NGC_CATALOG_ORG}" AND label:"{_NGC_CATALOG_LABEL}"',
        'filters': [],
        'orderBy': [{'field': 'score', 'value': 'DESC'}],
        'page': 0,
        'pageSize': 96,
        'scoredSize': 96,
    }
    encoded_query = quote(json.dumps(query, separators=(',', ':')), safe='')
    url = f'{_NGC_CATALOG_URL}?resource-type=ENDPOINT&q={encoded_query}&group-labels-by-labelset=true'
    try:
        request = Request(url, headers={
            'Accept': 'application/json',
            'User-Agent': _BUILD_USER_AGENT,
        })
        with urlopen(request, timeout=_BUILD_REQUEST_TIMEOUT) as response:
            payload = json.loads(response.read().decode('utf-8'))
    except Exception as err:
        debug_log(f'Build catalogue refresh failed: {err}')
        return 0

    resources = []
    for group in payload.get('results', ()) if isinstance(payload, dict) else ():
        if isinstance(group, dict):
            resources.extend(group.get('resources') or ())
    models: dict[str, dict[str, Any]] = {}
    for resource in resources:
        if not isinstance(resource, dict):
            continue
        resource_id = str(resource.get('resourceId') or '').strip().lower()
        prefix = f'{_NGC_CATALOG_ORG}/'
        if not resource_id.startswith(prefix):
            continue
        slug = resource_id[len(prefix):].strip()
        if not slug or '/' in slug:
            continue
        labels = resource.get('labels')
        publisher = ''
        if isinstance(labels, list):
            for label in labels:
                if not isinstance(label, dict) or str(label.get('key') or '').lower() != 'publisher':
                    continue
                values = label.get('unresolvedValues') or label.get('values') or ()
                if isinstance(values, (list, tuple)) and values:
                    publisher = str(values[0]).strip().lower()
                elif values:
                    publisher = str(values).strip().lower()
                break
        if not publisher:
            continue
        model_id = f'{publisher}/{slug}'
        models[model_id] = {
            'id': model_id,
            'build_slug': slug,
            'build_url': f'https://build.nvidia.com/{publisher}/{slug}',
            'created': str(resource.get('dateCreated') or ''),
            'description': str(resource.get('description') or '').strip(),
            'display_name': str(resource.get('displayName') or resource.get('name') or slug).strip(),
            'download_available': False,
            'free_endpoint': True,
            'partner_endpoint': False,
            'publisher': publisher,
            'publisher_slug': publisher,
            'updated_date': str(resource.get('dateModified') or ''),
            'use_cases': [],
        }
    expected = payload.get('resultTotal') if isinstance(payload, dict) else None
    if not models or (isinstance(expected, int) and len(models) != expected):
        debug_log(f'Build catalogue refresh rejected entries={len(models)} expected={expected}')
        return 0
    snapshot = {
        'models': models,
        'source': 'NVIDIA NGC catalog preview endpoint (nim_type_preview)',
        'version': SNAPSHOT_VERSION,
    }
    if _write_build_snapshot(snapshot):
        global _SNAPSHOT_CACHE
        _SNAPSHOT_CACHE = None
        return len(models)
    return 0


def _models_cache_path() -> str:
    # Mirror calibre's OpenRouter cache location style
    return os.path.join(cache_dir(), 'ai', 'NVIDIA-models-v1.json')


def _read_cached_models_json(path: str) -> dict[str, Any] | None:
    try:
        with open(path, 'rb') as f:
            raw = f.read()
        return json.loads(raw.decode('utf-8'))
    except Exception:
        return None


def _write_cached_models_json(path: str, data: dict[str, Any]) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'wb') as f:
            f.write(json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2).encode('utf-8'))
    except Exception:
        pass


def _slug_to_model_id(publisher: str, slug: str) -> str:
    publisher = str(publisher or '').strip().lower()
    slug = str(slug or '').strip().lower().replace('_', '.')
    if not publisher or not slug:
        return ''
    return f'{publisher}/{slug}'


def _candidate_model_ids(publisher: str, slug: str) -> tuple[str, ...]:
    publisher = str(publisher or '').strip().lower()
    slug = str(slug or '').strip().lower()
    if not publisher or not slug:
        return ()
    variants = []
    # Generate all possible separator combinations
    separators = ('.', '-', '_')
    for sep1 in separators:
        for sep2 in separators:
            for sep3 in separators:
                # Replace all separators with each combination
                test_slug = slug
                test_slug = test_slug.replace('.', sep1)
                test_slug = test_slug.replace('-', sep2)
                test_slug = test_slug.replace('_', sep3)
                if test_slug and test_slug not in variants:
                    variants.append(test_slug)
    # Also include the original slug
    if slug not in variants:
        variants.append(slug)
    # Build full model IDs
    model_ids = []
    for candidate_slug in variants:
        candidate_slug = candidate_slug.strip()
        if candidate_slug:
            model_ids.append(f'{publisher}/{candidate_slug}')
    seen = []
    for item in model_ids:
        if item not in seen:
            seen.append(item)
    return tuple(seen)


def _canonical_slug_variants(slug: str) -> tuple[str, ...]:
    slug = str(slug or '').strip().lower()
    if not slug:
        return ()
    variants = []
    queue = [slug]
    separators = ('.', '-', '_')
    while queue:
        current = queue.pop(0)
        if current in variants:
            continue
        variants.append(current)
        for src in separators:
            for dst in separators:
                if src == dst:
                    continue
                candidate = current.replace(src, dst)
                if candidate not in variants and candidate not in queue:
                    queue.append(candidate)
    return tuple(variants)


def _candidate_model_ids_from_model_id(model_id: str) -> tuple[str, ...]:
    model_id = str(model_id or '').strip().lower()
    if '/' not in model_id:
        return ()
    publisher, slug = model_id.split('/', 1)
    # Use the improved candidate generation that handles all separator combinations
    return _candidate_model_ids(publisher, slug)


def _extract_label_values(labels: list[str], prefix: str) -> list[str]:
    values = []
    for label in labels:
        parts = str(label or '').split(':', 2)
        if len(parts) == 3 and parts[0] == prefix:
            value = str(parts[2] or '').strip()
            if value:
                values.append(value)
    return values


def _normalise_build_markup(html: str) -> str:
    text = str(html or '')
    for _ in range(2):
        text = html_lib.unescape(text)
    replacements = {
        '\\"': '"',
        "\\'": "'",
        '\\/': '/',
        '\\u0026': '&',
        '\\u003c': '<',
        '\\u003e': '>',
        '\xa0': ' ',
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    return text


def _extract_description_from_card_block(block: str) -> str:
    patterns = (
        r'line-clamp-2[^>]{0,400}>\s*(?P<desc>[^<]{10,600})\s*<',
        r'line-clamp-2.{0,400}?children["\']?\s*[:=]\s*["\'](?P<desc>[^"\']{10,600})["\']',
        r'leading-body.{0,400}?children["\']?\s*[:=]\s*["\'](?P<desc>[^"\']{10,600})["\']',
    )
    for pattern in patterns:
        match = re.search(pattern, block, re.IGNORECASE | re.DOTALL)
        if match:
            desc = ' '.join(str(match.group('desc') or '').split())
            if desc:
                return desc
    return ''


def _extract_models_from_listing_html(html: str) -> dict[str, dict[str, Any]]:
    """Extract NVIDIA Build card metadata from live listing markup.

    NVIDIA currently serves either rendered HTML or a streamed React payload.
    This parser supports both by normalising the markup and then extracting
    card-local publisher/model/badge information.
    """
    markup = _normalise_build_markup(html)
    if not markup:
        return {}

    models: dict[str, dict[str, Any]] = {}
    artifact_positions = [
        match.start()
        for match in re.finditer(r'artifact-card(?!-publisher-link)', markup, re.IGNORECASE)
    ]

    for pos in artifact_positions:
        block = markup[max(0, pos - 1800):min(len(markup), pos + 3600)]
        href_match = re.search(
            r'href["\']?\s*[:=]\s*["\']?(?P<href>(?:https?://build\.nvidia\.com)?/(?P<publisher>[a-z0-9-]+)/(?P<slug>[a-z0-9._-]+))',
            block,
            re.IGNORECASE,
        )
        if not href_match:
            continue

        publisher = str(href_match.group('publisher') or '').strip().lower()
        slug = str(href_match.group('slug') or '').strip().lower()
        if not publisher or not slug:
            continue

        href = str(href_match.group('href') or '').strip()
        build_url = href if href.startswith('http') else f'https://build.nvidia.com/{publisher}/{slug}'
        has_free = 'Free Endpoint' in block
        has_partner = 'Partner Endpoint' in block or 'Partner API' in block
        has_download = 'Downloadable' in block or 'Download Available' in block
        description = _extract_description_from_card_block(block)

        for candidate_id in _candidate_model_ids(publisher, slug):
            existing = models.get(candidate_id) or {}
            models[candidate_id] = {
                'id': candidate_id,
                'name': slug,
                'publisher': publisher,
                'displayName': slug,
                'description': description or str(existing.get('description') or '').strip(),
                'is_free': bool(existing.get('is_free')) or has_free,
                'has_partner_endpoint': bool(existing.get('has_partner_endpoint')) or has_partner,
                'is_downloadable': bool(existing.get('is_downloadable')) or has_download,
                'build_url': build_url,
            }

    return models


def _extract_build_artifacts(html: str) -> list[dict[str, Any]]:
    """DEPRECATED: Extract model data from old JSON artifact format.

    This function is kept for backwards compatibility but the new listing pages
    don't use artifacts anymore. Use _extract_models_from_listing_html() instead.
    """
    return []


def _fetch_model_markdown_metadata(model_id: str) -> dict[str, Any]:
    """Read a single model's metadata from its plain-text ``.md`` mirror.

    ``build.nvidia.com`` serves the model catalogue through a JavaScript app
    guarded by an AWS WAF challenge, so the HTML listing cannot be fetched from
    a plain HTTP client. The ``.md`` mirror of each model page is served without
    the challenge and starts with a YAML frontmatter block:

        ---
        title: "nemotron-3-ultra-550b-a55b"
        publisher: "nvidia"
        type: "endpoint"
        updated: "2026-06-04T11:22:41.002Z"
        description: "..."
        canonical: "https://build.nvidia.com/nvidia/nemotron-3-ultra-550b-a55b"
        ---

    Endpoint badges (Free/Partner/Downloadable) are *not* present there, so
    those keep coming from the bundled snapshot; this call only supplies the
    dates and descriptions used for sorting and the details pane.
    """
    if '/' not in model_id:
        return {}
    publisher_slug, build_slug = model_id.split('/', 1)
    url = f'https://build.nvidia.com/{publisher_slug}/{build_slug}.md'
    try:
        br = Browser()
        br.set_user_agent(_BUILD_USER_AGENT)
        br.set_handle_gzip(True)
        br.set_current_header('Accept', 'text/markdown,text/plain,*/*')
        raw = br.open(url, timeout=_BUILD_REQUEST_TIMEOUT).read().decode('utf-8', 'replace')
    except Exception:
        debug_log(f'Markdown metadata fetch failed for {model_id}')
        return {}

    head = raw[:2000]
    if not head.lstrip().startswith('---'):
        return {}

    def field(name: str) -> str:
        match = re.search(rf'^{name}:\s*"?(?P<v>[^"\n]*)"?\s*$', head, re.MULTILINE)
        return str(match.group('v')).strip() if match else ''

    if field('type').lower() not in ('', 'endpoint'):
        return {}

    out: dict[str, Any] = {
        'id': model_id,
        'publisher_slug': publisher_slug,
        'build_slug': build_slug,
        'build_url': f'https://build.nvidia.com/{publisher_slug}/{build_slug}',
    }
    if publisher := field('publisher'):
        out['publisher'] = publisher
        out['publisher_slug'] = publisher.lower()
    if updated := field('updated'):
        out['updated_date'] = updated
    if description := field('description'):
        out['description'] = description
    if title := field('title'):
        out['name'] = title
    return out


def _fetch_build_page_metadata(model_ids: Sequence[str] | None = None) -> dict[str, dict[str, Any]]:
    """Enrich the bundled snapshot with live per-model metadata.

    Runs the ``.md`` requests concurrently and bounded, so a full refresh takes
    seconds rather than minutes. Never call this from the GUI thread.
    """
    ids = [str(m).strip().lower() for m in (model_ids or ()) if str(m).strip()]
    if not ids:
        ids = sorted(_load_bundled_build_snapshot())
    if not ids:
        return {}

    debug_log(f'Fetching live NVIDIA metadata for {len(ids)} models')
    metadata: dict[str, dict[str, Any]] = {}
    try:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=_BUILD_MAX_WORKERS) as pool:
            for model_id, result in zip(ids, pool.map(_fetch_model_markdown_metadata, ids)):
                if result:
                    metadata[model_id] = result
    except Exception:
        debug_log('Concurrent metadata fetch failed; falling back to serial')
        for model_id in ids:
            result = _fetch_model_markdown_metadata(model_id)
            if result:
                metadata[model_id] = result

    debug_log(f'Build metadata fetch result entries={len(metadata)}')
    return metadata


def _scrape_individual_model_page(model_id: str) -> dict[str, Any]:
    """Best-effort live metadata for a single model.

    Kept as a thin wrapper for backwards compatibility: the rendered HTML page
    is behind the same WAF challenge as the listing, so the ``.md`` mirror is
    used instead.
    """
    return _fetch_model_markdown_metadata(model_id)


def _get_build_metadata(use_cache: bool = True, model_ids: Sequence[str] | None = None) -> dict[str, dict[str, Any]]:
    """Get build metadata with session-level caching.

    Loads the bundled snapshot, then enriches it with live per-model metadata
    when ``use_cache`` is False. The live pass performs one HTTP request per
    model, so it must only ever run off the GUI thread.
    """
    global _BUILD_METADATA_CACHE, _BUILD_METADATA_CACHE_TIME

    now = time.time()
    # Return cached metadata if available and fresh
    if use_cache and _BUILD_METADATA_CACHE is not None and (now - _BUILD_METADATA_CACHE_TIME) < _MODELS_CACHE_TTL:
        debug_log(f'Using in-memory Build metadata cache entries={len(_BUILD_METADATA_CACHE)}')
        return _BUILD_METADATA_CACHE

    # Always load bundled snapshot as base so the UI never depends on live scraping.
    snap = _load_bundled_build_snapshot()

    if use_cache:
        _BUILD_METADATA_CACHE = snap
        _BUILD_METADATA_CACHE_TIME = now
        debug_log(f'Returning snapshot-only Build metadata entries={len(snap)} use_cache=True')
        return snap

    # Try to fetch live metadata, but don't fail if network is down
    try:
        live = _fetch_build_page_metadata(model_ids)
        result = _merge_build_metadata_dicts(snap, live)
    except Exception:
        # Fall back to snapshot only if live scraping fails
        result = snap
        debug_log('Live Build metadata merge failed; using snapshot only')

    # Cache the result
    _BUILD_METADATA_CACHE = result
    _BUILD_METADATA_CACHE_TIME = now
    debug_log(f'Cached Build metadata entries={len(result)} use_cache={use_cache}')
    return result


def warmup_build_metadata(use_cache: bool = True) -> None:
    """Refresh the hosted API list and live Build metadata off the GUI thread."""
    global _BUILD_WARMUP_STATE
    if use_cache:
        return
    with _BUILD_WARMUP_LOCK:
        if _BUILD_WARMUP_STATE == 1:
            debug_log('Warmup already running')
            return
        _BUILD_WARMUP_STATE = 1
    try:
        debug_log(f'Warmup start thread={threading.current_thread().name}')
        refreshed = _refresh_build_snapshot()
        debug_log(f'Build snapshot refresh entries={refreshed}')
        # Refresh /v1/models first. It is the authority for hosted models.
        invalidate_models_rich_cache()
        get_available_models_rich(
            use_cache=False, scrape_missing=False, refresh_metadata=False)
        # Rebuild the rich list after the snapshot refresh, without another
        # live per-model scrape.
        invalidate_models_rich_cache()
        get_available_models_rich(use_cache=True, scrape_missing=False)
        debug_log('Warmup finished successfully')
        with _BUILD_WARMUP_LOCK:
            _BUILD_WARMUP_STATE = 2
    except Exception:
        debug_log('Warmup failed with exception')
        with _BUILD_WARMUP_LOCK:
            _BUILD_WARMUP_STATE = 0


def invalidate_models_rich_cache() -> None:
    """Invalidate the in-memory rich models cache.

    The models dialog may have already built a rich list using snapshot-only
    Build metadata. After live Build metadata warmup completes, the dialog must
    rebuild the rich list so free flags and descriptions are re-merged.
    """
    global _MODELS_RICH_CACHE, _MODELS_RICH_CACHE_TIME
    _MODELS_RICH_CACHE = None
    _MODELS_RICH_CACHE_TIME = 0
    debug_log('Invalidated in-memory rich models cache')


def invalidate_build_metadata_cache() -> None:
    """Force the next metadata request to refetch live data.

    Also clears the warmup latch, otherwise a second press of "Refresh list"
    would be treated as a duplicate and silently do nothing.
    """
    global _BUILD_METADATA_CACHE, _BUILD_METADATA_CACHE_TIME, _BUILD_WARMUP_STATE
    _BUILD_METADATA_CACHE = None
    _BUILD_METADATA_CACHE_TIME = 0
    with _BUILD_WARMUP_LOCK:
        if _BUILD_WARMUP_STATE != 1:
            _BUILD_WARMUP_STATE = 0
    debug_log('Invalidated Build metadata cache')


def _is_free_model_entry(entry: dict[str, Any]) -> bool | None:
    # NVIDIA /v1/models does not reliably expose pricing. We treat models as "free"
    # only if the entry explicitly indicates it. Otherwise keep the value unknown
    # until Build metadata confirms it.
    for key in ('pricing', 'price', 'cost'):
        v = entry.get(key)
        if isinstance(v, dict):
            try:
                nums = [float(x) for x in v.values() if x is not None]
                if nums and max(nums) == 0:
                    return True
                if nums:
                    return False
            except Exception:
                pass
        if isinstance(v, (int, float)):
            try:
                return float(v) == 0
            except Exception:
                pass
    # Some APIs use flags
    for key in ('free', 'is_free', 'free_tier'):
        if entry.get(key) is True:
            return True
        if entry.get(key) is False:
            return False
    return None


def _normalise_model_entry(entry: dict[str, Any]) -> dict[str, Any]:
    # Keep a stable shape for the UI.
    model_id = str(entry.get('id', '') or '').strip()
    name = str(entry.get('name', '') or model_id).strip() or model_id
    description = str(entry.get('description', '') or '').strip()
    created = entry.get('created') if isinstance(entry.get('created'), str) else None
    context_length = entry.get('context_length') or entry.get('max_context_length') or entry.get('context')
    try:
        context_length = int(context_length) if context_length is not None else 0
    except Exception:
        context_length = 0
    free_status = _is_free_model_entry(entry)
    return {
        'id': model_id,
        'name': name,
        'description': description,
        'created': created,
        'context_length': context_length,
        'raw': entry,
        # Presence in NVIDIA's hosted API list is the reliable availability signal.
        'api_listed': True,
        'is_free': free_status if free_status is not None else True,
        'has_partner_endpoint': False,
        'is_downloadable': False,
        'publisher': '',
        'publisher_slug': '',
        'build_url': '',
        'use_cases': [],
        'updated_label': '',
        'updated_date': '',
    }


def _merge_build_metadata(models: list[dict[str, Any]], build_metadata: dict[str, dict[str, Any]] | None, scrape_missing: bool = False) -> list[dict[str, Any]]:
    """Merge build metadata into model entries.

    Args:
        models: List of model dicts from the NVIDIA API
        build_metadata: Build page metadata dict, or None if unavailable
        scrape_missing: If True, scrape individual model pages for missing metadata (slow!)

    Returns:
        The models list with enriched metadata (mutates in place)
    """
    debug_log(f'Merge: build_metadata keys count={len(build_metadata) if build_metadata else 0}')
    if build_metadata and len(build_metadata) > 0:
        # Show sample keys from build_metadata for debugging
        sample_keys = list(build_metadata.keys())[:5]
        debug_log(f'Merge: sample build_metadata keys: {sample_keys}')

    matched_count = 0
    for model in models:
        model_id = str(model.get('id') or '').strip().lower()
        if not model_id:
            continue

        # First try exact metadata, then canonical slug variants.
        extra = build_metadata.get(model_id) if build_metadata else None
        if not extra and build_metadata:
            for candidate_id in _candidate_model_ids_from_model_id(model_id):
                extra = build_metadata.get(candidate_id)
                if extra:
                    debug_log(f'Merge: matched {model_id} via candidate {candidate_id}')
                    break

        # If no metadata and scraping is enabled, fetch from individual page
        if not extra and scrape_missing and '/' in model_id:
            extra = _scrape_individual_model_page(model_id)
            # Cache the scraped result for future use
            if build_metadata is not None and extra:
                build_metadata[model_id] = extra

        if not extra:
            debug_log(f'Merge: no metadata found for {model_id}')
            # Construct minimal metadata from model ID
            if '/' in model_id:
                publisher_slug, build_slug = model_id.split('/', 1)
                if not model.get('build_url'):
                    model['build_url'] = f'https://build.nvidia.com/{publisher_slug}/{build_slug}'
                if not model.get('publisher_slug'):
                    model['publisher_slug'] = publisher_slug
                if not model.get('build_slug'):
                    model['build_slug'] = build_slug
            continue

        matched_count += 1
        # Presence in /v1/models establishes that this is currently exposed.
        if model.get('is_free') is None:
            model['is_free'] = True
        if extra.get('has_partner_endpoint') is True:
            model['has_partner_endpoint'] = True
        if extra.get('is_downloadable') is True:
            model['is_downloadable'] = True
        # Update other fields unconditionally if present.
        model['publisher'] = str(extra.get('publisher') or model.get('publisher') or '').strip()
        model['publisher_slug'] = str(extra.get('publisher_slug') or model.get('publisher_slug') or '').strip()
        model['build_slug'] = str(extra.get('build_slug') or model.get('build_slug') or '').strip()
        model['build_url'] = str(extra.get('build_url') or model.get('build_url') or '').strip()
        model['updated_label'] = str(extra.get('updated_label') or model.get('updated_label') or '').strip()
        model['updated_date'] = str(extra.get('updated_date') or model.get('updated_date') or '')
        model['created'] = str(extra.get('created') or model.get('created') or '')
        if extra.get('description') and not model.get('description'):
            model['description'] = str(extra.get('description') or '').strip()
        # Only replace use cases when the metadata actually has some, otherwise a
        # source without them would wipe out what another source supplied.
        use_cases = extra.get('use_cases')
        if isinstance(use_cases, (list, tuple)):
            cleaned = [str(x).strip() for x in use_cases if str(x).strip()]
            if cleaned:
                model['use_cases'] = cleaned
    debug_log(f'Merge: matched {matched_count}/{len(models)} models')
    return models


def _models_from_preview_snapshot(
    api_models: list[dict[str, Any]],
    build_metadata: dict[str, dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Build the dialog list from the current free preview catalogue."""
    if not build_metadata:
        return api_models

    api_by_id = {
        str(model.get('id') or '').strip().lower(): model
        for model in api_models
        if str(model.get('id') or '').strip()
    }
    models: list[dict[str, Any]] = []
    for model_id, metadata in sorted(build_metadata.items()):
        api_model = api_by_id.get(model_id)
        if api_model is None:
            for candidate_id in _candidate_model_ids_from_model_id(model_id):
                api_model = api_by_id.get(candidate_id)
                if api_model is not None:
                    break
        if api_model is not None:
            entry = dict(api_model)
            entry['id'] = model_id
        else:
            entry = _normalise_model_entry({'id': model_id, 'name': metadata.get('name') or model_id})
            entry['api_listed'] = False
        models.append(entry)
    return _merge_build_metadata(models, build_metadata)


def get_available_models_rich(
    use_cache: bool = True,
    scrape_missing: bool = False,
    allow_network: bool = True,
    refresh_metadata: bool = True,
) -> list[dict[str, Any]]:
    """Return hosted NVIDIA API models enriched with preview-catalogue metadata."""
    global _MODELS_RICH_CACHE, _MODELS_RICH_CACHE_TIME
    now = time.time()

    # Return in-memory cache if available and fresh
    if use_cache and _MODELS_RICH_CACHE is not None and (now - _MODELS_RICH_CACHE_TIME) < _MODELS_CACHE_TTL:
        debug_log(f'Using in-memory rich models cache entries={len(_MODELS_RICH_CACHE)}')
        return _MODELS_RICH_CACHE

    # Try disk cache next
    cache_path = _models_cache_path()
    cached = _read_cached_models_json(cache_path) if use_cache else None
    if cached and isinstance(cached.get('data'), list):
        models = [_normalise_model_entry(x) for x in cached.get('data', []) if isinstance(x, dict)]
        build_metadata = _get_build_metadata(use_cache=True) if refresh_metadata else {}
        models = _models_from_preview_snapshot(models, build_metadata) if refresh_metadata else models
        if not refresh_metadata:
            models = _merge_build_metadata(models, build_metadata, scrape_missing=scrape_missing)
        models = [m for m in models if m.get('id')]
        if models:
            _MODELS_RICH_CACHE = models
            _MODELS_RICH_CACHE_TIME = now
            free_count = sum(1 for item in models if item.get('is_free') is True)
            described_count = sum(1 for item in models if str(item.get('description') or '').strip())
            debug_log(f'Loaded rich models from disk cache total={len(models)} free={free_count} described={described_count}')
            return models

    # Fetch fresh from API using calibre's Browser
    try:
        if not allow_network:
            raise RuntimeError('network disallowed')
        br = Browser()
        br.set_user_agent('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36')
        br.set_current_header('Accept', 'application/json')

        response = br.open(f'{NVIDIA_FREE_API_URL}/v1/models', timeout=10)
        data = json.loads(response.read().decode('utf-8'))
        if isinstance(data, dict) and isinstance(data.get('data'), list):
            _write_cached_models_json(cache_path, data)
            models = [_normalise_model_entry(x) for x in data.get('data', []) if isinstance(x, dict)]
            build_metadata = _get_build_metadata(use_cache=True) if refresh_metadata else {}
            models = _models_from_preview_snapshot(models, build_metadata) if refresh_metadata else models
            if not refresh_metadata:
                models = _merge_build_metadata(models, build_metadata, scrape_missing=scrape_missing)
            models = [m for m in models if m.get('id')]
            if models:
                _MODELS_RICH_CACHE = models
                _MODELS_RICH_CACHE_TIME = now
                free_count = sum(1 for item in models if item.get('is_free') is True)
                described_count = sum(1 for item in models if str(item.get('description') or '').strip())
                debug_log(f'Loaded rich models from live API total={len(models)} free={free_count} described={described_count}')
                return models
    except Exception:
        debug_log('Live NVIDIA API model fetch failed; retaining the previous hosted cache')
        pass

    cached = _read_cached_models_json(cache_path)
    if cached and isinstance(cached.get('data'), list):
        models = [_normalise_model_entry(x) for x in cached.get('data', []) if isinstance(x, dict)]
        build_metadata = _get_build_metadata(use_cache=True) if refresh_metadata else {}
        models = _models_from_preview_snapshot(models, build_metadata) if refresh_metadata else models
        if not refresh_metadata:
            models = _merge_build_metadata(models, build_metadata, scrape_missing=False)
        models = [m for m in models if m.get('id')]
        if models:
            _MODELS_RICH_CACHE = models
            _MODELS_RICH_CACHE_TIME = now
            debug_log(f'Live refresh failed; retained hosted API cache entries={len(models)}')
            return models
    debug_log('No hosted NVIDIA API model list is available')
    return []


def _fetch_nvidia_models(use_cache: bool = True) -> list[str]:
    """Fetch the plain NVIDIA model-id list without triggering Build metadata scraping."""
    cache_path = _models_cache_path()
    cached = _read_cached_models_json(cache_path) if use_cache else None
    if cached and isinstance(cached.get('data'), list):
        models = sorted({
            str(x.get('id') or '').strip()
            for x in cached.get('data', [])
            if isinstance(x, dict) and str(x.get('id') or '').strip()
        })
        if models:
            return models

    try:
        br = Browser()
        br.set_user_agent('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36')
        br.set_current_header('Accept', 'application/json')

        response = br.open(f'{NVIDIA_FREE_API_URL}/v1/models', timeout=10)
        data = json.loads(response.read().decode('utf-8'))
        if isinstance(data, dict) and isinstance(data.get('data'), list):
            _write_cached_models_json(cache_path, data)
            models = sorted({
                str(x.get('id') or '').strip()
                for x in data.get('data', [])
                if isinstance(x, dict) and str(x.get('id') or '').strip()
            })
            if models:
                return models
    except Exception:
        pass
    return []


def get_available_models(use_cache: bool = True) -> list[str]:
    """Get NVIDIA models with lightweight caching."""
    global _MODELS_IDS_CACHE, _MODELS_IDS_CACHE_TIME
    if use_cache and _MODELS_IDS_CACHE is not None:
        if (time.time() - _MODELS_IDS_CACHE_TIME) < _MODELS_CACHE_TTL:
            return _MODELS_IDS_CACHE
    models = _fetch_nvidia_models(use_cache=use_cache)
    _MODELS_IDS_CACHE = models
    _MODELS_IDS_CACHE_TIME = time.time()
    return models


def get_cached_model_ids() -> list[str]:
    """Return the same catalogue used by the NVIDIA model browser, without HTTP."""
    try:
        models = get_available_models_rich(use_cache=True, allow_network=False)
        ids = sorted({
            str(item.get('id') or '').strip()
            for item in models
            if isinstance(item, dict) and str(item.get('id') or '').strip()
        })
        if ids:
            return ids
    except Exception:
        pass
    return []


def _get_setting(key: str, default: str = '') -> str:
    """Get a NVIDIA Free setting from plugin preferences."""
    return str(pref(f'nvidia_free_{key}', default) or default).strip()


def _set_setting(key: str, value: str) -> None:
    """Set a NVIDIA Free setting in plugin preferences."""
    set_pref(f'nvidia_free_{key}', value)


def get_proxy_url() -> str:
    """Get the configured proxy URL."""
    url = _get_setting('proxy_url', NVIDIA_FREE_PROXY_URL)
    return url.strip().rstrip('/')


def get_model() -> str:
    """Get the configured model name."""
    return _get_setting('model', NVIDIA_FREE_DEFAULT_MODEL)


def get_api_model() -> str:
    """Get the configured model name for NVIDIA Free (API)."""
    return str(pref('nvidia_free_api_model', NVIDIA_FREE_API_DEFAULT_MODEL) or NVIDIA_FREE_API_DEFAULT_MODEL).strip()


def get_api_key() -> str:
    """Get the configured API key for NVIDIA Free (API)."""
    return str(pref('nvidia_free_api_key', '') or '').strip()


def get_api_base_url() -> str:
    """Get the configured base URL for NVIDIA Free (API)."""
    url = str(pref('nvidia_free_api_base_url', NVIDIA_FREE_API_URL) or NVIDIA_FREE_API_URL).strip()
    return url.rstrip('/')


def is_ready_for_use() -> bool:
    """NVIDIA Free is always ready — no API key needed."""
    return True


class NvidiaFreeProvider:
    """Drop-in provider that mimics a calibre AIProviderPlugin for NVIDIA Free API."""

    name = 'NVIDIA Free'

    @property
    def is_ready_for_use(self) -> bool:
        return is_ready_for_use()

    def human_readable_model_name(self, model_id: str) -> str:
        """Return a human-readable model name."""
        model = model_id or get_model()
        if '/' in model:
            return model.split('/')[-1]
        return model

    def text_chat(self, messages: Iterable[ChatMessage], use_model: str = '') -> Iterator[ChatResponse]:
        """Send chat messages to NVIDIA Free API."""
        yield from chat_with_error_handler(self._text_chat_impl(messages, use_model))

    def _text_chat_impl(self, messages: Iterable[ChatMessage], use_model: str = '') -> Iterator[ChatResponse]:
        """Implementation of text_chat using NVIDIA Free proxy."""
        proxy_url = get_proxy_url()
        model = (use_model or get_model()).strip()
        if not proxy_url:
            raise ValueError(_('NVIDIA Free proxy URL is not configured'))

        api_url = f"{proxy_url}/api/chat"

        headers = {
            'Content-Type': 'application/json',
        }

        messages_data = [{'role': m.type.value, 'content': m.query} for m in messages]

        data: dict[str, Any] = {
            'model': model,
            'messages': messages_data,
            'temperature': 0.7,
            'max_tokens': 4096,
            'stream': True,
        }

        rq = Request(api_url, data=json.dumps(data).encode('utf-8'), headers=headers, method='POST')

        for chunk in read_streaming_response(rq, self.name):
            for choice in chunk.get('choices', []):
                delta = choice.get('delta', {})
                content = delta.get('content') or ''
                role = delta.get('role') or 'assistant'
                if content:
                    yield ChatResponse(content=content, type=ChatMessageType(role), plugin_name=self.name)

        yield ChatResponse(
            has_metadata=True,
            cost=0.0,
            currency='',
            provider=self.name,
            model=model,
            plugin_name=self.name,
        )

    def get_available_models(self) -> list[str]:
        """Return list of available models."""
        return get_available_models()


class NvidiaFreeApiProvider(NvidiaFreeProvider):
    """NVIDIA Free provider using an NVIDIA API key."""

    name = 'NVIDIA Free (API)'

    @property
    def is_ready_for_use(self) -> bool:
        return bool(get_api_key())

    def _text_chat_impl(self, messages: Iterable[ChatMessage], use_model: str = '') -> Iterator[ChatResponse]:
        api_key = get_api_key()
        if not api_key:
            raise ValueError(_('NVIDIA API key is not configured'))

        base_url = get_api_base_url()
        model = (use_model or get_api_model()).strip() or NVIDIA_FREE_API_DEFAULT_MODEL
        api_url = f"{base_url}/v1/chat/completions"

        headers = {
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {api_key}',
        }

        messages_data = [{'role': m.type.value, 'content': m.query} for m in messages]
        data: dict[str, Any] = {
            'model': model,
            'messages': messages_data,
            'temperature': 0.7,
            'max_tokens': 4096,
            'stream': True,
        }

        rq = Request(api_url, data=json.dumps(data).encode('utf-8'), headers=headers, method='POST')

        for chunk in read_streaming_response(rq, self.name):
            for choice in chunk.get('choices', []):
                delta = choice.get('delta', {})
                content = delta.get('content') or ''
                role = delta.get('role') or 'assistant'
                if content:
                    yield ChatResponse(content=content, type=ChatMessageType(role), plugin_name=self.name)

        yield ChatResponse(
            has_metadata=True,
            cost=0.0,
            currency='',
            provider=self.name,
            model=model,
            plugin_name=self.name,
        )


def resolve_provider(provider_name: str):
    """Return a provider instance, handling NVIDIA Free internally.

    This function is used by summarizer.py to get the AI provider.
    """
    name = str(provider_name or '').strip()
    if name == 'NVIDIA Free':
        return NvidiaFreeProvider()
    if name == 'NVIDIA Free (API)':
        return NvidiaFreeApiProvider()
    from calibre.ai.utils import plugin_for_name
    return plugin_for_name(provider_name)


def resolve_nvidia_free_provider() -> NvidiaFreeProvider:
    """Alias returning NvidiaFreeProvider for backwards compatibility."""
    return NvidiaFreeProvider()


def resolve_nvidia_free_api_provider() -> NvidiaFreeApiProvider:
    return NvidiaFreeApiProvider()


def _format_model_id_for_label(model_id: str) -> str:
    s = html_lib.escape(str(model_id or '').strip())
    if not s:
        return ''
    for ch in ('/', '-', '_', '.', ':'):
        s = s.replace(ch, ch + '\u200b')
    return s


def config_widget():
    """Build a configuration widget for NVIDIA Free settings."""
    from qt.core import QComboBox, QFormLayout, QLabel, QLineEdit, QPushButton, QWidget, QHBoxLayout
    w = QWidget()
    l = QFormLayout(w)
    # Wrap effective model label in a container to prevent clipping
    effective_model_container = QWidget(w)
    effective_model_layout = QHBoxLayout(effective_model_container)
    effective_model_layout.setContentsMargins(0, 0, 0, 0)
    w._effective_model_label = QLabel('', effective_model_container)
    w._effective_model_label.setWordWrap(True)
    w._effective_model_label.setStyleSheet('color: #888; font-style: italic;')
    w._effective_model_label.setToolTip(_('The model that will actually be used (manual overrides dropdown)'))
    # Ensure label expands to avoid clipping
    try:
        from qt.core import QSizePolicy
        w._effective_model_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        effective_model_container.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
    except Exception:
        pass
    effective_model_layout.addWidget(w._effective_model_label)

    def update_effective_model():
        manual = manual_model_edit.text().strip()
        if manual:
            w._effective_model_label.setText(_('Effective model: <b>%s</b> (manual)') % _format_model_id_for_label(manual))
        else:
            try:
                model = str(model_combo.currentData() or '').strip()
            except Exception:
                model = NVIDIA_FREE_DEFAULT_MODEL
            w._effective_model_label.setText(_('Effective model: <b>%s</b> (dropdown)') % _format_model_id_for_label(model))

    def on_combo_changed():
        if manual_model_edit.text().strip():
            manual_model_edit.clear()
        update_effective_model()

    def on_manual_changed():
        update_effective_model()

    try:
        l.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
    except Exception:
        try:
            l.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)
        except Exception:
            pass

    info = QLabel(_(
        '<p><b>NVIDIA Free</b> — No API key required.</p>'
        '<p>Uses a free proxy server to access NVIDIA AI models.</p>'
        '<p>Choose a current model below, or browse the full NVIDIA catalogue.</p>'
    ))
    info.setWordWrap(True)
    info.setOpenExternalLinks(True)
    l.addRow(info)

    # Keep the compact selector on the same catalogue as ChooseNvidiaModel.
    model_combo = QComboBox(w)
    model_combo.setToolTip(_('The NVIDIA model to use for text generation'))
    models = get_cached_model_ids()
    for m in models:
        label = m
        if m == NVIDIA_FREE_DEFAULT_MODEL:
            label = m + ' ' + _('(recommended)')
        model_combo.addItem(label, m)
    current_model = get_model()
    if current_model not in models:
        current_model = models[0] if models else ''
    idx = model_combo.findData(current_model)
    if idx >= 0:
        model_combo.setCurrentIndex(idx)
    l.addRow(_('Model:'), model_combo)

    browse_btn = QPushButton(_('Browse models…'))
    browse_btn.setToolTip(_('Browse the full NVIDIA models list with details'))

    manual_model_edit = QLineEdit(w)
    manual_model_edit.setPlaceholderText(_('Optional — enter a model ID'))
    manual_model_edit.setText(current_model)
    manual_model_edit.setToolTip(_('Optional manual model override.\n\nIf set, this value is used instead of the dropdown selection.'))
    manual_model_edit.textChanged.connect(on_manual_changed)
    row = QWidget(w)
    from qt.core import QHBoxLayout
    hl = QHBoxLayout(row)
    hl.setContentsMargins(0, 0, 0, 0)
    hl.addWidget(manual_model_edit)
    hl.addWidget(browse_btn)
    l.addRow(_('Manual model:'), row)

    def browse_models():
        try:
            from qt.core import QApplication, QCursor, Qt
            from calibre_plugins.chapter_summarizer.nvidia_free import warmup_build_metadata
            from calibre_plugins.chapter_summarizer.nvidia_models_dialog import ChooseNvidiaModel

            current_model = manual_model_edit.text().strip()
            if not current_model:
                try:
                    current_model = str(model_combo.currentData() or '').strip()
                except Exception:
                    current_model = ''

            browse_btn.setEnabled(False)
            browse_btn.setText(_('Loading models…'))
            QApplication.processEvents()
            QApplication.setOverrideCursor(QCursor(Qt.CursorShape.WaitCursor))
            try:
                d = ChooseNvidiaModel(current_model, w)
            finally:
                QApplication.restoreOverrideCursor()
                browse_btn.setText(_('Browse models…'))
                browse_btn.setEnabled(True)
                QApplication.processEvents()
            from qt.core import QDialog
            if d.exec() == QDialog.DialogCode.Accepted:
                if d.model_id:
                    model_combo.blockSignals(True)
                    idx = model_combo.findData(d.model_id)
                    if idx >= 0:
                        model_combo.setCurrentIndex(idx)
                    else:
                        model_combo.addItem(d.model_id, d.model_id)
                        model_combo.setCurrentIndex(model_combo.findData(d.model_id))
                    model_combo.blockSignals(False)
                    manual_model_edit.setText(d.model_id)
                    update_effective_model()
        except Exception:
            from calibre.gui2 import error_dialog
            import traceback
            error_dialog(w, _('Error'), _('Failed to open the NVIDIA models browser.') + '\n\n' + traceback.format_exc(), show=True)

    browse_btn.clicked.connect(browse_models)

    model_combo.currentIndexChanged.connect(on_combo_changed)
    l.addRow(_('Effective:'), effective_model_container)

    proxy_edit = QLineEdit(w)
    proxy_edit.setPlaceholderText(_('Leave empty for default proxy'))
    proxy_edit.setText(get_proxy_url())
    proxy_edit.setToolTip(_('Proxy server URL (leave empty for default)'))
    l.addRow(_('Proxy URL:'), proxy_edit)

    test_btn = QPushButton(_('Test connection'))
    test_btn.clicked.connect(lambda: _test_connection(w, manual_model_edit if manual_model_edit.text().strip() else model_combo))
    l.addRow('', test_btn)

    update_effective_model()

    w._model_combo = model_combo
    w._manual_model_edit = manual_model_edit
    w._proxy_edit = proxy_edit
    return w


def _test_connection(parent_widget, model_widget):
    """Test the connection to the NVIDIA Free proxy."""
    from qt.core import QApplication, QDialog, QLabel, QPushButton, QVBoxLayout, QDialogButtonBox

    proxy_url = get_proxy_url()
    try:
        model = str(model_widget.currentData() or '').strip() or NVIDIA_FREE_DEFAULT_MODEL
    except Exception:
        model = str(getattr(model_widget, 'text', lambda: '')() or '').strip() or NVIDIA_FREE_DEFAULT_MODEL

    d = QDialog(parent_widget)
    d.setWindowTitle(_('Test NVIDIA Free Connection'))
    vl = QVBoxLayout(d)

    status_label = QLabel(_('Testing connection…'), d)
    vl.addWidget(status_label)

    bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
    bb.accepted.connect(d.accept)
    bb.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
    vl.addWidget(bb)

    d.resize(400, 150)
    d.show()
    QApplication.processEvents()

    try:
        br = Browser()
        br.set_current_header('Content-Type', 'application/json')

        health_url = f"{proxy_url}/api/health"
        response = br.open(health_url, timeout=10)
        result = json.loads(response.read().decode('utf-8'))
        if result.get('status') == 'ok':
            status_label.setText(_(
                '<b>✓ Connection successful!</b><br><br>'
                f'Service: {result.get("service", "N/A")}<br>'
                f'Version: {result.get("version", "N/A")}<br>'
                f'Model: {model}'
            ))
            bb.button(QDialogButtonBox.StandardButton.Ok).setEnabled(True)
        else:
            status_label.setText(_('<b>✗ Connection failed:</b> Unexpected response'))
    except Exception as e:
        status_label.setText(_('<b>✗ Connection failed:</b><br>') + str(e))

    QApplication.processEvents()


def save_settings(widget) -> None:
    """Save NVIDIA Free settings from the configuration widget."""
    model = str(getattr(widget, '_manual_model_edit', None).text() if getattr(widget, '_manual_model_edit', None) is not None else '').strip()
    if not model:
        try:
            model = str(widget._model_combo.currentData() or '').strip()
        except Exception:
            pass
    if not model:
        model = NVIDIA_FREE_DEFAULT_MODEL
    proxy = widget._proxy_edit.text().strip()
    if not proxy:
        proxy = NVIDIA_FREE_PROXY_URL
    _set_setting('model', model)
    _set_setting('proxy_url', proxy)


def validate(widget) -> bool:
    """Validate the configuration widget — always valid for NVIDIA Free."""
    return True


def config_widget_api():
    """Build a configuration widget for NVIDIA Free (API) settings."""
    from qt.core import QComboBox, QFormLayout, QLabel, QLineEdit, QPushButton, QWidget, QHBoxLayout
    w = QWidget()
    l = QFormLayout(w)
    # Wrap effective model label in a container to prevent clipping
    api_effective_model_container = QWidget(w)
    api_effective_model_layout = QHBoxLayout(api_effective_model_container)
    api_effective_model_layout.setContentsMargins(0, 0, 0, 0)
    w._api_effective_model_label = QLabel('', api_effective_model_container)
    w._api_effective_model_label.setWordWrap(True)
    w._api_effective_model_label.setStyleSheet('color: #888; font-style: italic;')
    w._api_effective_model_label.setToolTip(_('The model that will actually be used (manual overrides dropdown)'))
    # Ensure label expands to avoid clipping
    try:
        from qt.core import QSizePolicy
        w._api_effective_model_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        api_effective_model_container.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
    except Exception:
        pass
    api_effective_model_layout.addWidget(w._api_effective_model_label)

    def update_effective_model():
        manual = manual_model_edit.text().strip()
        if manual:
            w._api_effective_model_label.setText(_('Effective model: <b>%s</b> (manual)') % _format_model_id_for_label(manual))
        else:
            try:
                model = str(model_combo.currentData() or '').strip()
            except Exception:
                model = NVIDIA_FREE_API_DEFAULT_MODEL
            w._api_effective_model_label.setText(_('Effective model: <b>%s</b> (dropdown)') % _format_model_id_for_label(model))

    def on_combo_changed():
        if manual_model_edit.text().strip():
            manual_model_edit.clear()
        update_effective_model()

    def on_manual_changed():
        update_effective_model()

    try:
        l.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
    except Exception:
        try:
            l.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)
        except Exception:
            pass

    info = QLabel(_(
        '<p><b>NVIDIA Free (API)</b> — Uses your NVIDIA API key.</p>'

    ))
    free_api_guide_button = QPushButton(_('Free API guide'))
    free_api_guide_button.setToolTip(_('How to obtain free API keys for NVIDIA, OpenRouter, and LM Studio'))
    def _open_free_api_guide():
        from calibre_plugins.chapter_summarizer.main import FreeApiGuideDialog
        d = FreeApiGuideDialog(parent=w)
        d.exec()
    free_api_guide_button.clicked.connect(_open_free_api_guide)
    try:
        from qt.core import QIcon
        free_api_guide_button.setIcon(QIcon.ic('help.png'))
    except Exception:
        pass
    free_api_guide_row = QHBoxLayout()
    free_api_guide_row.setContentsMargins(0, 0, 0, 0)
    free_api_guide_row.addWidget(free_api_guide_button)
    free_api_guide_row.addStretch(1)
    info.setWordWrap(True)
    info.setOpenExternalLinks(True)
    l.addRow(info)
    l.addRow(free_api_guide_row)

    key_edit = QLineEdit(w)
    key_edit.setPlaceholderText(_('Paste your NVIDIA API key'))
    key_edit.setText(get_api_key())
    key_edit.setToolTip(_('Required for NVIDIA Free (API)'))
    l.addRow(_('API key:'), key_edit)

    # Keep the compact selector on the same catalogue as ChooseNvidiaModel.
    model_combo = QComboBox(w)
    model_combo.setToolTip(_('The NVIDIA model to use for text generation'))
    models = get_cached_model_ids()
    for m in models:
        label = m
        if m == NVIDIA_FREE_API_DEFAULT_MODEL:
            label = m + ' ' + _('(recommended)')
        model_combo.addItem(label, m)
    current_model = get_api_model()
    if current_model not in models:
        current_model = models[0] if models else ''
    idx = model_combo.findData(current_model)
    if idx >= 0:
        model_combo.setCurrentIndex(idx)
    l.addRow(_('Model:'), model_combo)

    browse_btn = QPushButton(_('Browse models…'))
    browse_btn.setToolTip(_('Browse the full NVIDIA models list with details'))

    manual_model_edit = QLineEdit(w)
    manual_model_edit.setPlaceholderText(_('Optional — enter a model ID'))
    manual_model_edit.setText(current_model)
    manual_model_edit.setToolTip(_('Optional manual model override.\n\nIf set, this value is used instead of the dropdown selection.'))
    manual_model_edit.textChanged.connect(on_manual_changed)
    row = QWidget(w)
    from qt.core import QHBoxLayout
    hl = QHBoxLayout(row)
    hl.setContentsMargins(0, 0, 0, 0)
    hl.addWidget(manual_model_edit)
    hl.addWidget(browse_btn)
    l.addRow(_('Manual model:'), row)

    def browse_models():
        try:
            from qt.core import QApplication, QCursor, Qt
            from calibre_plugins.chapter_summarizer.nvidia_free import warmup_build_metadata
            from calibre_plugins.chapter_summarizer.nvidia_models_dialog import ChooseNvidiaModel

            current_model = manual_model_edit.text().strip()
            if not current_model:
                try:
                    current_model = str(model_combo.currentData() or '').strip()
                except Exception:
                    current_model = ''

            browse_btn.setEnabled(False)
            browse_btn.setText(_('Loading models…'))
            QApplication.processEvents()
            QApplication.setOverrideCursor(QCursor(Qt.CursorShape.WaitCursor))
            try:
                d = ChooseNvidiaModel(current_model, w)
            finally:
                QApplication.restoreOverrideCursor()
                browse_btn.setText(_('Browse models…'))
                browse_btn.setEnabled(True)
                QApplication.processEvents()
            from qt.core import QDialog
            if d.exec() == QDialog.DialogCode.Accepted:
                if d.model_id:
                    model_combo.blockSignals(True)
                    idx = model_combo.findData(d.model_id)
                    if idx >= 0:
                        model_combo.setCurrentIndex(idx)
                    else:
                        model_combo.addItem(d.model_id, d.model_id)
                        model_combo.setCurrentIndex(model_combo.findData(d.model_id))
                    model_combo.blockSignals(False)
                    manual_model_edit.setText(d.model_id)
                    update_effective_model()
        except Exception:
            from calibre.gui2 import error_dialog
            import traceback
            error_dialog(w, _('Error'), _('Failed to open the NVIDIA models browser.') + '\n\n' + traceback.format_exc(), show=True)

    browse_btn.clicked.connect(browse_models)

    model_combo.currentIndexChanged.connect(on_combo_changed)
    l.addRow(_('Effective:'), api_effective_model_container)

    base_url_edit = QLineEdit(w)
    base_url_edit.setPlaceholderText(_('Leave empty for default'))
    base_url_edit.setText(get_api_base_url())
    base_url_edit.setToolTip(_('Base URL for NVIDIA API (advanced)'))
    l.addRow(_('Base URL:'), base_url_edit)

    test_btn = QPushButton(_('Test connection'))
    test_btn.clicked.connect(lambda: _test_connection_api(
        w,
        manual_model_edit if manual_model_edit.text().strip() else model_combo,
        api_key=key_edit.text(),
        base_url=base_url_edit.text(),
    ))
    l.addRow('', test_btn)

    update_effective_model()

    w._api_key_edit = key_edit
    w._api_model_combo = model_combo
    w._api_manual_model_edit = manual_model_edit
    w._api_base_url_edit = base_url_edit
    return w


def _test_connection_api(parent_widget, model_widget, api_key: str = '', base_url: str = ''):
    from qt.core import QApplication, QDialog, QLabel, QDialogButtonBox, QVBoxLayout

    api_key = str(api_key or '').strip() or get_api_key()
    base_url = str(base_url or '').strip().rstrip('/') or get_api_base_url()
    try:
        model = str(model_widget.currentData() or '').strip()
    except Exception:
        model = ''
    if not model:
        try:
            model = str(model_widget.text() or '').strip()
        except Exception:
            pass
    model = model or NVIDIA_FREE_API_DEFAULT_MODEL

    d = QDialog(parent_widget)
    d.setWindowTitle(_('Test NVIDIA Free (API) Connection'))
    vl = QVBoxLayout(d)

    status_label = QLabel(_('Testing connection…'), d)
    vl.addWidget(status_label)

    bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
    bb.accepted.connect(d.accept)
    bb.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
    vl.addWidget(bb)

    d.resize(420, 160)
    d.show()
    QApplication.processEvents()

    if not api_key:
        status_label.setText(_('<b>✗ Missing API key.</b>'))
        QApplication.processEvents()
        return

    try:
        br = Browser()
        br.set_current_header('Authorization', f'Bearer {api_key}')

        health_url = f"{base_url}/v1/models"
        response = br.open(health_url, timeout=10)
        status_label.setText(_(
            '<b>✓ Connection successful!</b><br><br>'
            f'Base URL: {base_url}<br>'
            f'Model: {model}'
        ))
        bb.button(QDialogButtonBox.StandardButton.Ok).setEnabled(True)
    except Exception as e:
        status_label.setText(_('<b>✗ Connection failed:</b><br>') + str(e))

    QApplication.processEvents()


def save_settings_api(widget) -> None:
    api_key = str(widget._api_key_edit.text() or '').strip()
    model = str(getattr(widget, '_api_manual_model_edit', None).text() if getattr(widget, '_api_manual_model_edit', None) is not None else '').strip()
    if not model:
        try:
            model = str(widget._api_model_combo.currentData() or '').strip()
        except Exception:
            pass
    if not model:
        model = NVIDIA_FREE_API_DEFAULT_MODEL
    base_url = str(widget._api_base_url_edit.text() or '').strip() or NVIDIA_FREE_API_URL
    set_pref('nvidia_free_api_key', api_key)
    set_pref('nvidia_free_api_model', model)
    set_pref('nvidia_free_api_base_url', base_url)


def validate_api(widget) -> bool:
    return True
