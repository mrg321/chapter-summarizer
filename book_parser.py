#!/usr/bin/env python

from __future__ import annotations

__license__ = 'GPL v3'
__copyright__ = '2026, Comfy.n'
__docformat__ = 'restructuredtext en'

import os
import re
import shutil
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Iterable
from urllib.parse import unquote


@dataclass
class ChapterSection:
    index: int
    title: str
    text: str
    identifier: str = ''

    @property
    def word_count(self) -> int:
        return len(re.findall(r"\S+", self.text))


@dataclass
class ParsedDocument:
    title: str
    source_path: str
    source_format: str
    raw_text: str
    chapters: list[ChapterSection]

    @property
    def total_words(self) -> int:
        return len(re.findall(r"\S+", self.raw_text))


class _HTMLStripper(HTMLParser):

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() in {'p', 'div', 'section', 'article', 'br', 'li', 'h1', 'h2', 'h3', 'h4'}:
            self.parts.append('\n')

    def handle_data(self, data):
        if data:
            self.parts.append(data)

    def get_text(self) -> str:
        return ''.join(self.parts)


_HEADING_RE = re.compile(
    r'^\s*(?:chapter|cap[ií]tulo|capitulo|part|parte|section|se[cç][aã]o|secao|article|artigo)\s+(?:[0-9]+|[ivxlcdm]+)\b.*$',
    re.IGNORECASE,
)
_NUMERIC_HEADING_RE = re.compile(r'^\s*(?:[0-9]+|[IVXLCDM]+)\s*$')
_SHORT_TITLE_RE = re.compile(r'^[^\n]{2,90}$')
_BLOCK_TAGS = {
    'address', 'article', 'aside', 'blockquote', 'body', 'br', 'caption', 'dd', 'div',
    'dl', 'dt', 'figcaption', 'figure', 'footer', 'form', 'h1', 'h2', 'h3', 'h4', 'h5',
    'h6', 'header', 'hr', 'li', 'main', 'nav', 'ol', 'p', 'pre', 'section', 'table',
    'tbody', 'td', 'tfoot', 'th', 'thead', 'tr', 'ul'
}


def _debug_enabled() -> bool:
    try:
        return bool(os.environ.get('CHAPTER_SUMMARIZER_DEBUG_ENABLED'))
    except Exception:
        return False


def _debug_log(message: str) -> None:
    if not _debug_enabled():
        return
    try:
        print(f'[Chapter Summarizer][Parser] {message}')
    except Exception:
        pass


def guess_title(path: str, text: str = '') -> str:
    stem = os.path.splitext(os.path.basename(path))[0]
    stem = stem.replace('_', ' ').replace('-', ' ').strip()
    if text:
        for line in text.splitlines()[:20]:
            stripped = line.strip()
            lowered = stripped.lower()
            if 3 <= len(stripped) <= 120 and not _HEADING_RE.match(stripped):
                if lowered not in {'sumário', 'sumario', 'contents', 'índice', 'indice'} and not any(x in lowered for x in (
                    'copyright', 'título original', 'titulo original', 'preparação', 'preparacao',
                    'revisão', 'revisao', 'projeto gráfico', 'projeto grafico', 'arte de capa',
                    'edição digital', 'edicao digital', 'todos os direitos', 'editora', 'e-isbn'
                )):
                    return stripped
    return stem


