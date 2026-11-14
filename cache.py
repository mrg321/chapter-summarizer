#!/usr/bin/env python

from __future__ import annotations

__license__ = 'GPL v3'
__copyright__ = '2026, Comfy.n'
__docformat__ = 'restructuredtext en'

import glob
import hashlib
import html
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime

from calibre_plugins.chapter_summarizer.config import pref, set_pref

load_translations()

RUN_HISTORY_DB_NAME = 'chapter_summarizer_history.db'
CACHE_CUSTOM_DIR_PREF = 'cache_custom_dir'
CACHE_DIR_NAME = 'ChapterSummarizer'
CACHE_SUGGESTED_DIR_NAME = 'Chapter Summarizer Cache'
SQLITE_SIDE_SUFFIXES = ('', '-wal', '-shm')


def _normalize_dir_path(path: str) -> str:
    text = str(path or '').strip().strip('"')
    if not text:
        return ''
    try:
        text = os.path.expanduser(text)
    except Exception:
        pass
    try:
        return os.path.abspath(text)
    except Exception:
        return text


def _norm_path(path: str) -> str:
    text = _normalize_dir_path(path)
    if not text:
        return ''
    try:
        return os.path.normcase(os.path.realpath(text))
    except Exception:
        return os.path.normcase(text)


def _same_path(a: str, b: str) -> bool:
    aa = _norm_path(a)
    bb = _norm_path(b)
    return bool(aa and bb and aa == bb)


def _calibre_config_dir() -> str:
    try:
        from calibre.utils.config_base import config_dir  # type: ignore
        return str(config_dir or '')
    except Exception:
        return ''


def config_cache_path() -> str:
    config_dir = _calibre_config_dir()
    if config_dir:
        return os.path.join(config_dir, 'plugins', CACHE_DIR_NAME)
    return os.path.join(tempfile.gettempdir(), CACHE_DIR_NAME)


def suggested_external_cache_path() -> str:
    config_dir = _calibre_config_dir()
    try:
        tail = os.path.basename(str(config_dir or '').rstrip('\\/')) if config_dir else ''
    except Exception:
        tail = ''

    try:
        if tail.lower() == 'calibre settings' and config_dir:
            portable_root = os.path.dirname(str(config_dir).rstrip('\\/'))
            if portable_root:
                return os.path.join(portable_root, CACHE_SUGGESTED_DIR_NAME)
    except Exception:
        pass

    try:
        home = os.path.expanduser('~')
    except Exception:
        home = ''

    if os.name == 'nt':
        base = os.environ.get('LOCALAPPDATA') or home
    elif sys.platform == 'darwin':
        base = os.path.join(home, 'Library', 'Application Support') if home else ''
    else:
        base = os.environ.get('XDG_DATA_HOME') or (os.path.join(home, '.local', 'share') if home else '')

    if base:
        return os.path.join(base, CACHE_SUGGESTED_DIR_NAME)
    return os.path.join(tempfile.gettempdir(), CACHE_SUGGESTED_DIR_NAME)


def cache_location_details() -> dict[str, object]:
    config_path = config_cache_path()
    try:
        custom_value = str(pref(CACHE_CUSTOM_DIR_PREF, '') or '').strip()
    except Exception:
        custom_value = ''
    custom_path = _normalize_dir_path(custom_value)
    is_custom = bool(custom_path and not _same_path(custom_path, config_path))
    actual_path = custom_path if is_custom else config_path
    return {
        'path': actual_path,
        'config_path': config_path,
        'custom_path': custom_path if is_custom else '',
        'is_custom': is_custom,
        'suggested_path': suggested_external_cache_path(),
    }


def _iter_cache_storage_files(base_dir: str) -> list[str]:
    base_dir = _normalize_dir_path(base_dir)
    if not base_dir or not os.path.isdir(base_dir):
        return []
    files: list[str] = []
    seen: set[str] = set()
    for db_path in sorted(glob.glob(os.path.join(base_dir, '*.db'))):
        for suffix in SQLITE_SIDE_SUFFIXES:
            candidate = db_path if not suffix else db_path + suffix
            try:
                if not os.path.isfile(candidate):
                    continue
            except Exception:
                continue
            marker = _norm_path(candidate)
            if marker in seen:
                continue
            seen.add(marker)
            files.append(candidate)
    return files


def _copy_file_best_effort(src: str, dst: str) -> bool:
    try:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
    except Exception:
        pass
    try:
        shutil.copy2(src, dst)
        return True
    except Exception:
        return False


def relocate_cache_storage(target_dir: str) -> dict[str, object]:
    details = cache_location_details()
    source_dir = default_cache_path()
    config_dir = str(details.get('config_path') or '')
    target_dir = _normalize_dir_path(target_dir) or config_dir
    use_custom = bool(target_dir and config_dir and not _same_path(target_dir, config_dir))
    if not target_dir:
        raise ValueError(_('Please choose a cache folder.'))

    try:
        os.makedirs(target_dir, exist_ok=True)
    except Exception as e:
        raise ValueError(_('Could not create the selected cache folder.\n\n%s') % str(e))

    if _same_path(source_dir, target_dir):
        set_pref(CACHE_CUSTOM_DIR_PREF, target_dir if use_custom else '')
        return {
            'source_dir': source_dir,
            'target_dir': target_dir,
            'moved_files': 0,
            'leftover_files': [],
            'used_custom_path': use_custom,
        }

    source_files = _iter_cache_storage_files(source_dir)
    if source_files:
        conflicts = [
            os.path.basename(src)
            for src in source_files
            if os.path.exists(os.path.join(target_dir, os.path.basename(src)))
        ]
        if conflicts:
            raise ValueError(
                _('The selected folder already contains Chapter Summarizer cache database files with the same names.\n\n'
                  'Choose a different folder, or move/rename the existing files first.\n\nConflicts:\n%s')
                % '\n'.join(conflicts[:12])
            )

        copied_paths: list[str] = []
        failed: list[str] = []
        for src in source_files:
            dst = os.path.join(target_dir, os.path.basename(src))
            if _copy_file_best_effort(src, dst):
                copied_paths.append(dst)
            else:
                failed.append(os.path.basename(src))
        if failed:
            for copied in copied_paths:
                try:
                    os.remove(copied)
                except Exception:
                    pass
            raise ValueError(
                _('Could not copy the existing cache database files to the selected folder.\n\n'
                  'Close any Chapter Summarizer cache windows or active jobs and try again.\n\nFiles not copied:\n%s')
                % '\n'.join(failed[:12])
            )

    set_pref(CACHE_CUSTOM_DIR_PREF, target_dir if use_custom else '')

    leftovers: list[str] = []
    for src in source_files:
        try:
            os.remove(src)
        except Exception:
            leftovers.append(os.path.basename(src))

    try:
        if source_dir and not _same_path(source_dir, config_dir) and not _same_path(source_dir, target_dir):
            if os.path.isdir(source_dir) and not os.listdir(source_dir):
                os.rmdir(source_dir)
    except Exception:
        pass

    return {
        'source_dir': source_dir,
        'target_dir': target_dir,
        'moved_files': len(source_files),
        'leftover_files': leftovers,
        'used_custom_path': use_custom,
    }


def _plain_text(value: str) -> str:
    text = str(value or '').strip()
    if not text:
        return ''
    if '<' in text and '>' in text:
        text = re.sub(r'<[^>]+>', ' ', text)
        text = html.unescape(text)
    return ' '.join(text.split())


def _guess_library_path_from_source(source_path: str) -> str:
    source_path = str(source_path or '').strip()
    if not source_path:
        return ''
    try:
        abs_path = os.path.abspath(source_path)
    except Exception:
        abs_path = source_path
    current = os.path.dirname(abs_path)
    seen: set[str] = set()
    while current and current not in seen:
        seen.add(current)
        try:
            if os.path.isfile(os.path.join(current, 'metadata.db')):
                return current
        except Exception:
            pass
        parent = os.path.dirname(current)
        if not parent or parent == current:
            break
        current = parent
    try:
        book_dir = os.path.dirname(abs_path)
        author_dir = os.path.dirname(book_dir)
        library_dir = os.path.dirname(author_dir)
        if library_dir and library_dir not in (author_dir, book_dir):
            if re.search(r'\(\d+\)\s*$', os.path.basename(book_dir)):
                return library_dir
    except Exception:
        pass
    return ''


def _cache_model_label(cache) -> str:
    model_used = _plain_text(cache.get_info('model_used') or '')
    requested_model = _plain_text(cache.get_info('requested_model') or '')
    return model_used or requested_model


def _cache_source_library_path(cache) -> str:
    source_library_path = str(cache.get_info('source_library_path') or '').strip()
    if source_library_path:
        return source_library_path
    source_path = str(cache.get_info('source_path') or '').strip()
    derived = _guess_library_path_from_source(source_path)
    if derived:
        try:
            cache.set_info('source_library_path', derived)
        except Exception:
            pass
    return derived