def read_text_from_path(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    if ext in {'.txt', '.md', '.text'}:
        return _decode_file(path)
    if ext in {'.htm', '.html', '.xhtml'}:
        return strip_html(_decode_file(path))
    try:
        from calibre.db.fts.text import extract_text
        text = extract_text(path) or ''
        if text:
            return text
    except Exception:
        pass
    return _decode_file(path)


def _local_name(tag) -> str:
    if not isinstance(tag, str):
        return ''
    return tag.rsplit('}', 1)[-1].lower()


def _safe_title(text: str, fallback: str) -> str:
    cleaned = _clean_heading(text or '', 0)
    return cleaned if cleaned and cleaned != 'Chapter 0' else fallback


def _is_front_or_back_matter_title(title: str) -> bool:
    lowered = (title or '').strip().lower()
    if not lowered:
        return False
    prefixes = (
        'capa', 'cover', 'folha de rosto', 'rosto', 'créditos', 'creditos', 'copyright',
        'mídias', 'midias', 'sumário', 'sumario', 'contents', 'índice', 'indice',
        'agradecimentos', 'autor', 'sobre o autor', 'leia', 'notas', 'appendix', 'anexo'
    )
    return lowered.startswith(prefixes)


_MEANINGLESS_STEM_RE = re.compile(
    r'^(?:[a-z]\d+[a-z]?|(?:page|ch|sec|section|part|chapter|item|file)\d+|[a-z]{1,2})$',
    re.IGNORECASE,
)


def _is_meaningless_stem(stem: str) -> bool:
    return bool(_MEANINGLESS_STEM_RE.match(stem.strip()))


def _flatten_toc_leaves(node) -> list[object]:
    children = list(node)
    if not children:
        return [node] if getattr(node, 'dest', None) else []
    ans: list[object] = []
    for child in children:
        ans.extend(_flatten_toc_leaves(child))
    return ans


def _top_level_toc_nodes(toc) -> list[object]:
    ans: list[object] = []
    for child in list(toc):
        if getattr(child, 'dest', None):
            ans.append(child)
    return ans


def _toc_candidates(toc) -> list[object]:
    top_level = _top_level_toc_nodes(toc)
    chapter_like_top = [node for node in top_level if _HEADING_RE.match((node.title or '').strip())]
    if len(chapter_like_top) >= 2:
        nodes = chapter_like_top
    else:
        leaves = _flatten_toc_leaves(toc)
        nodes = leaves if len(leaves) >= 2 else [node for _, node in toc.iterdescendants(level=0) if getattr(node, 'dest', None)]
    deduped: list[object] = []
    seen: set[tuple[str, str, str]] = set()
    for node in nodes:
        key = ((node.title or '').strip(), node.dest or '', node.frag or '')
        if key in seen or not node.dest or _is_front_or_back_matter_title(node.title or ''):
            continue
        seen.add(key)
        deduped.append(node)
    return deduped


def _linearize_html_tree(root) -> tuple[str, dict[str, int]]:
    from lxml import etree

    body_nodes = root.xpath('//*[local-name() = "body"]') if hasattr(root, 'xpath') else []
    body = body_nodes[0] if body_nodes else root
    parts: list[str] = []
    anchors: dict[str, int] = {}
    length = 0

    def append(piece: str):
        nonlocal length
        if not piece:
            return
        parts.append(piece)
        length += len(piece)

    for event, elem in etree.iterwalk(body, events=('start', 'end')):
        tag = _local_name(getattr(elem, 'tag', ''))
        if event == 'start':
            elem_id = ''
            if hasattr(elem, 'get'):
                elem_id = elem.get('id') or (elem.get('name') if tag == 'a' else '') or ''
            if elem_id and elem_id not in anchors:
                anchors[elem_id] = length
            if tag in _BLOCK_TAGS and parts and not parts[-1].endswith('\n'):
                append('\n')
            if getattr(elem, 'text', None):
                append(elem.text)
        else:
            if tag in _BLOCK_TAGS and parts and not parts[-1].endswith('\n'):
                append('\n')
            if getattr(elem, 'tail', None):
                append(elem.tail)
    return normalize_text(''.join(parts)), anchors


def _epub_item_text(container, name: str, cache: dict[str, tuple[str, dict[str, int]]]) -> tuple[str, dict[str, int]]:
    cached = cache.get(name)
    if cached is not None:
        return cached
    try:
        text, anchors = _linearize_html_tree(container.parsed(name))
    except Exception:
        text = normalize_text(strip_html(container.raw_data(name) or ''))
        anchors = {}
    cache[name] = (text, anchors)
    return cache[name]


def _try_epub_item_text(
    container,
    name: str,
    cache: dict[str, tuple[str, dict[str, int]]],
    spine_lookup: dict[str, str] | None = None,
) -> tuple[str, dict[str, int], str]:
    name = str(name or '')
    candidates: list[str] = []

    def add(x: str):
        x = str(x or '')
        if x and x not in candidates:
            candidates.append(x)

    add(name)
    norm = _normalize_epub_href(name)
    add(norm)
    if norm and not norm.startswith('OEBPS/'):
        add('OEBPS/' + norm)
    if spine_lookup is not None:
        add(_resolve_container_name(spine_lookup, name))
        add(_resolve_container_name(spine_lookup, norm))
    for cand in candidates:
        try:
            text, anchors = _epub_item_text(container, cand, cache)
            if text:
                return text, anchors, cand
        except Exception:
            continue
    return '', {}, name


def _spine_position_map(container) -> dict[str, int]:
    return {name: idx for idx, (name, is_linear) in enumerate(container.spine_names) if is_linear}


def _normalize_epub_href(href: str) -> str:
    href = str(href or '').strip().replace('\\', '/')
    href = href.split('?', 1)[0]
    while href.startswith('./'):
        href = href[2:]
    while href.startswith('/'):
        href = href[1:]
    try:
        href = unquote(href)
    except Exception:
        pass
    return href


def _epub_href_variants(href: str) -> list[str]:
    href = _normalize_epub_href(href)
    if not href:
        return []
    parts = [href]
    if href.startswith('OEBPS/'):
        parts.append(href[6:])
    basename = href.rsplit('/', 1)[-1] if '/' in href else href
    if basename and basename != href:
        parts.append(basename)
    return parts


def _spine_name_lookup(container) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for name, is_linear in container.spine_names:
        if not is_linear:
            continue
        for key in _epub_href_variants(name):
            folded = key.casefold()
            if folded not in mapping:
                mapping[folded] = name
    return mapping


def _resolve_container_name(spine_lookup: dict[str, str], dest: str) -> str:
    for key in _epub_href_variants(dest):
        resolved = spine_lookup.get(key.casefold())
        if resolved:
            return resolved
    return str(dest or '')



def _toc_node_sort_key(
    container,
    node,
    spine_map: dict[str, int],
    spine_lookup: dict[str, str] | None,
    cache: dict[str, tuple[str, dict[str, int]]],
    fallback_index: int,
) -> tuple[int, int, int]:
    dest = str(getattr(node, 'dest', '') or '')
    frag = str(getattr(node, 'frag', '') or '')
    resolved_dest = dest
    if spine_lookup is not None and resolved_dest and resolved_dest not in spine_map:
        resolved_dest = _resolve_container_name(spine_lookup, resolved_dest)
    spine_index = int(spine_map.get(resolved_dest, 10 ** 9))
    anchor_index = 0
    if resolved_dest and resolved_dest in spine_map and frag:
        try:
            _text, anchors = _epub_item_text(container, resolved_dest, cache)
            anchor_index = int(anchors.get(frag, 0) or 0)
        except Exception:
            anchor_index = 0
    return (spine_index, anchor_index, int(fallback_index))



def _extract_epub_sections_from_toc(
    container,
    toc,
    selected_hrefs: set[str] | None = None,
    selected_indices: set[int] | None = None,
) -> list[ChapterSection]:
    explicit_selection = selected_hrefs is not None
    # Build an absolute index map from the full TOC so resumed partial runs keep correct ordering.
    def _walk_all(node) -> list[object]:
        ans: list[object] = []
        for child in list(node):
            if getattr(child, 'dest', None):
                ans.append(child)
            ans.extend(_walk_all(child))
        return ans

    all_toc_nodes = _walk_all(toc)
    absolute_index_map: dict[int, int] = {}
    for abs_idx, node in enumerate(all_toc_nodes, start=1):
        absolute_index_map[id(node)] = abs_idx

    selected_indices = set(selected_indices or set())

    if selected_hrefs is None:
        nodes = _toc_candidates(toc)
        if len(nodes) < 2:
            return []
    else:
        # Explicit selection: honor what the user checked, even if it is front/back matter.
        selected_hrefs = set(selected_hrefs)
        normalized_selected_hrefs: set[str] = set()
        normalized_selected_dests: set[str] = set()
        for href in selected_hrefs:
            normalized = _normalize_epub_href(href)
            if not normalized:
                continue
            if '#' in normalized:
                dest_part, frag_part = normalized.split('#', 1)
                if dest_part and frag_part:
                    for variant in _epub_href_variants(dest_part):
                        normalized_selected_hrefs.add(f'{variant}#{frag_part}')
                else:
                    normalized_selected_hrefs.add(normalized)
            else:
                normalized_selected_dests.add(normalized)
                for variant in _epub_href_variants(normalized):
                    normalized_selected_dests.add(variant)
        normalized_selected_hrefs_folded = {x.casefold() for x in normalized_selected_hrefs if x}
        normalized_selected_dests_folded = {x.casefold() for x in normalized_selected_dests if x}
        nodes = []
        seen: set[tuple[str, str]] = set()
        for node in all_toc_nodes:
            href = (node.dest or '') + (('#' + (node.frag or '')) if (node.frag or '') else '')
            key = (node.dest or '', node.frag or '')
            if not href or key in seen:
                continue
            href_norm = _normalize_epub_href(href)
            dest_norm = _normalize_epub_href(str(node.dest or ''))
            frag_norm = str(node.frag or '').strip()
            dest_variants = _epub_href_variants(dest_norm)
            href_matches = href_norm.casefold() in normalized_selected_hrefs_folded
            if frag_norm:
                href_matches = href_matches or any(f'{v}#{frag_norm}'.casefold() in normalized_selected_hrefs_folded for v in dest_variants)
            dest_matches = dest_norm.casefold() in normalized_selected_dests_folded or any(v.casefold() in normalized_selected_dests_folded for v in dest_variants)
            if (
                not href_matches
                and not dest_matches
            ):
                continue
            seen.add(key)
            nodes.append(node)
        if len(nodes) < 1 and not selected_indices:
            _debug_log(f'Explicit selection matched 0 ToC nodes. selected_hrefs={len(selected_hrefs)} selected_indices={len(selected_indices or set())}')
            return []

    if selected_indices:
        if selected_hrefs is None:
            indexed_nodes = _toc_candidates(toc)
        else:
            indexed_nodes = all_toc_nodes
        index_selected: list[object] = []
        seen_ids: set[int] = set()
        for abs_idx, node in enumerate(indexed_nodes, start=1):
            if abs_idx not in selected_indices:
                continue
            marker = id(node)
            if marker in seen_ids:
                continue
            seen_ids.add(marker)
            index_selected.append(node)
        if selected_hrefs is None:
            nodes = index_selected
        else:
            existing = {id(node) for node in nodes}
            for node in index_selected:
                marker = id(node)
                if marker not in existing:
                    nodes.append(node)
                    existing.add(marker)
        if len(nodes) < 1:
            return []
    spine_map = _spine_position_map(container)
    spine_lookup = _spine_name_lookup(container)
    if explicit_selection:
        _debug_log(f'Extracting selected sections: toc_nodes={len(all_toc_nodes)} matched_nodes={len(nodes)} selected_indices={len(selected_indices)}')
    cache: dict[str, tuple[str, dict[str, int]]] = {}
    spine_entries: list[tuple[int, str]] = []
    try:
        for spine_idx, (name, is_linear) in enumerate(container.spine_names):
            if is_linear:
                spine_entries.append((int(spine_idx), str(name)))
    except Exception:
        spine_entries = []

    boundary_nodes = _toc_candidates(toc)
    if not boundary_nodes:
        boundary_nodes = [n for n in all_toc_nodes if getattr(n, 'dest', None)]
    try:
        boundary_nodes = [
            node for _fallback_index, node in sorted(
                enumerate(list(boundary_nodes)),
                key=lambda pair: _toc_node_sort_key(container, pair[1], spine_map, spine_lookup, cache, pair[0]),
            )
        ]
    except Exception:
        pass
    boundary_pos_by_id: dict[int, int] = {id(n): i for i, n in enumerate(boundary_nodes)}
    boundary_pos_by_key: dict[tuple[str, str], int] = {}
    for i, n in enumerate(boundary_nodes):
        try:
            boundary_pos_by_key[(_normalize_epub_href(str(getattr(n, 'dest', '') or '')), str(getattr(n, 'frag', '') or ''))] = i
        except Exception:
            pass

    def _next_boundary_for(node) -> object | None:
        pos = boundary_pos_by_id.get(id(node))
        if pos is None:
            pos = boundary_pos_by_key.get((_normalize_epub_href(str(getattr(node, 'dest', '') or '')), str(getattr(node, 'frag', '') or '')))
        if pos is None:
            return None
        nxt = pos + 1
        if 0 <= nxt < len(boundary_nodes):
            return boundary_nodes[nxt]
        return None
    try:
        nodes = [
            node for _fallback_index, node in sorted(
                enumerate(list(nodes)),
                key=lambda pair: _toc_node_sort_key(container, pair[1], spine_map, spine_lookup, cache, pair[0]),
            )
        ]
    except Exception:
        pass
    sections: list[ChapterSection] = []
    for _idx, node in enumerate(nodes):
        resolved_dest = _resolve_container_name(spine_lookup, str(getattr(node, 'dest', '') or ''))
        if resolved_dest not in spine_map and not explicit_selection:
            continue
        text, anchors, used_dest = _try_epub_item_text(container, resolved_dest, cache, spine_lookup=spine_lookup)
        if not text:
            if explicit_selection:
                _debug_log(f'Skipping selected node: could not load dest="{resolved_dest}" title="{str(getattr(node, "title", "") or "").strip()}"')
            continue
        start_anchor = anchors.get(node.frag or '', 0)
        start_spine_idx = spine_map.get(used_dest, spine_map.get(resolved_dest))

        nxt_node = _next_boundary_for(node)
        nxt_dest = str(getattr(nxt_node, 'dest', '') or '') if nxt_node is not None else ''
        nxt_frag = str(getattr(nxt_node, 'frag', '') or '') if nxt_node is not None else ''
        nxt_resolved_dest = _resolve_container_name(spine_lookup, nxt_dest) if nxt_dest else ''
        nxt_spine_idx = spine_map.get(nxt_resolved_dest) if nxt_resolved_dest else None

        pieces: list[str] = []
        end_anchor_same_doc = None
        if nxt_node is not None and nxt_resolved_dest and nxt_resolved_dest == used_dest and nxt_frag:
            end_anchor_same_doc = anchors.get(nxt_frag, None)
            if end_anchor_same_doc is not None and int(end_anchor_same_doc) <= int(start_anchor):
                end_anchor_same_doc = None

        if end_anchor_same_doc is not None:
            pieces.append(text[int(start_anchor):int(end_anchor_same_doc)])
        else:
            pieces.append(text[int(start_anchor):])
            if start_spine_idx is not None:
                try:
                    start_spine_idx = int(start_spine_idx)
                except Exception:
                    start_spine_idx = None
            if nxt_spine_idx is not None:
                try:
                    nxt_spine_idx = int(nxt_spine_idx)
                except Exception:
                    nxt_spine_idx = None
            if start_spine_idx is not None:
                end_exclusive = nxt_spine_idx if (nxt_spine_idx is not None and nxt_spine_idx > start_spine_idx) else None
                for spine_idx, spine_name in spine_entries:
                    if spine_idx <= start_spine_idx:
                        continue
                    if end_exclusive is not None and spine_idx >= end_exclusive:
                        break
                    t, _a, _used = _try_epub_item_text(container, spine_name, cache, spine_lookup=spine_lookup)
                    if t:
                        pieces.append(t)

        chunk = normalize_text('\n\n'.join(pieces))
        chunk_words = len(chunk.split())
        if chunk_words < 80 and not explicit_selection:
            continue
        if chunk_words < 80 and explicit_selection:
            _debug_log(f'Selected node content is short ({chunk_words} words) dest="{used_dest}" title="{str(getattr(node, "title", "") or "").strip()}"')
        abs_index = absolute_index_map.get(id(node), len(sections) + 1)
        title = _safe_title(node.title or '', f'Chapter {abs_index}')
        # If the TOC title is generic or a meaningless filename stem, try to enrich it from the section content.
        is_generic = bool(re.match(r'^\s*(?:chapter|cap[ií]tulo|capitulo|part|parte|section|se[cç][aã]o|secao|article|artigo)\s+(?:[0-9]+|[ivxlcdm]+)\s*$', title, flags=re.IGNORECASE))
        if is_generic or _is_meaningless_stem(title):
            guessed = _guess_epub_section_title(node.dest, chunk, abs_index)
            if guessed and len(guessed) > len(title) + 3:
                title = guessed
            elif _is_meaningless_stem(title):
                title = f'Chapter {abs_index}'
        href = (node.dest or '') + (('#' + (node.frag or '')) if (node.frag or '') else '')
        sections.append(ChapterSection(index=abs_index, title=title, text=chunk, identifier=href or f'index:{abs_index}'))
    return sections


def _guess_epub_section_title(name: str, text: str, index: int) -> str:
    lines = [ln.strip() for ln in text.splitlines()[:18] if ln.strip()]
    if lines:
        first = lines[0]
        lowered = first.lower()
        if 3 <= len(first) <= 120 and not any(token in lowered for token in ('copyright', 'all rights reserved')):
            # If the first line is a plain chapter heading and a subtitle follows, merge them.
            if _HEADING_RE.match(first) and re.match(r'^\s*(?:chapter|cap[ií]tulo|capitulo|part|parte|section|se[cç][aã]o|secao|article|artigo)\s+(?:[0-9]+|[ivxlcdm]+)\s*$', first, flags=re.IGNORECASE):
                if len(lines) > 1:
                    nxt = lines[1]
                    if _SHORT_TITLE_RE.match(nxt) and len(nxt.split()) <= 14 and not _HEADING_RE.match(nxt):
                        return _clean_heading(f'{first}: {nxt}', index)
            return _clean_heading(first, index)
    stem = os.path.splitext(os.path.basename(name))[0].replace('_', ' ').replace('-', ' ').strip()
    if _is_meaningless_stem(stem):
        return f'Section {index}'
    return _safe_title(stem, f'Section {index}')


def _fallback_epub_sections(container) -> list[ChapterSection]:
    cache: dict[str, tuple[str, dict[str, int]]] = {}
    sections: list[ChapterSection] = []
    for name, is_linear in container.spine_names:
        if not is_linear:
            continue
        text, _ = _epub_item_text(container, name, cache)
        if len(text.split()) < 120:
            continue
        idx = len(sections) + 1
        sections.append(ChapterSection(index=idx, title=_guess_epub_section_title(name, text, idx), text=text, identifier=f'{name}'))
    return sections


def _parse_container_based_document(path: str, title: str = '', selected_chapter_ids: set[str] | None = None, format_name: str = 'epub') -> ParsedDocument | None:
    """Parse EPUB, KEPUB, or AZW3 files using calibre's container API.

    AZW3 (KF8) files are supported by calibre's get_container() and can extract ToC just like EPUB.
    """
    try:
        from calibre.ebooks.oeb.polish.container import get_container
        from calibre.ebooks.oeb.polish.toc import get_toc
    except Exception:
        return None
    container = None
    try:
        container = get_container(path, tweak_mode=True)
        guessed_title = title.strip() or guess_title(path)
        toc = get_toc(container, verify_destinations=False)
        if _debug_enabled():
            try:
                toc_nodes = len(list(toc.iterdescendants())) if toc is not None else 0
            except Exception:
                toc_nodes = 0
            _debug_log(f'container parse: format="{format_name}" toc_nodes={toc_nodes}')
        selected_hrefs = None
        selected_indices: set[int] = set()
        if selected_chapter_ids is not None:
            hrefs = {x for x in selected_chapter_ids if x and not x.startswith('index:')}
            for item in selected_chapter_ids:
                text = str(item or '').strip()
                if not text.startswith('index:'):
                    continue
                try:
                    selected_indices.add(int(text.split(':', 1)[1]))
                except Exception:
                    pass
            if hrefs:
                selected_hrefs = hrefs
            if _debug_enabled():
                sample = sorted(list(hrefs))[:6]
                _debug_log(f'container parse: selected_hrefs={len(hrefs)} selected_indices={len(selected_indices)} sample={sample}')
        chapters = (
            _extract_epub_sections_from_toc(
                container,
                toc,
                selected_hrefs=selected_hrefs,
                selected_indices=selected_indices,
            )
            if toc else []
        )
        if selected_chapter_ids is not None:
            _debug_log(f'container parse: extracted_chapters={len(chapters)}')
        if not chapters:
            # If a user explicitly selected chapters but none matched, don't silently fall back to full-book parsing.
            if selected_chapter_ids is not None:
                return ParsedDocument(
                    title=guessed_title,
                    source_path=path,
                    source_format=format_name,
                    raw_text='',
                    chapters=[],
                )
            chapters = _fallback_epub_sections(container)
            if selected_indices:
                chapters = [chapter for chapter in chapters if int(getattr(chapter, 'index', 0) or 0) in selected_indices]
        raw_text = normalize_text('\n\n'.join(chapter.text for chapter in chapters))
        if not raw_text:
            return None
        return ParsedDocument(
            title=guessed_title,
            source_path=path,
            source_format=format_name,
            raw_text=raw_text,
            chapters=chapters or fallback_sections(raw_text, title=guessed_title),
        )
    finally:
        root = getattr(container, 'root', '') if container is not None else ''
        if root and os.path.isdir(root):
            shutil.rmtree(root, ignore_errors=True)


def _decode_file(path: str) -> str:
    raw = open(path, 'rb').read()
    for enc in ('utf-8', 'utf-8-sig', 'cp1252', 'latin-1'):
        try:
            return raw.decode(enc)
        except Exception:
            pass
    return raw.decode('utf-8', 'replace')


def strip_html(text: str) -> str:
    stripper = _HTMLStripper()
    stripper.feed(text)
    return stripper.get_text()


def normalize_text(text: str) -> str:
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    text = text.replace('\x00', '')
    text = re.sub(r'\n{4,}', '\n\n\n', text)
    text = re.sub(r'[ \t]+\n', '\n', text)
    return text.strip()


def _char_offsets(lines: list[str]) -> list[int]:
    offsets = []
    pos = 0
    for line in lines:
        offsets.append(pos)
        pos += len(line) + 1
    return offsets


def _candidate_headings(lines: list[str]) -> list[tuple[int, int, str]]:
    offsets = _char_offsets(lines)
    ans: list[tuple[int, int, str]] = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        prev_blank = i == 0 or not lines[i - 1].strip()
        next_blank = i == len(lines) - 1 or not lines[i + 1].strip()
        if _HEADING_RE.match(stripped) and (prev_blank or next_blank):
            ans.append((i, offsets[i], stripped))
            continue
        if prev_blank and _NUMERIC_HEADING_RE.match(stripped):
            j = i + 1
            while j < len(lines) and not lines[j].strip():
                j += 1
            if j < len(lines):
                nxt = lines[j].strip()
                if _SHORT_TITLE_RE.match(nxt) and len(nxt.split()) <= 12:
                    ans.append((i, offsets[i], f'{stripped} — {nxt}'))
    deduped: list[tuple[int, int, str]] = []
    for item in ans:
        if deduped and item[1] - deduped[-1][1] < 120:
            continue
        deduped.append(item)
    return deduped


def _drop_probable_toc(candidates: list[tuple[int, int, str]], text_length: int) -> list[tuple[int, int, str]]:
    if len(candidates) < 3:
        return candidates
    while len(candidates) > 1 and candidates[0][1] < 800:
        candidates = candidates[1:]
    for idx in range(min(len(candidates) - 1, 12)):
        current = candidates[idx]
        nxt = candidates[idx + 1]
        if current[1] < text_length * 0.22 and nxt[1] - current[1] > 3000 and idx >= 1:
            return candidates[idx:]
    return candidates


def _repair_first_real_chapter(text: str, candidates: list[tuple[int, int, str]]) -> list[tuple[int, int, str]]:
    if len(candidates) < 2:
        return candidates
    first = candidates[0]
    second = candidates[1]
    if first[1] < len(text) * 0.1 and second[1] - first[1] > 10000:
        window = text[first[1]:second[1]]
        lines = window.split('\n')
        offsets = _char_offsets(lines)
        for i, line in enumerate(lines):
            stripped = line.strip()
            if not _NUMERIC_HEADING_RE.match(stripped):
                continue
            prev_blank = i == 0 or not lines[i - 1].strip()
            if not prev_blank:
                continue
            j = i + 1
            while j < len(lines) and not lines[j].strip():
                j += 1
            if j >= len(lines):
                continue
            nxt = lines[j].strip()
            if _SHORT_TITLE_RE.match(nxt) and len(nxt.split()) <= 12:
                pos = first[1] + offsets[i]
                title = f'{stripped} — {nxt}'
                candidates = list(candidates)
                candidates[0] = (first[0], pos, title)
                break
    return candidates


def split_into_chapters(text: str, title: str = '') -> list[ChapterSection]:
    text = normalize_text(text)
    if not text:
        return []
    lines = text.split('\n')
    candidates = _repair_first_real_chapter(text, _drop_probable_toc(_candidate_headings(lines), len(text)))
    chapters: list[ChapterSection] = []
    if len(candidates) >= 2:
        offsets = [c[1] for c in candidates] + [len(text)]
        for idx, candidate in enumerate(candidates):
            start = offsets[idx]
            end = offsets[idx + 1]
            chunk = text[start:end].strip()
            if len(chunk.split()) < 120:
                continue
            chapter_index = len(chapters) + 1
            chapters.append(ChapterSection(index=chapter_index, title=_clean_heading(candidate[2], chapter_index), text=chunk, identifier=f'index:{chapter_index}'))
    if chapters:
        return chapters
    return fallback_sections(text, title=title)


def _clean_heading(heading: str, index: int) -> str:
    heading = re.sub(r'\s+', ' ', heading).strip(' -–—:')
    if len(heading) > 120:
        heading = heading[:117].rstrip() + '...'
    return heading or f'Chapter {index}'


def fallback_sections(text: str, title: str = '') -> list[ChapterSection]:
    words = re.findall(r'\S+', text)
    total_words = len(words)
    if total_words <= 2200:
        label = title or 'Full text'
        return [ChapterSection(index=1, title=label, text=text.strip(), identifier='index:1')]
    target_sections = max(2, min(12, round(total_words / 3500)))
    per_section = max(1400, total_words // target_sections)
    sections: list[ChapterSection] = []
    current_words: list[str] = []
    current_count = 0
    for word in words:
        current_words.append(word)
        current_count += 1
        if current_count >= per_section:
            idx = len(sections) + 1
            sections.append(ChapterSection(index=idx, title=f'Section {idx}', text=' '.join(current_words).strip(), identifier=f'index:{idx}'))
            current_words = []
            current_count = 0
    if current_words:
        idx = len(sections) + 1
        sections.append(ChapterSection(index=idx, title=f'Section {idx}', text=' '.join(current_words).strip(), identifier=f'index:{idx}'))
    return sections


def parse_document(path: str, title: str = '', selected_chapter_ids: set[str] | None = None) -> ParsedDocument:
    ext = os.path.splitext(path)[1].lower()
    format_name = ext.lstrip('.')
    if selected_chapter_ids is not None:
        _debug_log(f'parse_document: path="{path}" format="{format_name}" selected_ids={len(selected_chapter_ids)}')

    # Try container-based parsing for EPUB, KEPUB, and AZW3
    if ext in {'.epub', '.kepub', '.azw3'}:
        parsed = _parse_container_based_document(path, title, selected_chapter_ids=selected_chapter_ids, format_name=format_name)
        if parsed is not None:
            if parsed.chapters:
                return parsed
            # Container parsing yielded nothing. For EPUB this means the user-selected hrefs
            # genuinely didn't match, so raise. For AZW3 the internal hrefs may not survive
            # across two separate get_container calls; fall through to text-based parsing.
            if ext != '.azw3':
                if selected_chapter_ids is not None:
                    _debug_log(f'parse_document: container parsing returned 0 chapters; raising No selected chapters matched the {format_name.upper()} ToC.')
                    raise ValueError(f'No selected chapters matched the {format_name.upper()} ToC.')
                return parsed  # empty book — return as-is

    # Fallback to text-based parsing for unsupported formats or when container parsing fails
    raw = normalize_text(read_text_from_path(path))
    guessed_title = title.strip() or guess_title(path, raw)
    chapters = split_into_chapters(raw, guessed_title)
    if selected_chapter_ids is not None:
        wanted: set[int] = set()
        for item in selected_chapter_ids:
            item = str(item or '').strip()
            if item.startswith('index:'):
                try:
                    wanted.add(int(item.split(':', 1)[1]))
                except Exception:
                    pass
        if wanted:
            chapters = [c for c in chapters if int(getattr(c, 'index', 0) or 0) in wanted]
            if not chapters:
                raise ValueError('No selected chapters matched the detected sections.')
            raw = normalize_text('\n\n'.join(chapter.text for chapter in chapters))
    return ParsedDocument(
        title=guessed_title,
        source_path=path,
        source_format=format_name,
        raw_text=raw,
        chapters=chapters,
    )


def chapter_overview(chapters: Iterable[ChapterSection]) -> str:
    return '\n'.join(f'{c.index}. {c.title} ({c.word_count} words)' for c in chapters)