def _iter_cache_db_paths() -> list[str]:
    paths: list[str] = []
    for file_path in glob.glob(os.path.join(default_cache_path(), '*.db')):
        try:
            if os.path.basename(file_path).lower() == RUN_HISTORY_DB_NAME:
                continue
        except Exception:
            pass
        paths.append(file_path)
    return paths


def default_cache_path() -> str:
    """Return the active persistent cache directory.

    By default this is the plugin's folder under calibre's configuration
    directory. When the user chooses a custom cache folder, that location is
    used instead. If the custom folder cannot be created, we fall back to the
    calibre-config location, then finally to the system temp directory.
    """
    details = cache_location_details()
    candidates = [
        str(details.get('path') or ''),
        str(details.get('config_path') or ''),
        os.path.join(tempfile.gettempdir(), CACHE_DIR_NAME),
    ]
    for candidate in candidates:
        candidate = _normalize_dir_path(candidate)
        if not candidate:
            continue
        try:
            os.makedirs(candidate, exist_ok=True)
            return candidate
        except Exception:
            pass
    return os.path.join(tempfile.gettempdir(), CACHE_DIR_NAME)


def source_fingerprint(path: str) -> str:
    try:
        st = os.stat(path)
        # Intentionally ignore mtime — calibre/plugins may touch files without changing content.
        raw = f'{os.path.abspath(path)}|{int(st.st_size)}'
    except Exception:
        raw = os.path.abspath(path or '')
    return hashlib.sha1(raw.encode('utf-8', 'replace')).hexdigest()


def build_cache_profile_key(
    source_path: str,
    provider_name: str,
    requested_model: str,
    target_language: str,
    base_paragraphs: int,
    include_highlight: bool,
    max_chunk_words: int,
    flash_mode: bool = False,
) -> str:
    # Use the normalised absolute path only — never include file size or mtime.
    # File size changes (e.g. calibre metadata writes) used to create a new DB
    # every time the epub was touched, silently fragmenting the cache.
    norm_path = os.path.normcase(os.path.abspath(source_path or ''))
    raw = '|'.join([
        norm_path,
        str(provider_name or '').strip(),
        str(requested_model or '').strip(),
        str(target_language or '').strip(),
        str(int(base_paragraphs or 0)),
        '1' if include_highlight else '0',
        str(int(max_chunk_words or 0)),
        'flash' if flash_mode else 'chapters',
    ])
    return hashlib.sha1(raw.encode('utf-8', 'replace')).hexdigest()


class ChapterSummaryCache:

    def __init__(self, profile_key: str):
        self.profile_key = str(profile_key or '').strip() or 'default'
        self.dir_path = default_cache_path()
        self.file_path = os.path.join(self.dir_path, self.profile_key + '.db')
        self.connection = sqlite3.connect(self.file_path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.cursor = self.connection.cursor()
        self.cursor.execute(
            'CREATE TABLE IF NOT EXISTS info(key TEXT PRIMARY KEY, value TEXT)'
        )
        self.cursor.execute(
            'CREATE TABLE IF NOT EXISTS chapter_summaries('
            'chapter_id TEXT PRIMARY KEY, '
            'title TEXT, '
            'summary TEXT, '
            'highlight TEXT, '
            'word_count INTEGER DEFAULT 0, '
            'provider_name TEXT, '
            'model_used TEXT, '
            'requested_model TEXT, '
            'request_count INTEGER DEFAULT 0, '
            'request_attempts INTEGER DEFAULT 0, '
            'duration_seconds REAL DEFAULT 0, '
            'created_at TEXT, '
            'source_hash TEXT, '
            'preview TEXT DEFAULT "", '
            'toc_index INTEGER DEFAULT -1'
            ')'
        )
        # Migrate older DBs that were created before toc_index was added.
        try:
            self.cursor.execute('ALTER TABLE chapter_summaries ADD COLUMN toc_index INTEGER DEFAULT -1')
        except Exception:
            pass  # column already exists
        self.connection.commit()

    # ------------------------------------------------------------------
    # Class-level helpers for the cache manager UI
    # ------------------------------------------------------------------

    @classmethod
    def get_list(cls) -> list[dict]:
        """Return metadata for every cached DB file, newest-updated first."""
        result = []
        for file_path in sorted(_iter_cache_db_paths()):
            profile_key = os.path.splitext(os.path.basename(file_path))[0]
            try:
                cache = cls(profile_key)
                title = cache.get_info('source_title') or '[Unknown]'
                provider = cache.get_info('provider_name') or ''
                model = _cache_model_label(cache)
                updated_at = cache.get_info('updated_at') or ''
                source_path = cache.get_info('source_path') or ''
                source_library_path = _cache_source_library_path(cache)
                source_book_id = cache.get_info('source_book_id') or ''
                source_format = cache.get_info('source_format') or ''
                chapter_count = cache.cursor.execute(
                    'SELECT COUNT(*) FROM chapter_summaries').fetchone()[0]
                size_bytes = os.path.getsize(file_path)
                cache.close()
                result.append({
                    'profile_key': profile_key,
                    'file_path': file_path,
                    'filename': os.path.basename(file_path),
                    'title': title,
                    'provider': provider,
                    'model': model,
                    'updated_at': updated_at,
                    'source_path': source_path,
                    'source_library_path': source_library_path,
                    'source_book_id': source_book_id,
                    'source_format': source_format,
                    'chapter_count': int(chapter_count),
                    'size_bytes': int(size_bytes),
                })
            except Exception:
                pass
        result.sort(key=lambda d: d.get('updated_at') or '', reverse=True)
        return result

    @classmethod
    def find_by_source(cls, source_path: str) -> list[dict]:
        """Find all cache DBs whose stored source_path matches *source_path*.

        Returns a list of dicts (same shape as get_list entries) sorted by
        usefulness then updated_at descending. Flash-only caches with stored
        rendered_text are included even when they have zero chapter rows.
        """
        if not source_path:
            return []
        norm = os.path.normcase(os.path.abspath(source_path))
        result = []
        for file_path in _iter_cache_db_paths():
            profile_key = os.path.splitext(os.path.basename(file_path))[0]
            cache = None
            try:
                cache = cls(profile_key)
                stored = cache.get_info('source_path') or ''
                if not stored:
                    continue
                if os.path.normcase(os.path.abspath(stored)) != norm:
                    continue
                chapter_count = int(cache.cursor.execute(
                    'SELECT COUNT(*) FROM chapter_summaries').fetchone()[0] or 0)
                rendered_text = str(cache.get_info('rendered_text') or '').strip()
                if chapter_count <= 0 and not rendered_text:
                    continue
                result.append({
                    'profile_key': profile_key,
                    'file_path': file_path,
                    'filename': os.path.basename(file_path),
                    'title': cache.get_info('source_title') or '[Unknown]',
                    'provider': cache.get_info('provider_name') or '',
                    'model': _cache_model_label(cache),
                    'updated_at': cache.get_info('updated_at') or '',
                    'source_path': stored,
                    'source_library_path': _cache_source_library_path(cache),
                    'source_book_id': cache.get_info('source_book_id') or '',
                    'source_format': cache.get_info('source_format') or '',
                    'chapter_count': chapter_count,
                    'rendered_text_len': len(rendered_text),
                    'size_bytes': int(os.path.getsize(file_path)),
                })
            except Exception:
                pass
            finally:
                try:
                    if cache is not None:
                        cache.close()
                except Exception:
                    pass
        result.sort(key=lambda d: (1 if int(d.get('chapter_count', 0) or 0) > 0 else 0, int(d.get('chapter_count', 0) or 0), 1 if int(d.get('rendered_text_len', 0) or 0) > 0 else 0, d.get('updated_at') or ''), reverse=True)
        return result

    @classmethod
    def delete(cls, profile_key: str) -> bool:
        """Delete the DB file for *profile_key*.  Returns True on success."""
        file_path = os.path.join(default_cache_path(), str(profile_key) + '.db')
        if os.path.exists(file_path):
            try:
                os.remove(file_path)
                return True
            except Exception:
                return False
        return False

    @classmethod
    def clear_all(cls) -> int:
        """Delete all DB files.  Returns count of files removed."""
        count = 0
        for file_path in _iter_cache_db_paths():
            try:
                os.remove(file_path)
                count += 1
            except Exception:
                pass
        return count

    @classmethod
    def total_size_bytes(cls) -> int:
        """Return total bytes used by all cache DB files."""
        total = 0
        for file_path in _iter_cache_db_paths():
            try:
                total += os.path.getsize(file_path)
            except Exception:
                pass
        return total

    def close(self):
        try:
            self.cursor.close()
        except Exception:
            pass
        try:
            self.connection.commit()
        except Exception:
            pass
        try:
            self.connection.close()
        except Exception:
            pass

    def set_info(self, key: str, value: str):
        self.cursor.execute(
            'INSERT INTO info(key, value) VALUES (?, ?) '
            'ON CONFLICT(key) DO UPDATE SET value=excluded.value',
            (str(key), str(value)),
        )
        self.connection.commit()

    def get_info(self, key: str, default: str = '') -> str:
        row = self.cursor.execute('SELECT value FROM info WHERE key=?', (str(key),)).fetchone()
        return str(row['value']) if row and row['value'] is not None else str(default)

    def upsert_chapter(self, data: dict[str, object]):
        toc_index = data.get('toc_index', -1)
        try:
            toc_index = int(toc_index) if toc_index is not None else -1
        except Exception:
            toc_index = -1
        self.cursor.execute(
            'INSERT INTO chapter_summaries('
            'chapter_id, title, summary, highlight, word_count, provider_name, model_used, requested_model, '
            'request_count, request_attempts, duration_seconds, created_at, source_hash, preview, toc_index'
            ') VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) '
            'ON CONFLICT(chapter_id) DO UPDATE SET '
            'title=excluded.title, '
            'summary=excluded.summary, '
            'highlight=excluded.highlight, '
            'word_count=excluded.word_count, '
            'provider_name=excluded.provider_name, '
            'model_used=excluded.model_used, '
            'requested_model=excluded.requested_model, '
            'request_count=excluded.request_count, '
            'request_attempts=excluded.request_attempts, '
            'duration_seconds=excluded.duration_seconds, '
            'created_at=excluded.created_at, '
            'source_hash=excluded.source_hash, '
            'preview=excluded.preview, '
            'toc_index=excluded.toc_index',
            (
                str(data.get('chapter_id') or ''),
                str(data.get('title') or ''),
                str(data.get('summary') or ''),
                str(data.get('highlight') or ''),
                int(data.get('word_count') or 0),
                str(data.get('provider_name') or ''),
                str(data.get('model_used') or ''),
                str(data.get('requested_model') or ''),
                int(data.get('request_count') or 0),
                int(data.get('request_attempts') or 0),
                float(data.get('duration_seconds') or 0.0),
                str(data.get('created_at') or datetime.now().strftime('%Y-%m-%d %H:%M:%S')),
                str(data.get('source_hash') or ''),
                str(data.get('preview') or ''),
                toc_index,
            ),
        )
        self.connection.commit()

    def all_entries(self) -> dict[str, dict[str, object]]:
        rows = self.cursor.execute('SELECT * FROM chapter_summaries ORDER BY title COLLATE NOCASE').fetchall()
        ans: dict[str, dict[str, object]] = {}
        for row in rows:
            item = dict(row)
            chapter_id = str(item.get('chapter_id') or '')
            if chapter_id:
                ans[chapter_id] = item
        return ans

    def delete_chapters(self, chapter_ids: list[str]) -> int:
        if not chapter_ids:
            return 0
        placeholders = ','.join('?' * len(chapter_ids))
        cur = self.cursor.execute(
            f'DELETE FROM chapter_summaries WHERE chapter_id IN ({placeholders})',
            [str(cid) for cid in chapter_ids],
        )
        self.connection.commit()
        return int(getattr(cur, 'rowcount', 0) or 0)


class RunHistoryDB:
    """Persistent store of Chapter Summarizer runs.

    Stored separately from per-profile chapter caches so we can list runs for
    a title regardless of cache settings.
    """

    def __init__(self):
        self.file_path = os.path.join(default_cache_path(), 'chapter_summarizer_history.db')
        self.connection = sqlite3.connect(self.file_path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.cursor = self.connection.cursor()
        self._init_schema()

    def _init_schema(self):
        self.cursor.execute(
            'CREATE TABLE IF NOT EXISTS runs('
            'id INTEGER PRIMARY KEY AUTOINCREMENT, '
            'source_path TEXT, '
            'source_norm TEXT, '
            'source_title TEXT, '
            'source_book_id TEXT, '
            'source_format TEXT, '
            'provider_name TEXT, '
            'requested_model TEXT, '
            'effective_model_label TEXT, '
            'model_used TEXT, '
            'target_language TEXT, '
            'selected_nodes INTEGER, '
            'total_nodes INTEGER, '
            'summarized_nodes INTEGER, '
            'status TEXT, '
            'error_message TEXT, '
            'started_at TEXT, '
            'finished_at TEXT, '
            'duration_seconds REAL'
            ')'
        )
        self.cursor.execute('CREATE INDEX IF NOT EXISTS runs_source_norm_idx ON runs(source_norm)')
        self.cursor.execute('CREATE INDEX IF NOT EXISTS runs_source_book_id_idx ON runs(source_book_id)')
        self.cursor.execute('CREATE INDEX IF NOT EXISTS runs_started_at_idx ON runs(started_at)')
        self.connection.commit()

    @staticmethod
    def _norm(path: str) -> str:
        try:
            return os.path.normcase(os.path.abspath(path or ''))
        except Exception:
            return str(path or '')

    def close(self):
        try:
            self.cursor.close()
        except Exception:
            pass
        try:
            self.connection.commit()
        except Exception:
            pass
        try:
            self.connection.close()
        except Exception:
            pass

    def record_start(
        self,
        *,
        source_path: str,
        source_title: str,
        source_book_id: str = '',
        source_format: str = '',
        provider_name: str = '',
        requested_model: str = '',
        effective_model_label: str = '',
        target_language: str = '',
        selected_nodes: int | None = None,
        total_nodes: int | None = None,
        started_at: str | None = None,
    ) -> int:
        started_at = started_at or datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        self.cursor.execute(
            'INSERT INTO runs('
            'source_path, source_norm, source_title, source_book_id, source_format, '
            'provider_name, requested_model, effective_model_label, model_used, target_language, '
            'selected_nodes, total_nodes, summarized_nodes, status, error_message, '
            'started_at, finished_at, duration_seconds'
            ') VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (
                str(source_path or ''),
                self._norm(source_path),
                str(source_title or ''),
                str(source_book_id or ''),
                str(source_format or ''),
                str(provider_name or ''),
                str(requested_model or ''),
                str(effective_model_label or ''),
                '',
                str(target_language or ''),
                None if selected_nodes is None else int(selected_nodes),
                None if total_nodes is None else int(total_nodes),
                None,
                'running',
                '',
                str(started_at),
                '',
                None,
            ),
        )
        self.connection.commit()
        rid = self.cursor.execute('SELECT last_insert_rowid()').fetchone()[0]
        return int(rid)

    def record_finish(
        self,
        run_id: int,
        *,
        status: str,
        summarized_nodes: int | None = None,
        error_message: str = '',
        model_used: str = '',
        finished_at: str | None = None,
        duration_seconds: float | None = None,
    ) -> None:
        finished_at = finished_at or datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        self.cursor.execute(
            'UPDATE runs SET status=?, summarized_nodes=?, error_message=?, model_used=?, finished_at=?, duration_seconds=? '
            'WHERE id=?',
            (
                str(status or ''),
                None if summarized_nodes is None else int(summarized_nodes),
                str(error_message or ''),
                str(model_used or ''),
                str(finished_at),
                None if duration_seconds is None else float(duration_seconds),
                int(run_id),
            ),
        )
        self.connection.commit()

    def list_for_source(self, *, source_path: str = '', source_book_id: str = '', limit: int = 250) -> list[dict]:
        limit = max(1, min(int(limit or 250), 2000))
        rows = []
        if source_book_id:
            rows = self.cursor.execute(
                'SELECT * FROM runs WHERE source_book_id=? ORDER BY started_at DESC LIMIT ?',
                (str(source_book_id), limit),
            ).fetchall()
        elif source_path:
            rows = self.cursor.execute(
                'SELECT * FROM runs WHERE source_norm=? ORDER BY started_at DESC LIMIT ?',
                (self._norm(source_path), limit),
            ).fetchall()
        return [dict(r) for r in (rows or [])]

    def last_run_for_source(self, *, source_path: str = '', source_book_id: str = '') -> dict | None:
        rows = self.list_for_source(source_path=source_path, source_book_id=source_book_id, limit=1)
        return rows[0] if rows else None

    def clear_for_source(self, *, source_path: str = '', source_book_id: str = '') -> int:
        if source_book_id:
            cur = self.cursor.execute('DELETE FROM runs WHERE source_book_id=?', (str(source_book_id),))
        elif source_path:
            cur = self.cursor.execute('DELETE FROM runs WHERE source_norm=?', (self._norm(source_path),))
        else:
            return 0
        self.connection.commit()
        try:
            return int(getattr(cur, 'rowcount', 0) or 0)
        except Exception:
            return 0
