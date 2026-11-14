#!/usr/bin/env python

from __future__ import annotations

__license__ = 'GPL v3'
__copyright__ = '2026, Comfy.n'
__docformat__ = 'restructuredtext en'

import html
import os
import re
import sqlite3
import time
import traceback
import shutil
import tempfile
from io import BytesIO
from datetime import datetime
from threading import Event, Thread

from qt.core import (
    QApplication,
    QByteArray,
    QBuffer,
    QButtonGroup,
    QCheckBox,
    QColor,
    QComboBox,
    QEvent,
    QFileDialog,
    QFrame,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QDialog,
    QDialogButtonBox,
    QIcon,
    QLabel,
    QLineEdit,
    QMenu,
    QObject,
    QPlainTextEdit,
    QProgressBar,
    QPalette,
    QPixmap,
    QPainter,
    QPen,
    QSize,
    QToolButton,
    QShortcut,
    QAbstractItemView,
    QHeaderView,
    QTreeWidget,
    QTreeWidgetItem,
    QUrl,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QSizePolicy,
    QTabWidget,
    QTimer,
    Qt,
    QVBoxLayout,
    QWidget,
    pyqtSignal,
    QTextBrowser,
)

def _call_get_icons(icon_name: str, plugin_name: str | None = None) -> QIcon | None:
    gi = globals().get('get_icons', None)
    if not callable(gi):
        try:
            from calibre.customize.zipplugin import get_icons as gi
        except Exception:
            gi = None
    if not callable(gi):
        return None

    icon_name = icon_name.replace('\\', '/')
    icon_name_backslash = icon_name.replace('/', '\\')
    icon_base = icon_name.replace('images/', '') if icon_name.startswith('images/') else icon_name
    icon_images = icon_name if icon_name.startswith('images/') else ('images/' + icon_name)
    icon_images_backslash = icon_images.replace('/', '\\')
    icon_base_backslash = icon_base.replace('/', '\\')

    candidates = []
    if plugin_name:
        candidates.append((icon_images, plugin_name))
        if icon_images_backslash != icon_images:
            candidates.append((icon_images_backslash, plugin_name))
        candidates.append((icon_name, plugin_name))
        if icon_name_backslash != icon_name:
            candidates.append((icon_name_backslash, plugin_name))
        candidates.append((icon_base, plugin_name))
        if icon_base_backslash != icon_base:
            candidates.append((icon_base_backslash, plugin_name))

    candidates.append((icon_images,))
    if icon_images_backslash != icon_images:
        candidates.append((icon_images_backslash,))
    candidates.append((icon_name,))
    if icon_name_backslash != icon_name:
        candidates.append((icon_name_backslash,))
    candidates.append((icon_base,))
    if icon_base_backslash != icon_base:
        candidates.append((icon_base_backslash,))

    for args in candidates:
        try:
            ic = gi(*args)
            if isinstance(ic, QIcon) and not ic.isNull():
                return ic
        except Exception:
            pass
    return None


def _load_plugin_icon(dark: bool) -> QIcon | None:
    icon_name = 'images/iconplugin_dark.png' if dark else 'images/iconplugin_light.png'
    return _call_get_icons(icon_name, 'Chapter Summarizer')


def _load_bundled_icon(icon_name: str) -> QIcon | None:
    return _call_get_icons(icon_name, 'Chapter Summarizer')


def _plugin_theme_icon() -> QIcon | None:
    try:
        app = QApplication.instance()
        palette = app.palette() if app is not None else None
        if palette is None:
            dark = False
        else:
            wc = palette.color(QPalette.ColorRole.Window)
            tc = palette.color(QPalette.ColorRole.WindowText)
            dark = tc.lightness() > wc.lightness()
        icon = _load_plugin_icon(dark)
        if icon is None:
            icon = _load_plugin_icon(not dark)
        return icon
    except Exception:
        return None

class CustomColumnComboBox(QComboBox):
    def __init__(self, parent, custom_columns={}, selected_column='', initial_items=['']):
        QComboBox.__init__(self, parent)
        self.populate_combo(custom_columns, selected_column, initial_items)

    def populate_combo(self, custom_columns, selected_column, initial_items=['']):
        self.clear()
        self.column_names = list(initial_items)
        if len(initial_items) > 0:
            self.addItems(initial_items)
        selected_idx = 0
        for idx, value in enumerate(initial_items):
            if value == selected_column:
                selected_idx = idx

        for key in sorted(custom_columns.keys()):
            self.column_names.append(key)
            self.addItem(_('%s (%s)') % (key, custom_columns[key]['name']))
            if key == selected_column:
                selected_idx = len(self.column_names) - 1
        self.setCurrentIndex(selected_idx)

    def get_selected_column(self):
        return self.column_names[self.currentIndex()]


from calibre.ai import AICapabilities
from calibre.ai.prefs import pref_for_provider
from calibre.ai.utils import configure as configure_provider, plugin_for_name
try:
    from calibre.constants import __version__ as CALIBRE_VERSION
except Exception:
    CALIBRE_VERSION = '?'
try:
    from calibre.utils.localization import get_language
except Exception:
    def get_language(lang, gettext_func=None):
        return str(lang or '')
from calibre.customize.ui import available_ai_provider_plugins
from calibre.gui2 import choose_save_file, error_dialog, info_dialog, question_dialog
from calibre.gui2 import open_url
from calibre.gui2.widgets2 import Dialog
from calibre.library.comments import comments_to_html
from calibre.db.constants import DATA_DIR_NAME
from calibre.ebooks.metadata import MetaInformation

from calibre_plugins.chapter_summarizer import ChapterSummarizerPlugin
from calibre_plugins.chapter_summarizer.book_parser import ParsedDocument, parse_document
from calibre_plugins.chapter_summarizer.cache import ChapterSummaryCache, RunHistoryDB, build_cache_profile_key, cache_location_details, default_cache_path, relocate_cache_storage, source_fingerprint
from calibre_plugins.chapter_summarizer.config import pref, pref_bool, set_pref
from calibre_plugins.chapter_summarizer.summarizer import ChapterSummary, PartialSummaryError, RunStats, SummaryCancelledError, SummaryOptions, SummaryResult, default_highlight_label, flash_summarize_document, infer_language_from_text, render_language_line, render_summary, resolve_provider as resolve_summary_provider, summarize_document
from calibre_plugins.chapter_summarizer.zero_config_demo import ZERO_CONFIG_DEMO_PROVIDER_A, ZERO_CONFIG_DEMO_PROVIDER_B, is_zero_config_demo_provider, planned_model_for_provider

load_translations()


def _cs_debug_enabled() -> bool:
    try:
        return bool(os.environ.get('CHAPTER_SUMMARIZER_DEBUG_ENABLED'))
    except Exception:
        return False


def _cs_debug_log(message: str) -> None:
    if not _cs_debug_enabled():
        return
    try:
        print(f'[Chapter Summarizer][CacheSwitch] {message}')
    except Exception:
        pass


def _cs_perf_log(message: str) -> None:
    if not _cs_debug_enabled():
        return
    try:
        print(f'[Chapter Summarizer][Perf] {message}')
    except Exception:
        pass


def _elapsed_ms(started: float) -> float:
    try:
        return max(0.0, (time.monotonic() - float(started)) * 1000.0)
    except Exception:
        return 0.0

# Apply LM Studio config widget patch to fix model status text clipping
try:
    from calibre_plugins.chapter_summarizer.lm_studio_patch import patch_lm_studio_config_widget
    patch_lm_studio_config_widget()
except Exception:
    pass


class CtrlWheelZoomFilter(QObject):

    def __init__(self, parent=None, min_pt: float = 7.0, max_pt: float = 18.0, step: float = 1.0):
        super().__init__(parent)
        self.min_pt = float(min_pt)
        self.max_pt = float(max_pt)
        self.step = float(step)

    def eventFilter(self, obj, event):
        try:
            if event.type() != QEvent.Type.Wheel:
                return False
            mods = event.modifiers() if hasattr(event, 'modifiers') else QApplication.keyboardModifiers()
            if not (mods & Qt.KeyboardModifier.ControlModifier):
                return False
            delta = 0
            try:
                delta = int(event.angleDelta().y())
            except Exception:
                delta = 0
            if delta == 0:
                return True
            direction = 1.0 if delta > 0 else -1.0
            f = obj.font()
            current = float(f.pointSizeF() if f.pointSizeF() > 0 else f.pointSize() if f.pointSize() > 0 else 9.0)
            new_pt = max(self.min_pt, min(self.max_pt, current + (direction * self.step)))
            f.setPointSizeF(new_pt)
            obj.setFont(f)
            try:
                event.accept()
            except Exception:
                pass
            return True
        except Exception:
            return False


def install_ctrl_wheel_zoom(widget, min_pt: float = 7.0, max_pt: float = 18.0, step: float = 1.0):
    try:
        flt = CtrlWheelZoomFilter(widget, min_pt=min_pt, max_pt=max_pt, step=step)
        widget.installEventFilter(flt)
        # Keep a reference so Qt doesn't GC the filter.
        setattr(widget, '_chapter_summarizer_ctrl_wheel_zoom', flt)
    except Exception:
        pass


def _safe_cache_close(cache_obj):
    """Close a ChapterSummaryCache instance safely.

    calibre can keep old plugin modules in memory across reloads, so we must
    tolerate older ChapterSummaryCache implementations that may not have a
    close() helper.
    """
    if cache_obj is None:
        return
    try:
        close_fn = getattr(cache_obj, 'close', None)
        if callable(close_fn):
            close_fn()
            return
    except Exception:
        pass
    for attr in ('cursor', 'connection'):
        try:
            part = getattr(cache_obj, attr, None)
            if part is None:
                continue
            closer = getattr(part, 'close', None)
            if callable(closer):
                closer()
        except Exception:
            pass


def _cache_has_method(cache_obj, name: str) -> bool:
    try:
        return callable(getattr(cache_obj, name, None))
    except Exception:
        return False


def _safe_cache_get_info(cache_obj, key: str, default: str = '') -> str:
    try:
        fn = getattr(cache_obj, 'get_info', None)
        if callable(fn):
            return str(fn(key, default))
    except Exception:
        pass
    file_path = str(getattr(cache_obj, 'file_path', '') or '').strip()
    if file_path:
        try:
            return str(_sqlite_cache_info_map(file_path).get(str(key), default))
        except Exception:
            pass
    return str(default)


def _safe_cache_set_info(cache_obj, key: str, value: str) -> bool:
    try:
        fn = getattr(cache_obj, 'set_info', None)
        if callable(fn):
            fn(key, value)
            return True
    except Exception:
        pass
    return False


def _safe_cache_all_entries(cache_obj) -> dict[str, dict[str, object]]:
    try:
        fn = getattr(cache_obj, 'all_entries', None)
        if callable(fn):
            return fn()
    except Exception:
        pass
    file_path = str(getattr(cache_obj, 'file_path', '') or '').strip()
    if file_path:
        return _sqlite_cache_all_entries(file_path)
    return {}


def _safe_cache_upsert_chapter(cache_obj, data: dict[str, object]) -> bool:
    try:
        fn = getattr(cache_obj, 'upsert_chapter', None)
        if callable(fn):
            fn(data)
            return True
    except Exception:
        pass
    return False


def _safe_cache_db_paths() -> list[str]:
    paths: list[str] = []
    try:
        cache_dir = default_cache_path()
    except Exception:
        return paths
    try:
        for name in os.listdir(cache_dir):
            if not str(name or '').lower().endswith('.db'):
                continue
            if str(name).lower() == 'chapter_summarizer_history.db':
                continue
            paths.append(os.path.join(cache_dir, name))
    except Exception:
        return []
    return sorted(paths)


def _canonical_library_path(path: str) -> str:
    path = (path or '').strip()
    if not path:
        return ''
    try:
        path = os.path.abspath(path)
    except Exception:
        pass
    return path.replace('\\', '/').rstrip('/')


def _same_library_path(a: str, b: str) -> bool:
    aa = _canonical_library_path(a)
    bb = _canonical_library_path(b)
    if not aa or not bb:
        return aa == bb
    return os.path.normcase(aa) == os.path.normcase(bb)


def _derive_library_path_from_source_path(source_path: str) -> str:
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


def _resolve_cached_book_id(source_path: str = '', source_title: str = '', source_book_id_raw: str = '', db=None) -> int | None:
    """Resolve a source book id in the current library from cached metadata.

    Cache entries may contain an id from a different library. Prefer the stored
    source book id first, then the stored source path, so the book is selected
    in the intended DB without guessing by title.
    """
    try:
        db = db or getattr(getattr(getattr(__import__('sys').modules.get('calibre_plugins.chapter_summarizer.main', None), 'ChapterSummarizerDialog', None), 'gui', None), 'current_db', None)
        db = getattr(db, 'new_api', db) if db is not None else None
    except Exception:
        db = None
    if db is None:
        _cs_debug_log('_resolve_cached_book_id: no database available')
        return None

    db_library_path = ''
    try:
        db_library_path = _canonical_library_path(getattr(db, 'library_path', '') or '')
    except Exception:
        db_library_path = ''
    _cs_debug_log(
        '_resolve_cached_book_id: start '
        f'library={db_library_path!r} raw_id={str(source_book_id_raw or "").strip()!r} '
        f'source_path={str(source_path or "").strip()!r} source_title={str(source_title or "").strip()!r}'
    )

    book_id = None
    try:
        if source_book_id_raw:
            candidate = int(str(source_book_id_raw).strip())
            has_id = getattr(db, 'has_id', None)
            if callable(has_id):
                if has_id(candidate):
                    book_id = candidate
                    _cs_debug_log(f'_resolve_cached_book_id: matched stored source_book_id={candidate!r}')
                else:
                    _cs_debug_log(f'_resolve_cached_book_id: stored source_book_id={candidate!r} not present in current DB')
            else:
                book_id = candidate
                _cs_debug_log(f'_resolve_cached_book_id: using stored source_book_id without has_id check={candidate!r}')
    except Exception:
        _cs_debug_log(f'_resolve_cached_book_id: invalid stored source_book_id={str(source_book_id_raw or "").strip()!r}')
        book_id = None

    def format_path_for_book(bid, fmt):
        try:
            return db.format_abspath(bid, fmt, index_is_id=True)
        except Exception:
            pass
        try:
            return db.format_abspath(bid, fmt)
        except Exception:
            pass
        try:
            return db.format(bid, fmt, index_is_id=True, as_path=True, preserve_filename=True)
        except Exception:
            pass
        try:
            return db.format(bid, fmt, as_path=True, preserve_filename=True)
        except Exception:
            pass
        return ''

    if book_id is None and source_path:
        source_norm = os.path.normcase(os.path.abspath(source_path))
        source_base = os.path.normcase(os.path.basename(source_path))
        basename_match = None
        try:
            all_ids = list(getattr(db, 'all_book_ids', lambda: [])())
        except Exception:
            all_ids = []
        for bid in all_ids:
            try:
                fmt_string = ''
                try:
                    fmt_string = db.formats(bid, index_is_id=True) or ''
                except TypeError:
                    try:
                        fmt_string = db.formats(bid) or ''
                    except Exception:
                        fmt_string = ''
                except Exception:
                    fmt_string = ''
                for fmt in [x.strip().upper() for x in fmt_string.split(',') if x.strip()]:
                    fpath = format_path_for_book(bid, fmt)
                    if not fpath:
                        continue
                    cand_norm = os.path.normcase(os.path.abspath(fpath))
                    if cand_norm == source_norm:
                        _cs_debug_log(f'_resolve_cached_book_id: exact path match book_id={int(bid)!r} format={fmt!r}')
                        return int(bid)
                    if basename_match is None and os.path.normcase(os.path.basename(fpath)) == source_base:
                        basename_match = int(bid)
            except Exception:
                continue
        book_id = basename_match
        if book_id is not None:
            _cs_debug_log(f'_resolve_cached_book_id: basename fallback match book_id={book_id!r}')
        else:
            _cs_debug_log('_resolve_cached_book_id: no exact path match and title fallback disabled')
    if book_id is None:
        _cs_debug_log('_resolve_cached_book_id: returning None')
    else:
        _cs_debug_log(f'_resolve_cached_book_id: returning book_id={int(book_id)!r}')
    return None if book_id is None else int(book_id)


def _sqlite_cache_info_map(file_path: str) -> dict[str, str]:
    conn = None
    try:
        conn = sqlite3.connect(file_path)
        rows = conn.execute('SELECT key, value FROM info').fetchall()
        return {str(k): '' if v is None else str(v) for k, v in rows}
    except Exception:
        return {}
    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass


def _sqlite_cache_all_entries(file_path: str) -> dict[str, dict[str, object]]:
    conn = None
    try:
        conn = sqlite3.connect(file_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute('SELECT * FROM chapter_summaries ORDER BY title COLLATE NOCASE').fetchall()
        ans: dict[str, dict[str, object]] = {}
        for row in rows:
            item = dict(row)
            chapter_id = str(item.get('chapter_id') or '')
            if chapter_id:
                ans[chapter_id] = item
        return ans
    except Exception:
        return {}
    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass


def _sqlite_cache_metadata(file_path: str) -> dict[str, object]:
    info = _sqlite_cache_info_map(file_path)
    entries = _sqlite_cache_all_entries(file_path)
    try:
        size_bytes = int(os.path.getsize(file_path))
    except Exception:
        size_bytes = 0
    return {
        'profile_key': os.path.splitext(os.path.basename(file_path))[0],
        'file_path': file_path,
        'filename': os.path.basename(file_path),
        'title': str(info.get('source_title') or _('[Unknown]')),
        'provider': str(info.get('provider_name') or ''),
        'model': str(info.get('requested_model') or ''),
        'updated_at': str(info.get('updated_at') or ''),
        'source_path': str(info.get('source_path') or ''),
        'source_library_path': str(info.get('source_library_path') or ''),
        'source_book_id': str(info.get('source_book_id') or ''),
        'source_format': str(info.get('source_format') or ''),
        'chapter_count': int(len(entries)),
        'size_bytes': int(size_bytes),
    }


def _safe_cache_get_list() -> list[dict]:
    try:
        fn = getattr(ChapterSummaryCache, 'get_list', None)
        if callable(fn):
            return list(fn() or [])
    except Exception:
        pass
    result: list[dict] = []
    for file_path in _safe_cache_db_paths():
        cache = None
        try:
            profile_key = os.path.splitext(os.path.basename(file_path))[0]
            cache = ChapterSummaryCache(profile_key)
            if _cache_has_method(cache, 'get_info') and _cache_has_method(cache, 'all_entries'):
                title = _safe_cache_get_info(cache, 'source_title', _('[Unknown]')) or _('[Unknown]')
                provider = _safe_cache_get_info(cache, 'provider_name', '')
                model = _safe_cache_get_info(cache, 'requested_model', '')
                updated_at = _safe_cache_get_info(cache, 'updated_at', '')
                source_path = _safe_cache_get_info(cache, 'source_path', '')
                source_library_path = _safe_cache_get_info(cache, 'source_library_path', '')
                source_book_id = _safe_cache_get_info(cache, 'source_book_id', '')
                source_format = _safe_cache_get_info(cache, 'source_format', '')
                chapter_count = len(_safe_cache_all_entries(cache))
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
                    'size_bytes': int(os.path.getsize(file_path)),
                })
            else:
                result.append(_sqlite_cache_metadata(file_path))
        except Exception:
            result.append(_sqlite_cache_metadata(file_path))
        finally:
            _safe_cache_close(cache)
    result.sort(key=lambda d: d.get('updated_at') or '', reverse=True)
    return result


def _safe_cache_total_size_bytes() -> int:
    try:
        fn = getattr(ChapterSummaryCache, 'total_size_bytes', None)
        if callable(fn):
            return int(fn() or 0)
    except Exception:
        pass
    total = 0
    for file_path in _safe_cache_db_paths():
        try:
            total += int(os.path.getsize(file_path))
        except Exception:
            pass
    return total


def _safe_cache_delete_profile(profile_key: str) -> bool:
    try:
        fn = getattr(ChapterSummaryCache, 'delete', None)
        if callable(fn):
            return bool(fn(profile_key))
    except Exception:
        pass
    file_path = os.path.join(default_cache_path(), str(profile_key or '').strip() + '.db')
    try:
        if os.path.basename(file_path).lower() == 'chapter_summarizer_history.db':
            return False
    except Exception:
        pass
    try:
        if os.path.exists(file_path):
            os.remove(file_path)
            return True
    except Exception:
        pass
    return False


def _safe_cache_clear_all() -> int:
    try:
        fn = getattr(ChapterSummaryCache, 'clear_all', None)
        if callable(fn):
            return int(fn() or 0)
    except Exception:
        pass
    count = 0
    for file_path in _safe_cache_db_paths():
        try:
            os.remove(file_path)
            count += 1
        except Exception:
            pass
    return count


def _safe_cache_find_by_source(source_path: str) -> list[dict]:
    try:
        fn = getattr(ChapterSummaryCache, 'find_by_source', None)
        if callable(fn):
            return list(fn(source_path) or [])
    except Exception:
        pass
    if not source_path:
        return []
    norm = os.path.normcase(os.path.abspath(source_path))
    matches = []
    for entry in _safe_cache_get_list():
        stored = str(entry.get('source_path') or '')
        if not stored:
            continue
        try:
            if os.path.normcase(os.path.abspath(stored)) != norm:
                continue
        except Exception:
            continue
        rendered_text_len = int(entry.get('rendered_text_len', 0) or 0)
        if int(entry.get('chapter_count', 0) or 0) <= 0 and rendered_text_len <= 0:
            continue
        matches.append(entry)
    matches.sort(key=lambda d: (1 if int(d.get('chapter_count', 0) or 0) > 0 else 0, int(d.get('chapter_count', 0) or 0), 1 if int(d.get('rendered_text_len', 0) or 0) > 0 else 0, d.get('updated_at') or ''), reverse=True)
    return matches


def _history_db_path() -> str:
    try:
        return os.path.join(default_cache_path(), 'chapter_summarizer_history.db')
    except Exception:
        return os.path.join(tempfile.gettempdir(), 'chapter_summarizer_history.db')


def _history_ensure_schema(connection):
    cur = connection.cursor()
    cur.execute(
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
    cur.execute('CREATE INDEX IF NOT EXISTS runs_source_norm_idx ON runs(source_norm)')
    cur.execute('CREATE INDEX IF NOT EXISTS runs_source_book_id_idx ON runs(source_book_id)')
    cur.execute('CREATE INDEX IF NOT EXISTS runs_started_at_idx ON runs(started_at)')
    connection.commit()


def _history_norm(path: str) -> str:
    try:
        return os.path.normcase(os.path.abspath(path or ''))
    except Exception:
        return str(path or '')


def _safe_history_close(db_obj):
    if db_obj is None:
        return
    try:
        close_fn = getattr(db_obj, 'close', None)
        if callable(close_fn):
            close_fn()
            return
    except Exception:
        pass
    for attr in ('cursor', 'connection'):
        try:
            part = getattr(db_obj, attr, None)
            if part is None:
                continue
            closer = getattr(part, 'close', None)
            if callable(closer):
                closer()
        except Exception:
            pass


def _safe_history_list_for_source(*, source_path: str = '', source_book_id: str = '', limit: int = 250) -> list[dict]:
    try:
        db = RunHistoryDB()
        try:
            fn = getattr(db, 'list_for_source', None)
            if callable(fn):
                return list(fn(source_path=source_path, source_book_id=source_book_id, limit=limit) or [])
        finally:
            _safe_history_close(db)
    except Exception:
        pass
    rows = []
    conn = None
    try:
        conn = sqlite3.connect(_history_db_path())
        conn.row_factory = sqlite3.Row
        _history_ensure_schema(conn)
        cur = conn.cursor()
        lim = max(1, min(int(limit or 250), 2000))
        if source_book_id:
            rows = cur.execute('SELECT * FROM runs WHERE source_book_id=? ORDER BY started_at DESC LIMIT ?', (str(source_book_id), lim)).fetchall()
        elif source_path:
            rows = cur.execute('SELECT * FROM runs WHERE source_norm=? ORDER BY started_at DESC LIMIT ?', (_history_norm(source_path), lim)).fetchall()
    except Exception:
        rows = []
    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass
    return [dict(r) for r in (rows or [])]


def _safe_history_clear_for_source(*, source_path: str = '', source_book_id: str = '') -> int:
    try:
        db = RunHistoryDB()
        try:
            fn = getattr(db, 'clear_for_source', None)
            if callable(fn):
                return int(fn(source_path=source_path, source_book_id=source_book_id) or 0)
        finally:
            _safe_history_close(db)
    except Exception:
        pass
    conn = None
    try:
        conn = sqlite3.connect(_history_db_path())
        _history_ensure_schema(conn)
        cur = conn.cursor()
        if source_book_id:
            ans = cur.execute('DELETE FROM runs WHERE source_book_id=?', (str(source_book_id),))
        elif source_path:
            ans = cur.execute('DELETE FROM runs WHERE source_norm=?', (_history_norm(source_path),))
        else:
            return 0
        conn.commit()
        return int(getattr(ans, 'rowcount', 0) or 0)
    except Exception:
        return 0
    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass


def _safe_history_record_start(
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
) -> int:
    try:
        db = RunHistoryDB()
        try:
            fn = getattr(db, 'record_start', None)
            if callable(fn):
                return int(fn(
                    source_path=source_path,
                    source_title=source_title,
                    source_book_id=source_book_id,
                    source_format=source_format,
                    provider_name=provider_name,
                    requested_model=requested_model,
                    effective_model_label=effective_model_label,
                    target_language=target_language,
                    selected_nodes=selected_nodes,
                    total_nodes=total_nodes,
                ) or 0)
        finally:
            _safe_history_close(db)
    except Exception:
        pass
    conn = None
    try:
        conn = sqlite3.connect(_history_db_path())
        _history_ensure_schema(conn)
        cur = conn.cursor()
        started_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        cur.execute(
            'INSERT INTO runs('
            'source_path, source_norm, source_title, source_book_id, source_format, '
            'provider_name, requested_model, effective_model_label, model_used, target_language, '
            'selected_nodes, total_nodes, summarized_nodes, status, error_message, '
            'started_at, finished_at, duration_seconds'
            ') VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (
                str(source_path or ''), _history_norm(source_path), str(source_title or ''), str(source_book_id or ''), str(source_format or ''),
                str(provider_name or ''), str(requested_model or ''), str(effective_model_label or ''), '', str(target_language or ''),
                None if selected_nodes is None else int(selected_nodes),
                None if total_nodes is None else int(total_nodes),
                None, 'running', '', str(started_at), '', None,
            ),
        )
        conn.commit()
        row = cur.execute('SELECT last_insert_rowid()').fetchone()
        return int(row[0]) if row else 0
    except Exception:
        return 0
    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass


def _safe_history_record_finish(
    run_id: int,
    *,
    status: str,
    summarized_nodes: int | None = None,
    error_message: str = '',
    model_used: str = '',
    duration_seconds: float | None = None,
) -> None:
    try:
        db = RunHistoryDB()
        try:
            fn = getattr(db, 'record_finish', None)
            if callable(fn):
                fn(
                    run_id,
                    status=status,
                    summarized_nodes=summarized_nodes,
                    error_message=error_message,
                    model_used=model_used,
                    duration_seconds=duration_seconds,
                )
                return
        finally:
            _safe_history_close(db)
    except Exception:
        pass
    conn = None
    try:
        conn = sqlite3.connect(_history_db_path())
        _history_ensure_schema(conn)
        conn.execute(
            'UPDATE runs SET status=?, summarized_nodes=?, error_message=?, model_used=?, finished_at=?, duration_seconds=? WHERE id=?',
            (
                str(status or ''),
                None if summarized_nodes is None else int(summarized_nodes),
                str(error_message or ''),
                str(model_used or ''),
                datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                None if duration_seconds is None else float(duration_seconds),
                int(run_id),
            ),
        )
        conn.commit()
    except Exception:
        pass
    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass


def plugin_version_text() -> str:
    try:
        return '.'.join(str(x) for x in ChapterSummarizerPlugin.version)
    except Exception:
        return '?.?.?'


def calibre_version_text() -> str:
    try:
        return str(CALIBRE_VERSION)
    except Exception:
        return '?'


TARGET_LANGUAGE_CODES = (
    'en', 'it', 'fr', 'de', 'es', 'pt', 'pt_BR', 'ru', 'uk', 'sr', 'hr', 'bg',
    'ro', 'pl', 'nl', 'cs', 'sk', 'sl', 'hu', 'sv', 'da', 'fi', 'el', 'tr',
    'ar', 'he', 'fa', 'vi', 'id', 'ms', 'ja', 'ko', 'zh_CN', 'zh_TW'
)


def normalize_target_language(value: str) -> str:
    raw = str(value or '').strip()
    if not raw:
        return 'same-as-source'
    if raw == 'english':
        return 'en'
    return raw


def display_language_name(code: str) -> str:
    code = normalize_target_language(code)
    if code == 'same-as-source':
        return _('Same as source')
    try:
        return str(get_language(code) or code)
    except Exception:
        return code


def summary_title_position() -> str:
    value = str(pref('summary_title_position', 'after') or 'after').strip().lower()
    return value if value in {'before', 'after'} else 'after'


def format_summary_book_title(source_title: str) -> str:
    base = str(source_title or '').strip() or _('Summary')
    label = _('Summary')
    if summary_title_position() == 'before':
        return _('%s — %s') % (label, base)
    return _('%s — %s') % (base, label)


def shorten_cover_title(text: str, limit: int = 92) -> str:
    raw = re.sub(r'\s+', ' ', str(text or '').strip())
    if len(raw) <= limit:
        return raw
    clipped = raw[:limit].rsplit(' ', 1)[0].strip()
    return (clipped or raw[:limit].strip()) + '…'


class ChapterSummarizerConfigDialog(Dialog):
    must_restart_before_config = False

    def __init__(self, gui, icon=None, embed=False):
        # gui may be the CS dialog window rather than calibre's main window;
        # resolve the real calibre gui for internal use while using the passed
        # widget as the Qt parent so the dialog appears centred over it.
        from calibre.gui2.ui import get_gui
        self.gui = get_gui() or gui
        self._embed_mode = embed
        self._palette_signal_connected = False
        self._original_cache_path = ''
        self._pending_cache_path = ''
        self._config_cache_path = ''
        self._suggested_cache_path = ''
        Dialog.__init__(self, _('Configure Chapter Summarizer'), 'chapter_summarizer_config_dialog', parent=gui)
        try:
            if icon is not None:
                self.setWindowIcon(icon)
        except Exception:
            pass

    def setup_ui(self):
        self.resize(640, 430)
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        box = QGroupBox(_('General'))
        form = QFormLayout(box)
        form.setVerticalSpacing(6)
        try:
            form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        except Exception:
            try:
                form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
            except Exception:
                pass

        self.data_files_format_combo = QComboBox(self)
        self.data_files_format_combo.addItem(_('Markdown (.md)'), 'md')
        self.data_files_format_combo.addItem(_('HTML (.html)'), 'html')
        self.data_files_format_combo.addItem(_('EPUB (.epub)'), 'epub')
        current = str(pref('data_files_format', 'md') or 'md').strip().lower()
        idx = self.data_files_format_combo.findData(current)
        self.data_files_format_combo.setCurrentIndex(max(0, idx))

        self.add_to_library_format_combo = QComboBox(self)
        self.add_to_library_format_combo.addItem(_('EPUB (.epub)'), 'epub')
        self.add_to_library_format_combo.addItem(_('HTML (.html)'), 'html')
        self.add_to_library_format_combo.addItem(_('Text (.txt)'), 'txt')
        library_current = str(pref('add_to_library_format', 'epub') or 'epub').strip().lower()
        library_idx = self.add_to_library_format_combo.findData(library_current)
        self.add_to_library_format_combo.setCurrentIndex(max(0, library_idx))
        try:
            self.data_files_format_combo.setStyleSheet(ccr_combo_stylesheet())
        except Exception:
            pass

        self.verbose_ai_log_check = QCheckBox(_('Verbose AI log'))
        self.verbose_ai_log_check.setChecked(pref_bool('verbose_ai_log', False))
        self.verbose_ai_log_check.setToolTip(_('Log provider/model calls and prompt previews to the AI log tab.'))

        self.append_to_comments_check = QCheckBox(_('Auto-append to comments'))
        self.append_to_comments_check.setChecked(pref_bool('append_to_comments', False))
        self.append_to_comments_check.setToolTip(_('Automatically append the generated summary to the source book comments when summarization completes.\n\nNote: this increases metadata.db size. Use with care.'))

        # Custom append column selector
        custom_columns = self._get_long_text_columns()
        self.custom_append_column_combo = CustomColumnComboBox(self, custom_columns, str(pref('custom_append_column', '') or ''), initial_items=[''])
        self.custom_append_column_combo.setToolTip(_('Select a custom column to append summaries instead of the built-in comments field.\nThe column must be of type "long text" or "comments".\nIf left empty, summaries will be appended to the built-in comments field.'))
        self.custom_append_column_combo.setMinimumWidth(200)

        # Helper button to create custom column
        self.create_custom_column_button = QPushButton(self)
        self.create_custom_column_button.setIcon(QIcon.ic('plus.png'))
        self.create_custom_column_button.setToolTip(_('Open calibre custom columns configuration to create a new column'))
        self.create_custom_column_button.setMaximumWidth(40)
        self.create_custom_column_button.clicked.connect(self._create_summary_column)

        self.summary_custom_tags_edit = QLineEdit(self)
        self.summary_custom_tags_edit.setText(str(pref('summary_custom_tags', '') or ''))
        self.summary_custom_tags_edit.setPlaceholderText(_('Comma-separated tags (optional)'))
        self.summary_custom_tags_edit.setToolTip(_('Extra tags to apply when adding the summary as a new book. Separate tags with commas.'))

        self.summary_title_position_combo = QComboBox(self)
        self.summary_title_position_combo.addItem(_('After title — Book Title — Summary'), 'after')
        self.summary_title_position_combo.addItem(_('Before title — Summary — Book Title'), 'before')
        position_idx = self.summary_title_position_combo.findData(summary_title_position())
        self.summary_title_position_combo.setCurrentIndex(max(0, position_idx))
        self.summary_title_position_combo.setToolTip(_('Choose whether the summary label appears before or after the source title when adding summary books to calibre.'))

        self.summary_copy_source_cover_check = QCheckBox(_('Use source book cover for summary book'))
        self.summary_copy_source_cover_check.setChecked(pref_bool('summary_copy_source_cover', True))
        self.summary_copy_source_cover_check.setToolTip(_('When adding the summary as a new book, copy the selected source book cover to the new summary entry.'))

        self.chapter_cache_check = QCheckBox(_('Enable chapter summary cache'))
        self.chapter_cache_check.setChecked(pref_bool('chapter_cache_enabled', True))
        self.chapter_cache_check.setToolTip(_('Store per-chapter summary results and metadata so Select chapters can show cached summaries and tooltips.'))

        cache_details = cache_location_details()
        self._original_cache_path = str(cache_details.get('path') or '')
        self._config_cache_path = str(cache_details.get('config_path') or '')
        self._suggested_cache_path = str(cache_details.get('suggested_path') or '')
        self._pending_cache_path = self._original_cache_path or self._config_cache_path

        self.cache_location_edit = QLineEdit(self)
        self.cache_location_edit.setReadOnly(True)
        self.cache_location_edit.setToolTip(_('Chapter Summarizer stores its cache and run-history database files in this folder.'))
        try:
            self.cache_location_edit.setMinimumWidth(420)
            self.cache_location_edit.setCursorPosition(0)
        except Exception:
            pass

        self.choose_cache_folder_button = QPushButton(_('Choose custom folder…'))
        self.choose_cache_folder_button.setToolTip(_('Choose a custom folder for Chapter Summarizer cache database files.\n\nThe existing cache DBs are moved when you press OK.'))
        self.choose_cache_folder_button.clicked.connect(self.choose_cache_folder)

        self.use_config_cache_folder_button = QPushButton(_('Use config folder'))
        self.use_config_cache_folder_button.setToolTip(_('Use the default Chapter Summarizer cache folder inside calibre configuration.\n\nThe existing cache DBs are moved when you press OK.'))
        self.use_config_cache_folder_button.clicked.connect(self.use_config_cache_folder)

        cache_location_row = QWidget(self)
        cache_location_layout = QHBoxLayout(cache_location_row)
        cache_location_layout.setContentsMargins(0, 0, 0, 0)
        cache_location_layout.setSpacing(6)
        cache_location_layout.addWidget(self.cache_location_edit, 1)
        cache_location_layout.addWidget(self.choose_cache_folder_button)
        cache_location_layout.addWidget(self.use_config_cache_folder_button)

        self.cache_location_note = QLabel('')
        self.cache_location_note.setWordWrap(True)
        self.cache_location_note.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.cache_location_note.setStyleSheet('color: palette(mid); font-size: 8pt;')

        self.multi_file_check = QCheckBox(_('Enable multi-file batch summarization'))
        self.multi_file_check.setChecked(pref_bool('multi_file_enabled', False))
        self.multi_file_check.setToolTip(_('When enabled, all selected books in calibre are summarized in sequence. Progress tracks across all files.'))

        self.max_chunk_words_spin = QSpinBox(self)
        self.max_chunk_words_spin.setRange(500, 8000)
        self.max_chunk_words_spin.setSingleStep(100)
        self.max_chunk_words_spin.setValue(int(pref('max_chunk_words', 1400)))
        self.max_chunk_words_spin.setSuffix(' ' + _('words'))
        self.max_chunk_words_spin.setToolTip(_('Maximum words per API request. Lower values use more requests but reduce failures. Most models support 1200-1600 safely.'))

        self.request_interval_free_spin = QSpinBox(self)
        self.request_interval_free_spin.setRange(0, 300)
        self.request_interval_free_spin.setSuffix(' s')
        self.request_interval_free_spin.setValue(int(pref('request_interval_free', 1)))
        self.request_interval_free_spin.setToolTip(_('Minimum seconds to wait between AI requests when the model name contains ":free". Raise this if you hit rate limits. 0 disables pacing.'))

        self.request_interval_default_spin = QSpinBox(self)
        self.request_interval_default_spin.setRange(0, 120)
        self.request_interval_default_spin.setSuffix(' s')
        self.request_interval_default_spin.setValue(int(pref('request_interval_default', 0)))
        self.request_interval_default_spin.setToolTip(_('Minimum seconds to wait between AI requests for non-free models. 0 disables pacing.'))

        self.anthropic_openai_compatible_check = QCheckBox(_('Enable Anthropic latest-model compatibility for OpenAI compatible provider'))
        self.anthropic_openai_compatible_check.setChecked(pref_bool('anthropic_openai_compatible_override', False))
        self.anthropic_openai_compatible_check.setToolTip(_(
            'When enabled, Chapter Summarizer omits the temperature field for its own requests through calibre\'s OpenAI compatible provider.\n\n'
            'Use this only when that provider is configured for Anthropic Claude models that reject temperature, such as Sonnet 5.\n\n'
            'This does not change calibre\'s native provider settings or behaviour outside this plugin.'
        ))

        pace_row_widget = QWidget(self)
        pace_row_layout = QHBoxLayout(pace_row_widget)
        pace_row_layout.setContentsMargins(0, 0, 0, 0)
        pace_row_layout.setSpacing(6)
        pace_row_layout.addWidget(QLabel(_('Free:'), self))
        pace_row_layout.addWidget(self.request_interval_free_spin)
        pace_row_layout.addWidget(QLabel(_('Other:'), self))
        pace_row_layout.addWidget(self.request_interval_default_spin)
        pace_row_layout.addStretch(1)

        self.epub_style_combo = QComboBox(self)
        for label, style_name in (
            (_('Auto'), 'auto'),
            (_('Light'), 'light'),
            (_('Dark'), 'dark'),
            (_('Sepia'), 'sepia'),
            (_('Custom'), 'custom'),
        ):
            self.epub_style_combo.addItem(label, style_name)
        epub_style = normalize_summary_output_style(pref('epub_output_style', 'auto'))
        epub_idx = self.epub_style_combo.findData(epub_style)
        self.epub_style_combo.setCurrentIndex(max(0, epub_idx))
        self._epub_custom_css = str(pref('epub_custom_css', '') or '').strip()
        self.epub_style_combo.setToolTip(_('Style used for EPUB export and other book-style summary outputs.\n\nNote: this does NOT affect the live preview in the main dialog — that uses the HTML/browser style buttons.'))

        self.extra_prompts_button = QPushButton(_('Extra prompt additions…'))
        self.extra_prompts_button.clicked.connect(self.edit_extra_prompts)
        self.extra_prompts_button.setToolTip(_(
            'Optional extra guidance appended to the AI user prompt.\n\n'
            'Use this for house style, audience, or constraints.\n'
            'Keep it short — it is applied on every request for the chosen mode/type.'
        ))
        self.epub_style_edit_button = QPushButton(_('Edit EPUB custom…'))
        self.epub_style_edit_button.clicked.connect(self.edit_epub_custom_style)
        self.epub_style_edit_button.setToolTip(_('Edit the custom CSS preset used when the EPUB/book-output style is set to Custom.'))

        epub_style_row = QWidget(self)
        epub_style_layout = QHBoxLayout(epub_style_row)
        epub_style_layout.setContentsMargins(0, 0, 0, 0)
        epub_style_layout.setSpacing(6)
        epub_style_layout.addWidget(self.epub_style_combo)
        epub_style_layout.addWidget(self.epub_style_edit_button)
        epub_style_layout.addStretch(1)

        self.model_override_edit = QLineEdit(self)
        self.model_override_edit.setText(str(pref('model_name', '') or ''))
        self.model_override_edit.setPlaceholderText(_('Leave blank to use the provider-configured text model'))
        self._model_override_default_tooltip = _('Optional manual model override.\n\nLeave blank: use the model configured in the chosen calibre AI provider.\nFill this in: force a specific model ID/name for this plugin.\n\nMost useful with providers that expose multiple model IDs, such as OpenRouter, or other calibre AI providers that accept an explicit model name.\nIf a provider/plugin does not honor manual model names, its normal configured/default model is used instead.')
        self._model_override_zero_config_tooltip = _('Zero-config demo providers use their own curated rotating model pool.\n\nManual model override is disabled for these providers.')
        self.model_override_edit.setToolTip(self._model_override_default_tooltip)
        try:
            self.model_override_edit.setMinimumWidth(420)
            self.model_override_edit.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        except Exception:
            pass

        try:
            self.data_files_format_combo.setStyleSheet(ccr_combo_stylesheet())
            self.add_to_library_format_combo.setStyleSheet(ccr_combo_stylesheet())
            self.model_override_edit.setStyleSheet(ccr_combo_stylesheet())
            self.summary_custom_tags_edit.setStyleSheet(ccr_combo_stylesheet())
            self.summary_title_position_combo.setStyleSheet(ccr_combo_stylesheet())
            self.epub_style_combo.setStyleSheet(ccr_combo_stylesheet())
            self.cache_location_edit.setStyleSheet(ccr_combo_stylesheet())
        except Exception:
            pass

        form.addRow(_('Add to data files format:'), self.data_files_format_combo)
        form.addRow(_('Add as new book format:'), self.add_to_library_format_combo)
        form.addRow(_('Summary title placement:'), self.summary_title_position_combo)
        form.addRow(_('EPUB / book-output style:'), epub_style_row)
        form.addRow(_('Model override configuration:'), self.model_override_edit)
        form.addRow('', self.anthropic_openai_compatible_check)
        form.addRow('', self.extra_prompts_button)
        form.addRow(_('Summary book tags:'), self.summary_custom_tags_edit)
        form.addRow(_('Chunk size:'), self.max_chunk_words_spin)
        form.addRow(_('Request pacing:'), pace_row_widget)
        form.addRow('', self.summary_copy_source_cover_check)
        form.addRow('', self.chapter_cache_check)
        form.addRow(_('Cache storage folder:'), cache_location_row)
        form.addRow('', self.cache_location_note)
        form.addRow('', self.multi_file_check)
        form.addRow('', self.append_to_comments_check)

        # Custom append column row
        custom_column_row = QHBoxLayout()
        custom_column_row.addWidget(self.custom_append_column_combo)
        custom_column_row.addWidget(self.create_custom_column_button)
        custom_column_row.setSpacing(4)
        form.addRow(_('Custom append column:'), custom_column_row)

        # Compact layout for verbose/debug checkboxes
        debug_row = QHBoxLayout()
        debug_row.addWidget(self.verbose_ai_log_check)
        debug_row.setSpacing(20)
        form.addRow(_('Logging:'), debug_row)

        root.addWidget(box)
        root.addStretch(1)

        # When embedded as a calibre plugin configuration widget, calibre
        # provides its own OK/Cancel buttons. Adding another button box causes
        # duplicate OK/Cancel rows.
        if not self._embed_mode:
            bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, parent=self)
            bb.accepted.connect(self.accept)
            bb.rejected.connect(self.reject)
            root.addWidget(bb)

        self._apply_runtime_theme()
        app = QApplication.instance()
        if app is not None and hasattr(app, 'palette_changed') and not self._palette_signal_connected:
            try:
                app.palette_changed.connect(self._apply_runtime_theme)
                self._palette_signal_connected = True
            except Exception:
                pass

        self._refresh_cache_location_ui()

    def _get_long_text_columns(self):
        """Get custom columns that support long text (comments, long text, etc.)"""
        long_text_columns = {}
        try:
            custom_columns = self.gui.library_view.model().custom_columns
            for key, column in custom_columns.items():
                datatype = column.get('datatype', '')
                if datatype in ('comments', 'longtext'):
                    long_text_columns[key] = column
        except Exception:
            pass
        return long_text_columns

    def _create_summary_column(self):
        """Create a new custom column for summaries using calibre's column creation dialog"""
        try:
            from calibre.gui2.preferences.create_custom_column import CreateNewCustomColumn

            create_dialog = CreateNewCustomColumn(self.gui)

            # Set up the column for long text type
            display_params = {
                'description': _('Chapter summaries'),
            }

            result = create_dialog.create_column(
                '#summary',
                _('Summary'),
                'comments',
                False,
                display=display_params,
                generate_unused_lookup_name=True,
                freeze_lookup_name=False
            )

            if result[0] == CreateNewCustomColumn.Result.COLUMN_ADDED:
                # Refresh the combo box with the new column
                new_column_key = result[1]
                custom_columns = self._get_long_text_columns()
                self.custom_append_column_combo.populate_combo(custom_columns, new_column_key, initial_items=[''])

                # Show info dialog about needing to restart calibre
                from calibre.gui2 import info_dialog
                info_dialog(self, _('Column Created'),
                          _('A new custom column has been created.\n\nPlease restart calibre for the new column to be fully available.'),
                          show_copy_button=False, show=True)
        except Exception as e:
            from calibre.gui2 import error_dialog
            error_dialog(self, _('Error'),
                        _('Failed to create custom column: {}').format(str(e)),
                        show_copy_button=True, show=True)

    def _apply_runtime_theme(self):
        try:
            combo_style = ccr_combo_stylesheet()
            self.data_files_format_combo.setStyleSheet(combo_style)
            self.add_to_library_format_combo.setStyleSheet(combo_style)
            self.model_override_edit.setStyleSheet(combo_style)
            self.summary_custom_tags_edit.setStyleSheet(combo_style)
            self.summary_title_position_combo.setStyleSheet(combo_style)
            self.epub_style_combo.setStyleSheet(combo_style)
            self.cache_location_edit.setStyleSheet(combo_style)
        except Exception:
            pass

    def _normalize_cache_folder(self, path: str) -> str:
        text = str(path or '').strip().strip('"')
        if not text:
            return str(self._config_cache_path or '')
        try:
            text = os.path.expanduser(text)
        except Exception:
            pass
        try:
            return os.path.abspath(text)
        except Exception:
            return text

    def _same_cache_folder(self, a: str, b: str) -> bool:
        aa = self._normalize_cache_folder(a)
        bb = self._normalize_cache_folder(b)
        if not aa or not bb:
            return False
        try:
            return os.path.normcase(os.path.realpath(aa)) == os.path.normcase(os.path.realpath(bb))
        except Exception:
            return os.path.normcase(aa) == os.path.normcase(bb)

    def _refresh_cache_location_ui(self):
        current_path = self._normalize_cache_folder(self._pending_cache_path or self._config_cache_path)
        using_custom = bool(current_path and not self._same_cache_folder(current_path, self._config_cache_path))
        try:
            self.cache_location_edit.setText(current_path)
            self.cache_location_edit.setCursorPosition(0)
        except Exception:
            pass

        lines = [
            _('Currently using a custom cache folder.') if using_custom else _('Currently using the calibre config folder.'),
        ]
        lines.append(_('Changes are applied when you press OK.\nExisting cache DB files are moved automatically.'))
        try:
            self.cache_location_note.setText('\n\n'.join(lines))
        except Exception:
            pass
        try:
            self.use_config_cache_folder_button.setEnabled(not self._same_cache_folder(current_path, self._config_cache_path))
        except Exception:
            pass

    def choose_cache_folder(self):
        start_dir = self._normalize_cache_folder(self._pending_cache_path or self._suggested_cache_path or self._config_cache_path or default_cache_path())
        try:
            chosen = QFileDialog.getExistingDirectory(self, _('Choose Chapter Summarizer cache folder'), start_dir)
        except Exception:
            chosen = ''
        chosen = self._normalize_cache_folder(chosen)
        if not chosen:
            return
        self._pending_cache_path = chosen
        self._refresh_cache_location_ui()

    def use_config_cache_folder(self):
        self._pending_cache_path = str(self._config_cache_path or default_cache_path())
        self._refresh_cache_location_ui()

    def edit_epub_custom_style(self):
        dialog = SummaryStyleDialog(self._epub_custom_css, parent=self)
        try:
            dialog.setWindowIcon(self.windowIcon())
        except Exception:
            pass
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._epub_custom_css = dialog.value
            if (self.epub_style_combo.currentData() or 'auto') == 'custom':
                self.epub_style_combo.setCurrentIndex(max(0, self.epub_style_combo.findData('custom')))

    def accept(self):
        target_cache_path = self._normalize_cache_folder(self._pending_cache_path or self._config_cache_path)
        if target_cache_path and not self._same_cache_folder(target_cache_path, self._original_cache_path):
            worker = getattr(self.gui, '_worker', None)
            if worker is not None:
                try:
                    is_alive = getattr(worker, 'is_alive', None)
                    if callable(is_alive) and is_alive():
                        error_dialog(self, _('Cache storage folder'), _('Stop the current summarization before changing the cache storage folder.'), show=True)
                        return
                except Exception:
                    pass
            try:
                relocate_cache_storage(target_cache_path)
                self._original_cache_path = target_cache_path
                self._pending_cache_path = target_cache_path
            except Exception as e:
                error_dialog(self, _('Cache storage folder'), str(e), show=True)
                return
        try:
            set_pref('data_files_format', str(self.data_files_format_combo.currentData() or 'md'))
            set_pref('add_to_library_format', str(self.add_to_library_format_combo.currentData() or 'epub'))
            set_pref('summary_title_position', str(self.summary_title_position_combo.currentData() or 'after'))
            set_pref('epub_output_style', str(self.epub_style_combo.currentData() or 'auto'))
            set_pref('epub_custom_css', self._epub_custom_css)
            set_pref('model_name', str(self.model_override_edit.text() or '').strip())
            set_pref('anthropic_openai_compatible_override', bool(self.anthropic_openai_compatible_check.isChecked()))
            set_pref('verbose_ai_log', bool(self.verbose_ai_log_check.isChecked()))
            set_pref('append_to_comments', bool(self.append_to_comments_check.isChecked()))
            set_pref('custom_append_column', str(self.custom_append_column_combo.get_selected_column() or ''))
            set_pref('summary_custom_tags', str(self.summary_custom_tags_edit.text() or '').strip())
            set_pref('summary_copy_source_cover', bool(self.summary_copy_source_cover_check.isChecked()))
            set_pref('chapter_cache_enabled', bool(self.chapter_cache_check.isChecked()))
            set_pref('multi_file_enabled', bool(self.multi_file_check.isChecked()))
            set_pref('max_chunk_words', int(self.max_chunk_words_spin.value()))
            set_pref('request_interval_free', int(self.request_interval_free_spin.value()))
            set_pref('request_interval_default', int(self.request_interval_default_spin.value()))
        except Exception:
            pass
        super().accept()
    def edit_extra_prompts(self):
        d = ExtraPromptAdditionsDialog(parent=self)
        try:
            d.setWindowIcon(self.windowIcon())
        except Exception:
            pass
        d.exec()


class ExtraPromptAdditionsDialog(Dialog):

    def __init__(self, parent=None):
        self._palette_signal_connected = False
        self._edits: dict[str, QPlainTextEdit] = {}
        self._tabs = None
        Dialog.__init__(self, _('Extra prompt additions'), 'chapter_summarizer_extra_prompts_dialog', parent=parent)

    def setup_ui(self):
        # Keep the initial height reasonable on high-DPI / large screens.
        self.resize(640, 420)
        self.setMinimumSize(560, 320)
        self.setMaximumHeight(700)
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        hint = QLabel(_(
            'These fields are appended to the AI user prompt.\n\n'
            'They cannot override the required output format (JSON-only) — keep them as style or constraint notes.\n'
            'They also cannot make the model browse the web or fetch outside information that is not already in the provided text.\n'
            'Newlines are collapsed to spaces and very long text is truncated.'
        ))
        hint.setWordWrap(True)
        root.addWidget(hint)

        tabs = QTabWidget(self)
        self._tabs = tabs
        root.addWidget(tabs)

        def add_tab(title: str, prefix: str):
            w = QWidget(self)
            layout = QFormLayout(w)
            layout.setVerticalSpacing(8)

            for label, key_suffix in (
                (_('Default (all document types)'), 'default'),
                (_('Fiction'), 'fiction'),
                (_('Non-fiction'), 'nonfiction'),
                (_('Other (news, article, magazine, reference, poetry, mixed)'), 'other'),
            ):
                edit = QPlainTextEdit(self)
                edit.setTabChangesFocus(True)
                install_ctrl_wheel_zoom(edit, min_pt=7.0, max_pt=22.0, step=1.0)
                edit.setPlaceholderText(_('Optional — keep it short.'))
                pref_key = f'extra_prompt_{prefix}_{key_suffix}'
                edit.setPlainText(str(pref(pref_key, '') or ''))
                try:
                    edit.setStyleSheet(ccr_combo_stylesheet())
                except Exception:
                    pass
                self._edits[pref_key] = edit
                layout.addRow(label + ':', edit)

            tabs.addTab(w, title)

        add_tab(_('Full summary'), 'full')
        add_tab(_('Flash overview'), 'flash')

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, parent=self)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        root.addWidget(bb)

        self._apply_runtime_theme()
        app = QApplication.instance()
        if app is not None and hasattr(app, 'palette_changed') and not self._palette_signal_connected:
            try:
                app.palette_changed.connect(self._apply_runtime_theme)
                self._palette_signal_connected = True
            except Exception:
                pass

    def _apply_runtime_theme(self):
        try:
            self.setStyleSheet(chapter_summarizer_tab_stylesheet(self) + ccr_scrollbar_stylesheet() + ccr_combo_stylesheet())
        except Exception:
            pass
        try:
            if self._tabs is not None:
                self._tabs.setStyleSheet(chapter_summarizer_tab_stylesheet(self._tabs))
        except Exception:
            pass
        try:
            combo_style = ccr_combo_stylesheet()
            for edit in (self._edits or {}).values():
                edit.setStyleSheet(combo_style)
        except Exception:
            pass

    def accept(self):
        def normalize(value: str) -> str:
            text = ' '.join(str(value or '').split())
            # Hard cap to keep prompts sane.
            return text[:400]

        try:
            for key, edit in (self._edits or {}).items():
                set_pref(key, normalize(edit.toPlainText()))
        except Exception:
            pass
        super().accept()


def _free_api_guide_html_nvidia() -> str:
    return _(
        '<p><b>Get an NVIDIA API key</b></p>'
        '<ol>'
        '<li>Go to <a href="https://build.nvidia.com/models?filters=nimType%253Anim_type_preview%3Aundefined%2CnimType%3Anim_type_preview&modal=signin">build.nvidia.com</a> and create a free account — just type your e-mail address to get the verification code.</li>'
        '<li>Then go to <a href="https://build.nvidia.com/settings/api-keys">API Keys</a> and click Generate API Key. Copy the key immediately — it is shown only once (typically starts with <code>nvapi-</code>). </li>'
        '<li>In Chapter Summarizer: choose <b>NVIDIA Free (API)</b> → <b>Configure provider</b> → paste the key → pick a model → use <b>Test connection</b>.</li>'
        '</ol>'
    )


def _free_api_guide_html_openrouter() -> str:
    return _(
        '<p>OpenRouter gives access to many cloud models through one simple API.</p>'
        '<p><b>Get an OpenRouter API key</b></p>'
        '<ol>'
        '<li>Create a free account at <a href="https://openrouter.ai">openrouter.ai</a> (email, Google, or GitHub).</li>'
        '<li>Open <a href="https://openrouter.ai/settings/keys">openrouter.ai/settings/keys</a>.</li>'
        '<li>Click <b>Create Key</b>, name it (for example <code>calibre-chapter-summarizer</code>), and copy the key immediately — it is shown only once (starts with <code>sk-or-</code>).</li>'
        '<li>In calibre: <b>Preferences → AI → OpenRouter</b> → paste the key.</li>'
        '<li>In Chapter Summarizer: choose <b>OpenRouter</b> as the provider. Optionally set a model override or configure the text model in calibre.</li>'
        '</ol>'
        '<p><b>Free tier:</b> about <b>50 requests/day</b> on free models (model IDs ending in <code>:free</code>). '
        'No credit card is required to use free models. With at least <b>$10</b> in credits: about <b>1,000 free-model requests/day</b> and <b>20 requests/minute</b> on free models.</p>'
    )


def _free_api_guide_html_lm_studio() -> str:
    return _(
        '<p>LM Studio runs models locally on your computer — there is no cloud signup or billing API key for basic use.</p>'
        '<p><b>Basic setup</b></p>'
        '<ol>'
        '<li>Install <a href="https://lmstudio.ai">LM Studio</a> and download a model.</li>'
        '<li>Open the <b>Developer</b> page and start the local server (toggle, or run <code>lms server start</code> in a terminal).</li>'
        '<li>Default server URL: <code>http://localhost:1234</code> (OpenAI-compatible).</li>'
        '<li>In calibre: <b>Preferences → AI → LM Studio</b> — confirm the URL, then load/select a model.</li>'
        '<li>In Chapter Summarizer: choose <b>LM Studio</b> as the provider.</li>'
        '</ol>'
    )


class FreeApiGuideDialog(Dialog):

    def __init__(self, parent=None):
        self._palette_signal_connected = False
        self._tabs = None
        self._scroll_areas: list[QScrollArea] = []
        Dialog.__init__(self, _('Free API guide'), 'chapter_summarizer_free_api_guide_dialog', parent=parent)

    def setup_ui(self):
        self.resize(660, 480)
        self.setMinimumSize(560, 360)
        self.setMaximumHeight(720)
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        tabs = QTabWidget(self)
        self._tabs = tabs

        def add_guide_tab(title: str, html: str):
            tab = QWidget(self)
            tab_layout = QVBoxLayout(tab)
            tab_layout.setContentsMargins(0, 0, 0, 0)
            scroll = QScrollArea(tab)
            scroll.setWidgetResizable(True)
            try:
                scroll.setFrameShape(QFrame.Shape.NoFrame)
            except Exception:
                pass
            body = QWidget(scroll)
            body_layout = QVBoxLayout(body)
            body_layout.setContentsMargins(8, 8, 8, 8)
            body_layout.setSpacing(10)
            label = QLabel(html, body)
            label.setWordWrap(True)
            label.setTextFormat(Qt.TextFormat.RichText)
            try:
                label.setOpenExternalLinks(False)
            except Exception:
                pass
            try:
                label.linkActivated.connect(open_url)
            except Exception:
                pass
            try:
                label.setTextInteractionFlags(
                    Qt.TextInteractionFlag.TextSelectableByMouse | Qt.TextInteractionFlag.LinksAccessibleByMouse
                )
            except Exception:
                pass
            body_layout.addWidget(label)
            body_layout.addStretch(1)
            scroll.setWidget(body)
            tab_layout.addWidget(scroll)
            self._scroll_areas.append(scroll)
            tabs.addTab(tab, title)

        add_guide_tab(_('NVIDIA Free'), _free_api_guide_html_nvidia())
        add_guide_tab(_('OpenRouter'), _free_api_guide_html_openrouter())
        add_guide_tab(_('LM Studio'), _free_api_guide_html_lm_studio())
        root.addWidget(tabs, 1)

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, parent=self)
        try:
            bb.button(QDialogButtonBox.StandardButton.Close).clicked.connect(self.accept)
        except Exception:
            bb.rejected.connect(self.reject)
        root.addWidget(bb)

        self._apply_runtime_theme()
        app = QApplication.instance()
        if app is not None and hasattr(app, 'palette_changed') and not self._palette_signal_connected:
            try:
                app.palette_changed.connect(self._apply_runtime_theme)
                self._palette_signal_connected = True
            except Exception:
                pass

    def _apply_runtime_theme(self):
        sheet = chapter_summarizer_tab_stylesheet(self) + ccr_scrollbar_stylesheet() + ccr_combo_stylesheet()
        try:
            self.setStyleSheet(sheet)
        except Exception:
            pass
        try:
            if self._tabs is not None:
                self._tabs.setStyleSheet(chapter_summarizer_tab_stylesheet(self._tabs))
        except Exception:
            pass
        scroll_style = ccr_scrollbar_stylesheet()
        for scroll in self._scroll_areas:
            try:
                scroll.setStyleSheet(scroll_style)
            except Exception:
                pass


class WorkerSignals(QObject):
    progress = pyqtSignal(str, int, int)
    preview = pyqtSignal(str)
    finished = pyqtSignal(object)
    cancelled = pyqtSignal(object)
    error = pyqtSignal(str, str, object)
    log = pyqtSignal(str)
    status = pyqtSignal(str)


class NoUnderlineLinkLabel(QLabel):
    def __init__(self, text, url, parent=None):
        super().__init__(text, parent)
        self._url = url
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            try:
                open_url(self._url)
            except Exception:
                try:
                    from qt.core import QDesktopServices
                    QDesktopServices.openUrl(QUrl(self._url))
                except Exception:
                    pass
        super().mousePressEvent(event)


def _now_stamp() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _timestamp_suffix() -> str:
    return datetime.now().strftime('%Y-%m-%d_%H-%M-%S')


def _safe_filename_component(text: str, fallback: str) -> str:
    value = (text or '').strip()
    if not value:
        value = fallback
    value = re.sub(r'[\\/:*?"<>|]+', '-', value)
    value = re.sub(r'\s+', ' ', value).strip(' .-_')
    value = value.replace(' ', '_')
    return value[:90] or fallback


def _html_escape(text: str) -> str:
    return html.escape('' if text is None else str(text), quote=True)


def _summary_css_from_palette(palette: QPalette) -> str:
    try:
        base = palette.color(QPalette.ColorRole.Base).name()
        text = palette.color(QPalette.ColorRole.Text).name()
        link = palette.color(QPalette.ColorRole.Link).name()
        mid = palette.color(QPalette.ColorRole.Mid).name()
    except Exception:
        base, text, link, mid = '#ffffff', '#222222', '#1e6bd6', '#888888'
    return f'''
body {{ background: {base}; color: {text}; font: 10pt/1.45 -apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Helvetica,Arial,sans-serif; margin: 0; }}
.page {{ max-width: 980px; margin: 0 auto; padding: 18px 22px; }}
h1 {{ font-size: 18pt; margin: 0 0 12px 0; color: {text}; text-indent: 0.6em; }}
h2 {{ font-size: 13.5pt; margin: 18px 0 8px 0; color: {link}; text-indent: 0.9em; }}
.meta {{ list-style: none; padding: 10px 12px; margin: 0 0 14px 0; border: 1px solid {mid}; border-radius: 8px; background: rgba(0,0,0,0.02); }}
.meta li {{ margin: 4px 0; }}
.meta .k {{ color: {mid}; font-weight: 600; }}
.meta .v {{ color: {text}; }}
p {{ margin: 0 0 10px 0; text-indent: 1.35em; }}
hr {{ border: 0; border-top: 1px solid {mid}; margin: 16px 0; }}
strong {{ font-weight: 700; }}
code {{ font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace; font-size: 0.95em; }}
.partial-banner {{ border: 1px solid {link}; border-left: 4px solid {link}; border-radius: 8px; padding: 10px 12px; margin: 0 0 14px 0; background: rgba(0,0,0,0.03); color: {text}; font-weight: 600; }}
'''.strip()


def normalize_summary_output_style(value: str) -> str:
    style = str(value or 'auto').strip().lower()
    if style not in {'auto', 'light', 'dark', 'sepia', 'custom'}:
        return 'auto'
    return style


def summary_style_css_preset(name: str = 'light') -> str:
    style = normalize_summary_output_style(name)
    if style == 'dark':
        return '''
body { background: #11161D; color: #D7E1EC; font: 10pt/1.5 -apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Helvetica,Arial,sans-serif; margin: 0; }
.page { max-width: 980px; margin: 0 auto; padding: 18px 22px; }
h1 { font-size: 18pt; margin: 0 0 12px 0; color: #E6EEF8; text-indent: 0.6em; }
h2 { font-size: 13.5pt; margin: 18px 0 8px 0; color: #8FC8FF; text-indent: 0.9em; }
.meta { list-style: none; padding: 10px 12px; margin: 0 0 14px 0; border: 1px solid #324356; border-radius: 8px; background: #18212B; }
.meta li { margin: 4px 0; }
.meta .k { color: #93A6BB; font-weight: 600; }
.meta .v { color: #D7E1EC; }
p { margin: 0 0 10px 0; text-indent: 1.35em; }
hr { border: 0; border-top: 1px solid #324356; margin: 16px 0; }
strong { font-weight: 700; }
code { font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace; font-size: 0.95em; }
.partial-banner { border: 1px solid #8FC8FF; border-left: 4px solid #8FC8FF; border-radius: 8px; padding: 10px 12px; margin: 0 0 14px 0; background: #18212B; color: #E6EEF8; font-weight: 600; }
'''.strip()
    if style == 'sepia':
        return '''
body { background: #F6F3E9; color: #39322B; font: 10pt/1.5 -apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Helvetica,Arial,sans-serif; margin: 0; }
.page { max-width: 980px; margin: 0 auto; padding: 18px 22px; }
h1 { font-size: 18pt; margin: 0 0 12px 0; color: #39322B; text-indent: 0.6em; }
h2 { font-size: 13.5pt; margin: 18px 0 8px 0; color: #7A5A30; text-indent: 0.9em; }
.meta { list-style: none; padding: 10px 12px; margin: 0 0 14px 0; border: 1px solid #D7C9A8; border-radius: 8px; background: #FBF7ED; }
.meta li { margin: 4px 0; }
.meta .k { color: #8E7A5D; font-weight: 600; }
.meta .v { color: #39322B; }
p { margin: 0 0 10px 0; text-indent: 1.35em; }
hr { border: 0; border-top: 1px solid #D7C9A8; margin: 16px 0; }
strong { font-weight: 700; }
code { font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace; font-size: 0.95em; }
.partial-banner { border: 1px solid #A8864D; border-left: 4px solid #A8864D; border-radius: 8px; padding: 10px 12px; margin: 0 0 14px 0; background: #F3EBD8; color: #39322B; font-weight: 600; }
'''.strip()
    return '''
body { background: #F8F7F3; color: #24303C; font: 10pt/1.5 -apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Helvetica,Arial,sans-serif; margin: 0; }
.page { max-width: 980px; margin: 0 auto; padding: 18px 22px; }
h1 { font-size: 18pt; margin: 0 0 12px 0; color: #1E2A36; text-indent: 0.6em; }
h2 { font-size: 13.5pt; margin: 18px 0 8px 0; color: #355D8A; text-indent: 0.9em; }
.meta { list-style: none; padding: 10px 12px; margin: 0 0 14px 0; border: 1px solid #C6C4BF; border-radius: 8px; background: #F6F4F0; }
.meta li { margin: 4px 0; }
.meta .k { color: #76879A; font-weight: 600; }
.meta .v { color: #24303C; }
p { margin: 0 0 10px 0; text-indent: 1.35em; }
hr { border: 0; border-top: 1px solid #C6C4BF; margin: 16px 0; }
strong { font-weight: 700; }
code { font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace; font-size: 0.95em; }
.partial-banner { border: 1px solid #355D8A; border-left: 4px solid #355D8A; border-radius: 8px; padding: 10px 12px; margin: 0 0 14px 0; background: #EEF2F7; color: #1E2A36; font-weight: 600; }
'''.strip()


def default_summary_html_css() -> str:
    return summary_style_css_preset('light')


def summary_html_css_value(style_name: str = 'auto', palette: QPalette | None = None, custom_css: str = '') -> str:
    style = normalize_summary_output_style(style_name)
    if style == 'auto':
        return _summary_css_from_palette(palette or QPalette())
    if style == 'custom':
        css = str(custom_css or '').strip()
        return css or default_summary_html_css()
    return summary_style_css_preset(style)


def _is_dark_widget_theme(widget=None) -> bool:
    try:
        app = QApplication.instance()
        palette = widget.palette() if widget is not None else (app.palette() if app is not None else None)
        if palette is None:
            return False
        window_color = palette.color(QPalette.ColorRole.Window)
        text_color = palette.color(QPalette.ColorRole.WindowText)
        return text_color.lightness() > window_color.lightness()
    except Exception:
        return False


def _goldendict_theme_colors(widget=None) -> dict[str, str]:
    if _is_dark_widget_theme(widget):
        return {
            'accent': '#E0B84D',
            'accent_hover': '#F0C96A',
            'tab_selected_text': '#F6EDC2',
            'text': '#D7E1EC',
            'muted': '#93A6BB',
            'border': '#324356',
            'tab_bg': '#18212B',
            'tab_hover': '#223040',
            'pane_bg': '#11161D',
        }
    return {
        'accent': '#C6A22A',
        'accent_hover': '#D8B84D',
        'tab_selected_text': '#A27A14',
        'text': '#24303C',
        'muted': '#8F7942',
        'border': '#C6C4BF',
        'tab_bg': '#F8F4EA',
        'tab_hover': '#F6EFD8',
        'pane_bg': '#ECEAE6',
    }


def _footer_version_label_stylesheet(widget=None) -> str:
    colors = _goldendict_theme_colors(widget)
    return (
        'QLabel {'
        ' font-size: 8pt;'
        f' color: {colors["muted"]};'
        ' font-weight: 600;'
        ' padding: 0px 4px 0px 0px;'
        '}'
    )


def _primary_action_button_stylesheet(widget=None) -> str:
    if _is_dark_widget_theme(widget):
        border = '#5CB85C'
        border_hover = '#72C572'
        border_pressed = '#449D44'
        bg_tint = 'rgba(92, 184, 92, 0.14)'
        bg_hover = 'rgba(92, 184, 92, 0.22)'
    else:
        border = '#2E7D32'
        border_hover = '#388E3C'
        border_pressed = '#1B5E20'
        bg_tint = 'rgba(46, 125, 50, 0.08)'
        bg_hover = 'rgba(46, 125, 50, 0.14)'
    return (
        'QPushButton#primaryAction {'
        ' font-weight: 700;'
        f' border: 2px solid {border};'
        ' border-radius: 6px;'
        f' background-color: {bg_tint};'
        ' padding: 5px 12px;'
        '}'
        'QPushButton#primaryAction:hover {'
        f' border-color: {border_hover};'
        f' background-color: {bg_hover};'
        '}'
        'QPushButton#primaryAction:pressed {'
        f' border-color: {border_pressed};'
        '}'
        'QPushButton#primaryAction:disabled {'
        ' border: 2px solid palette(mid);'
        ' background-color: palette(base);'
        ' color: palette(mid);'
        '}'
    )


def chapter_summarizer_tab_stylesheet(widget=None) -> str:
    c = _goldendict_theme_colors(widget)
    return (
        'QTabWidget::pane {'
        ' border-top: none;'
        ' margin-left: 8px;'
        ' margin-right: 8px;'
        f' background: {c["pane_bg"]};'
        '}'
        'QTabBar::tab {'
        ' min-width: 96px;'
        ' padding-top: 6px;'
        ' padding-bottom: 6px;'
        ' padding-left: 12px;'
        ' padding-right: 12px;'
        ' font-size: 9pt;'
        ' font-style: normal;'
        ' font-weight: 600;'
        f' background: {c["tab_bg"]};'
        f' color: {c["text"]};'
        ' border-top: 1px solid palette(dark);'
        ' border-left: 1px solid palette(dark);'
        ' border-right: 1px solid palette(dark);'
        ' border-bottom: none;'
        ' margin-right: 3px;'
        ' border-top-left-radius: 7px;'
        ' border-top-right-radius: 7px;'
        '}'
        'QTabBar::tab:selected {'
        ' font-style: normal;'
        ' font-weight: 700;'
        f' color: {c.get("tab_selected_text", c["accent"])};'
        f' background: {c["pane_bg"]};'
        f' border-top: 2px solid {c["accent"]};'
        f' border-left: 2px solid {c["accent"]};'
        f' border-right: 2px solid {c["accent"]};'
        '}'
        'QTabBar::tab:hover:!selected {'
        f' background: {c["tab_hover"]};'
        f' color: {c["text"]};'
        '}'
        'QTabBar::tab:!selected {'
        ' font-style: normal;'
        ' margin-top: 2px;'
        '}'
    )


def compact_subtab_stylesheet() -> str:
    return """
        QTabWidget::pane {
            border: 1px solid palette(dark);
            background-color: palette(base);
            margin-top: 2px;
        }
        QTabBar::tab {
            background-color: palette(window);
            border: 1px solid palette(dark);
            border-bottom: none;
            border-top-left-radius: 3px;
            border-top-right-radius: 3px;
            padding: 4px 8px;
            margin-right: 0px;
            font-size: 9pt;
            font-style: normal;
            font-weight: normal;
            min-width: 160px;
        }
        QTabBar::tab:selected {
            background-color: palette(window);
            color: palette(text);
            font-style: normal;
            font-weight: bold;
            border: 2px solid #FFA500;
            border-bottom: none;
        }
        QTabBar::tab:!selected {
            color: palette(text);
            background-color: palette(window);
            font-style: normal;
        }
    """


def ccr_scrollbar_stylesheet() -> str:
    return """
QScrollBar::handle {
   border: 1px solid #5B6985;
}
QScrollBar:vertical {
    background: transparent;
    width: 12px;
    margin: 0px 0px 0px 0px;
    padding: 6px 0px 6px 0px;
}
QScrollBar::handle:vertical {
    background: rgba(140, 172, 204, 0.25);
    min-height: 22px;
    border-radius: 4px;
    margin: 4px 0px;
}
QScrollBar::handle:vertical:hover {
    background: rgba(140, 172, 204, 0.45);
}
QScrollBar:horizontal {
    background: transparent;
    height: 12px;
    margin: 0px 0px 0px 0px;
    padding: 0px 6px 0px 6px;
}
QScrollBar::handle:horizontal {
    background: rgba(140, 172, 204, 0.25);
    min-width: 22px;
    border-radius: 4px;
    margin: 0px 4px;
}
QScrollBar::handle:horizontal:hover {
    background: rgba(140, 172, 204, 0.45);
}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical,
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {
    background: none;
}
"""


def ccr_combo_stylesheet() -> str:
    return '''
QComboBox {
    border: 1px solid palette(mid);
    min-height: 20px;
    padding: 2px 6px;
}
QComboBox:focus {
    border: 1px solid palette(link);
}
QLineEdit {
    border: 1px solid palette(mid);
    min-height: 20px;
    padding: 2px 6px;
}
QLineEdit:focus {
    border: 1px solid palette(link);
}
'''


_BOLD_RE = re.compile(r'\*\*([^*]+)\*\*')


def _inline_markdown_to_html(text: str) -> str:
    s = _html_escape(text)
    s = _BOLD_RE.sub(lambda m: f'<strong>{m.group(1)}</strong>', s)
    return s


def _render_meta_list(items: list[str]) -> str:
    def render_meta_segment(segment: str) -> str:
        text = segment.strip()
        if ':' in text:
            key, val = text.split(':', 1)
            key = key.strip()
            val = val.strip()
            if key:
                return f'<span class="k">{_html_escape(key)}</span>: <span class="v">{_inline_markdown_to_html(val)}</span>'
        return _inline_markdown_to_html(text)

    lis: list[str] = []
    for raw in items:
        body = raw.strip()[2:].strip() if raw.strip().startswith('- ') else raw.strip()
        if ' | ' in body:
            parts = [render_meta_segment(part) for part in body.split(' | ')]
            lis.append(f'<li>{" | ".join(parts)}</li>')
            continue
        if ':' in body:
            lis.append(f'<li>{render_meta_segment(body)}</li>')
            continue
        lis.append(f'<li>{_inline_markdown_to_html(body)}</li>')
    return '<ul class="meta">' + ''.join(lis) + '</ul>'


def summary_html_layout_css(layout_mode: str = 'standard') -> str:
    mode = str(layout_mode or 'standard').strip().lower()
    if mode in {'browser', 'html-export', 'wide'}:
        return '.page { width: 70%; max-width: 1200px; min-width: 320px; margin: 0 auto; }'
    return ''


def build_summary_html(markdown_text: str, palette: QPalette | None = None, style_name: str = 'auto', custom_css: str = '', layout_mode: str = 'standard') -> str:
    md = (markdown_text or '').replace('\r\n', '\n').replace('\r', '\n')
    lines = md.split('\n')
    partial_banner = ''
    if lines and lines[0].startswith('[PARTIAL RESULT]'):
        partial_banner = lines[0].strip()
        lines = lines[1:]
        while lines and not lines[0].strip():
            lines = lines[1:]
    title = ''
    if lines and lines[0].startswith('# '):
        title = lines[0][2:].strip()
    css = summary_html_css_value(style_name=style_name, palette=palette or QPalette(), custom_css=custom_css)
    layout_css = summary_html_layout_css(layout_mode)
    if layout_css:
        css = css + '\n' + layout_css

    body_parts: list[str] = ['<div class="page">']
    if partial_banner:
        body_parts.append(f'<div class="partial-banner">{_html_escape(partial_banner)}</div>')
    if title:
        body_parts.append(f'<h1>{_html_escape(title)}</h1>')

    idx = 1 if title else 0
    # Skip leading blank lines
    while idx < len(lines) and not lines[idx].strip():
        idx += 1

    # Collect meta bullets until the first chapter header
    meta_lines: list[str] = []
    while idx < len(lines):
        line = lines[idx].rstrip()
        if line.startswith('## '):
            break
        if line.strip().startswith('- '):
            meta_lines.append(line.strip())
        idx += 1
    if meta_lines:
        body_parts.append(_render_meta_list(meta_lines))

    # Render the remainder
    paragraph: list[str] = []

    def flush_paragraph():
        nonlocal paragraph
        if paragraph:
            text = ' '.join(x.strip() for x in paragraph if x.strip()).strip()
            if text:
                body_parts.append(f'<p>{_inline_markdown_to_html(text)}</p>')
        paragraph = []

    while idx < len(lines):
        line = lines[idx].rstrip()
        if line.startswith('## '):
            flush_paragraph()
            body_parts.append(f'<h2>{_html_escape(line[3:].strip())}</h2>')
            idx += 1
            continue
        if not line.strip():
            flush_paragraph()
            idx += 1
            continue
        if line.strip().startswith('- '):
            # Treat late bullet lines as normal paragraphs
            paragraph.append(line.strip())
            idx += 1
            continue
        paragraph.append(line)
        idx += 1
    flush_paragraph()
    body_parts.append('</div>')

    safe_doc_title = _html_escape(title or 'Summary')
    return (
        '<!DOCTYPE html><html><head><meta charset="utf-8"/>'
        f'<title>{safe_doc_title}</title><style>{css}</style></head><body>'
        + '\n'.join(body_parts)
        + '</body></html>'
    )


class SummaryStyleDialog(QDialog):

    def __init__(self, initial_css: str = '', parent=None):
        super().__init__(parent)
        self.setWindowTitle(_('Summary output style'))
        self.resize(760, 520)

        layout = QVBoxLayout(self)
        info = QLabel(_(
            'Edit the CSS used for HTML/EPUB export, browser preview, and Add as new book output. '
            'Viewer themes are independent, so use an explicit preset here when you want predictable readability.'), self)
        info.setWordWrap(True)
        layout.addWidget(info)

        self.editor = QPlainTextEdit(self)
        self.editor.setPlainText((initial_css or '').strip() or default_summary_html_css())
        layout.addWidget(self.editor, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self)
        light = buttons.addButton(_('Use light preset'), QDialogButtonBox.ButtonRole.ActionRole)
        dark = buttons.addButton(_('Use dark preset'), QDialogButtonBox.ButtonRole.ActionRole)
        sepia = buttons.addButton(_('Use sepia preset'), QDialogButtonBox.ButtonRole.ActionRole)
        restore = buttons.addButton(_('Restore default'), QDialogButtonBox.ButtonRole.ResetRole)
        light.clicked.connect(lambda: self.editor.setPlainText(summary_style_css_preset('light')))
        dark.clicked.connect(lambda: self.editor.setPlainText(summary_style_css_preset('dark')))
        sepia.clicked.connect(lambda: self.editor.setPlainText(summary_style_css_preset('sepia')))
        restore.clicked.connect(lambda: self.editor.setPlainText(default_summary_html_css()))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    @property
    def value(self):
        return self.editor.toPlainText().strip() or default_summary_html_css()


def export_html_to_epub(html_doc: str, filepath: str) -> tuple[bool, str]:
    tmp_html = os.path.join(tempfile.gettempdir(), f'chapter_summarizer_export_{_timestamp_suffix()}.html')
    try:
        with open(tmp_html, 'w', encoding='utf-8') as f:
            f.write(html_doc or '')
        try:
            from calibre.ebooks.conversion.cli import main as convert_main
            convert_main(['ebook-convert', tmp_html, filepath, '--no-default-epub-cover'])
            return True, ''
        except SystemExit:
            return True, ''
        except Exception:
            try:
                from calibre.ebooks.conversion.plumber import Plumber
                from calibre.utils.logging import Log
                log = Log()
                plumber = Plumber(tmp_html, filepath, log)
                plumber.run()
                return True, ''
            except Exception as e:
                return False, str(e)
    finally:
        try:
            os.remove(tmp_html)
        except Exception:
            pass


def generate_summary_cover_bytes(title: str, authors: list[str] | tuple[str, ...] | None = None, source_cover_bytes: bytes | None = None) -> bytes | None:
    """Generate a summary book cover using calibre's built-in cover engine.

    Uses the same styling system as calibre's "Cover generation settings" dialog:
    fonts, sizes, colour themes and styles all come from ``cprefs`` (or the
    user's saved global prefs).  The GPM title template shortens the book
    title to 90 characters so the cover looks clean.

    If *source_cover_bytes* is provided, the original book's cover is
    rendered as a small picture-in-picture inset in the lower-right corner.
    """
    try:
        from calibre.ebooks.covers import generate_cover, override_prefs, cprefs
        from calibre.ebooks.metadata.book.base import Metadata

        raw_title = str(title or '').strip() or _('Untitled summary')
        label = _('Summary')
        cover_badge_label = _('CS Summary')
        base_title = raw_title
        parts = [p.strip() for p in raw_title.split(' — ') if p.strip()]
        if len(parts) >= 2:
            if parts[0].casefold() == label.casefold():
                base_title = ' — '.join(parts[1:]).strip() or raw_title
            elif parts[-1].casefold() == label.casefold():
                base_title = ' — '.join(parts[:-1]).strip() or raw_title

        author_list = [str(a).strip() for a in (authors or []) if str(a).strip()] or [_('Chapter Summarizer')]
        mi = Metadata(base_title, author_list)

        # Use a short plugin badge while leaving original authors in the footer.
        mi.series = cover_badge_label
        mi.series_index = 0

        prefs = override_prefs(
            cprefs,
            # GPM template: shorten title to 90 chars
            title_template="program: shorten($title, 90, '...', 0)",
            subtitle_template="program: strcat('<i>', field('series'), '</i>')",
            footer_template="program: authors = field('authors');\n"
                            "num = count(authors, ' &amp; ');\n"
                            "authors = sublist(authors, 0, 2, ' &amp; ');\n"
                            "authors = list_re(authors, ' &amp; ', '(.+)', '<b>\\1');\n"
                            "authors = re(authors, ' &amp; ', '<br>');\n"
                            "re(authors, '&amp;&amp;', '&amp;')",
        )

        img = generate_cover(mi, prefs=prefs, as_qimage=True)

        # --- Source cover inset overlay ---
        pixmap = QPixmap.fromImage(img)
        if source_cover_bytes:
            source_pixmap = QPixmap()
            if source_pixmap.loadFromData(source_cover_bytes) and not source_pixmap.isNull():
                pw, ph = pixmap.width(), pixmap.height()
                # Smaller inset, centred well above the footer to never overlap author names
                inset_w = max(160, int(pw * 0.22))
                inset_h = max(220, int(inset_w * 1.42))
                margin = int(pw * 0.04)
                inset_x = max(margin, (pw - inset_w) // 2)
                # Reserve more space for footer to prevent any overlap
                footer_reserve = max(140, int(ph * 0.20))
                inset_y = min(int(ph * 0.52), ph - footer_reserve - inset_h - margin * 2)

                scaled = source_pixmap.scaled(
                    inset_w, inset_h,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
                # Centre the scaled image within the inset box
                draw_x = inset_x + max(0, (inset_w - scaled.width()) // 2)
                draw_y = inset_y + max(0, (inset_h - scaled.height()) // 2)

                painter = QPainter(pixmap)
                try:
                    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
                    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
                except Exception:
                    pass
                # White mat behind the inset
                painter.fillRect(
                    inset_x - 6, inset_y - 6,
                    inset_w + 12, inset_h + 12,
                    QColor(255, 255, 255, 210),
                )
                # Dark border
                painter.setPen(QPen(QColor('#2E3845'), 3))
                painter.drawRect(inset_x - 3, inset_y - 3, inset_w + 6, inset_h + 6)
                # Draw the source cover
                painter.drawPixmap(draw_x, draw_y, scaled)
                painter.end()

        data = QByteArray()
        buffer = QBuffer(data)
        try:
            buffer.open(QBuffer.OpenModeFlag.WriteOnly)
        except Exception:
            buffer.open(QBuffer.WriteOnly)
        ok = pixmap.save(buffer, 'PNG')
        buffer.close()
        if ok:
            return bytes(data)
    except Exception:
        return None
    return None


class TocWorkerSignals(QObject):
    finished = pyqtSignal(object)
    error = pyqtSignal(str, str)


class TocTreeWidget(QTreeWidget):

    openRequested = pyqtSignal(object, int)

    def keyPressEvent(self, event):
        try:
            if event.key() in {Qt.Key.Key_Return, Qt.Key.Key_Enter}:
                item = self.currentItem()
                if item is not None:
                    self.openRequested.emit(item, self.currentColumn())
                    event.accept()
                    return
        except Exception:
            pass
        super().keyPressEvent(event)


def _extract_toc_tree_from_container(container) -> list[dict[str, object]]:
    try:
        from calibre.ebooks.oeb.polish.toc import get_toc
        toc = get_toc(container, verify_destinations=False)
    except Exception:
        toc = None
    if toc is None:
        return []

    def walk(node):
        items = []
        for child in node:
            items.append({
                'title': str(getattr(child, 'title', '') or ''),
                'dest': str(getattr(child, 'dest', '') or ''),
                'frag': str(getattr(child, 'frag', '') or ''),
                'children': walk(child),
            })
        return items

    return walk(toc)


def _count_toc_nodes_in_container(container) -> int | None:
    try:
        from calibre.ebooks.oeb.polish.toc import get_toc
        toc = get_toc(container, verify_destinations=False)
    except Exception:
        toc = None
    if toc is None:
        return None
    try:
        return len(list(toc.iterdescendants()))
    except Exception:
        return None


def count_toc_nodes_for_path(path: str) -> int | None:
    fmt = os.path.splitext(path or '')[1].lower().lstrip('.')
    container = None
    exploded_root = ''
    try:
        # AZW3 (KF8) files are supported by calibre's get_container just like EPUB.
        # Calibre's restriction on ToC *editing* doesn't apply to ToC *reading*.
        if fmt in {'epub', 'kepub', 'azw3'}:
            try:
                from calibre.ebooks.oeb.polish.container import get_container
            except Exception:
                get_container = None
            if callable(get_container):
                container = get_container(path, tweak_mode=True)
                return _count_toc_nodes_in_container(container)

        # Future hook for other reflowable formats.
        # MOBI (non-KF8) requires KindleUnpack extraction.
        # HTMLZ/DOCX could potentially be added via similar extraction flows.
        if fmt in {'htmlz', 'mobi', 'docx'}:
            return None
        return None
    finally:
        root = getattr(container, 'root', '') if container is not None else ''
        if root and os.path.isdir(root):
            shutil.rmtree(root, ignore_errors=True)
        if exploded_root and os.path.isdir(exploded_root):
            shutil.rmtree(exploded_root, ignore_errors=True)


def extract_toc_tree(path: str) -> list[dict[str, object]]:
    """Extract ToC tree from a book file.

    Supports EPUB, KEPUB, and AZW3 via calibre's container API.
    Falls back to text-based chapter detection for other formats.
    """
    try:
        from calibre.ebooks.oeb.polish.container import get_container
    except Exception:
        get_container = None

    container = None
    fmt = os.path.splitext(path or '')[1].lower().lstrip('.')
    try:
        # Try container-based extraction for supported formats
        if callable(get_container) and fmt in {'epub', 'kepub', 'azw3'}:
            container = get_container(path, tweak_mode=True)
            items = _extract_toc_tree_from_container(container)
            if items:
                return items
        # Fallback: use our parser's detected chapters (flat)
        doc = parse_document(path, os.path.splitext(os.path.basename(path))[0])
        return [{'title': f'{c.index}. {c.title}', 'dest': '', 'frag': '', 'children': []} for c in (doc.chapters or [])]
    finally:
        root = getattr(container, 'root', '') if container is not None else ''
        if root and os.path.isdir(root):
            shutil.rmtree(root, ignore_errors=True)


class FastTocViewerDialog(QDialog):

    def __init__(self, path: str, title: str = '', gui=None, book_id=None, book_format: str = '', parent=None):
        super().__init__(parent)
        self.path = path
        self.gui = gui
        self.book_id = book_id
        self.book_format = str(book_format or '').upper()
        self.setWindowTitle(_('Table of Contents') + (f' — {title}' if title else ''))
        self.resize(720, 520)
        try:
            if parent is not None:
                self.setWindowIcon(parent.windowIcon())
        except Exception:
            pass
        try:
            self.setStyleSheet(chapter_summarizer_tab_stylesheet() + ccr_scrollbar_stylesheet() + ccr_combo_stylesheet())
        except Exception:
            pass

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(6)

        self.filter_edit = QLineEdit(self)
        self.filter_edit.setPlaceholderText(_('Filter ToC…'))
        self.filter_edit.textChanged.connect(self.apply_filter)
        try:
            self.filter_edit.setStyleSheet(ccr_combo_stylesheet())
        except Exception:
            pass
        root.addWidget(self.filter_edit)

        # Loading indicator with animation
        self._loading_frames = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏']
        self._loading_index = 0
        self._show_loading_indicator = self._should_show_loading_indicator()

        self.status = QLabel(self)
        self.status.setStyleSheet('color: gray; font-size: 9pt;')
        root.addWidget(self.status)

        if self._show_loading_indicator:
            self._loading_timer = QTimer(self)
            self._loading_timer.timeout.connect(self._update_loading_indicator)
            self._loading_timer.start(100)
            self._update_loading_indicator()
        else:
            self.status.setText(_('Loading…'))

        self.tree = TocTreeWidget(self)
        self.tree.setHeaderHidden(True)
        self.tree.setUniformRowHeights(True)
        install_ctrl_wheel_zoom(self.tree)
        self.tree.itemActivated.connect(self._open_selected_item_in_viewer)
        self.tree.openRequested.connect(self._open_selected_item_in_viewer)
        self.tree.setToolTip(_('Double-click or press Enter on a ToC item to open that chapter in calibre viewer when the source is a library book in a reflowable format.'))
        root.addWidget(self.tree, 1)

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, parent=self)
        bb.rejected.connect(self.reject)
        bb.accepted.connect(self.accept)
        try:
            close_button = bb.button(QDialogButtonBox.StandardButton.Close)
            if close_button is not None:
                close_button.setAutoDefault(False)
                close_button.setDefault(False)
        except Exception:
            pass
        root.addWidget(bb)

        self._signals = TocWorkerSignals()
        self._signals.finished.connect(self._on_loaded)
        self._signals.error.connect(self._on_error)
        self._thread = Thread(target=self._load, name='ChapterSummarizerTocWorker', daemon=True)
        self._thread.start()

    def _should_show_loading_indicator(self) -> bool:
        """Show loading indicator for AZW3 or large EPUB files (>5 MB)."""
        try:
            ext = os.path.splitext(self.path or '')[1].lower()
            if ext == '.azw3':
                return True
            if ext in {'.epub', '.kepub'}:
                size_mb = os.path.getsize(self.path) / (1024 * 1024)
                return size_mb > 5.0
        except Exception:
            pass
        return False

    def _update_loading_indicator(self):
        """Animate the loading indicator spinner."""
        if self._show_loading_indicator:
            frame = self._loading_frames[self._loading_index % len(self._loading_frames)]
            self._loading_index += 1
            self.status.setText(f'{frame} {_("Loading ToC…")}')

    def _load(self):
        try:
            items = extract_toc_tree(self.path)
            self._signals.finished.emit(items)
        except Exception as e:
            self._signals.error.emit(str(e), traceback.format_exc())

    def _add_items(self, parent_item, items: list[dict[str, object]]):
        for entry in items:
            title = str((entry or {}).get('title') or '').strip() or _('(Untitled)')
            dest = str((entry or {}).get('dest') or '').strip()
            frag = str((entry or {}).get('frag') or '').strip()
            node = QTreeWidgetItem([title])
            node.setData(0, Qt.ItemDataRole.UserRole, title.casefold())
            node.setData(0, Qt.ItemDataRole.UserRole + 1, dest)
            node.setData(0, Qt.ItemDataRole.UserRole + 2, frag)
            node.setData(0, Qt.ItemDataRole.UserRole + 3, title)
            if parent_item is None:
                self.tree.addTopLevelItem(node)
            else:
                parent_item.addChild(node)
            children = (entry or {}).get('children') or []
            if isinstance(children, list) and children:
                self._add_items(node, children)

    def _on_loaded(self, items):
        # Stop loading animation if running
        if self._show_loading_indicator:
            try:
                self._loading_timer.stop()
            except Exception:
                pass

        self.tree.clear()
        if not items:
            self.status.setText(_('No ToC items found.'))
            return
        self._add_items(None, items)
        self.tree.expandAll()
        self.status.setText(_('%d ToC item(s)') % self._count_items())
        self.apply_filter()

    def _count_items(self) -> int:
        count = 0

        def walk(item: QTreeWidgetItem):
            nonlocal count
            count += 1
            for i in range(item.childCount()):
                walk(item.child(i))

        for i in range(self.tree.topLevelItemCount()):
            walk(self.tree.topLevelItem(i))
        return count

    def _on_error(self, msg: str, details: str):
        if self._show_loading_indicator:
            try:
                self._loading_timer.stop()
            except Exception:
                pass
        self.status.setText(_('Failed to load ToC.'))
        error_dialog(self, _('ToC failed'), msg, det_msg=details, show=True)

    def apply_filter(self):
        query = (self.filter_edit.text() or '').strip().casefold()

        def match_item(item: QTreeWidgetItem) -> bool:
            text_key = str(item.data(0, Qt.ItemDataRole.UserRole) or '')
            matched = (not query) or (query in text_key)
            any_child = False
            for i in range(item.childCount()):
                if match_item(item.child(i)):
                    any_child = True
            visible = matched or any_child
            item.setHidden(not visible)
            return visible

        for i in range(self.tree.topLevelItemCount()):
            match_item(self.tree.topLevelItem(i))

    def _open_selected_item_in_viewer(self, item: QTreeWidgetItem, _column: int = 0):
        if item is None or self.gui is None or self.book_id is None or not self.book_format:
            return
        open_at = None
        dest = str(item.data(0, Qt.ItemDataRole.UserRole + 1) or '').strip()
        frag = str(item.data(0, Qt.ItemDataRole.UserRole + 2) or '').strip()
        title = str(item.data(0, Qt.ItemDataRole.UserRole + 3) or item.text(0) or '').strip()
        if dest:
            href = dest + (('#' + frag) if frag else '')
            open_at = 'toc-href:' + href
        elif title:
            open_at = 'toc:' + title
        if not open_at:
            return
        try:
            self.gui.iactions['View'].view_format_by_id(self.book_id, self.book_format, open_at=open_at)
        except Exception:
            if title:
                try:
                    self.gui.iactions['View'].view_format_by_id(self.book_id, self.book_format, open_at='search:' + title)
                except Exception:
                    try:
                        self.gui.iactions['View'].view_historical(self.book_id)
                    except Exception:
                        pass


class ChapterSelectorDialog(QDialog):

    def __init__(
        self,
        path: str,
        title: str = '',
        selected_ids: set[str] | None = None,
        cache_entries: dict[str, dict[str, object]] | None = None,
        last_run: dict[str, object] | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self.path = path
        self._selected_ids_initial = None if selected_ids is None else set(selected_ids)
        self._selected_ids: set[str] | None = None
        self._selectable_ids: set[str] = set()
        self._cache_entries = dict(cache_entries or {})
        self._cache_entries_by_toc_index = self._build_cache_entries_by_toc_index(self._cache_entries)
        self._toc_item_counter = 0
        self._updating_checks = False
        self._collapsed_paths: set[str] = set()
        self._path_key = self._make_path_key(path)
        self._load_collapsed_state()
        self.setWindowTitle(_('Select chapters') + (f' — {title}' if title else ''))
        self.resize(760, 560)
        try:
            if parent is not None:
                self.setWindowIcon(parent.windowIcon())
        except Exception:
            pass
        try:
            self.setStyleSheet(chapter_summarizer_tab_stylesheet() + ccr_scrollbar_stylesheet() + ccr_combo_stylesheet())
        except Exception:
            pass

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(6)

        top_row = QHBoxLayout()
        self.filter_edit = QLineEdit(self)
        self.filter_edit.setPlaceholderText(_('Filter chapters…'))
        self.filter_edit.textChanged.connect(self.apply_filter)
        try:
            self.filter_edit.setStyleSheet(ccr_combo_stylesheet())
        except Exception:
            pass
        top_row.addWidget(self.filter_edit, 1)

        self.filter_status = QComboBox(self)
        self.filter_status.addItems([_('All'), _('Cached only'), _('Uncached only')])
        self.filter_status.currentIndexChanged.connect(self.apply_filter)
        top_row.addWidget(self.filter_status)

        self.select_all_btn = QPushButton(_('Select all'))
        self.select_all_btn.setShortcut('Ctrl+A')
        self.select_all_btn.clicked.connect(lambda: self._set_all_check_state(Qt.CheckState.Checked))
        self.select_none_btn = QPushButton(_('Select none'))
        self.select_none_btn.clicked.connect(lambda: self._set_all_check_state(Qt.CheckState.Unchecked))
        self.select_uncached_btn = QPushButton(_('Select uncached'))
        self.select_uncached_btn.setToolTip(_('Check only chapters that have no cached summary.'))
        self.select_uncached_btn.setShortcut('Ctrl+U')
        self.select_uncached_btn.clicked.connect(self._select_uncached)
        self.unselect_cached_btn = QPushButton(_('Uncheck cached'))
        self.unselect_cached_btn.setToolTip(_('Uncheck chapters that already have a cached summary.'))
        self.unselect_cached_btn.clicked.connect(self._unselect_cached)
        self.invert_btn = QPushButton(_('Invert'))
        self.invert_btn.setToolTip(_('Invert the current selection.'))
        self.invert_btn.setShortcut('Ctrl+I')
        self.invert_btn.clicked.connect(self._invert_selection)
        top_row.addWidget(self.select_all_btn)
        top_row.addWidget(self.select_none_btn)
        top_row.addWidget(self.select_uncached_btn)
        top_row.addWidget(self.unselect_cached_btn)
        top_row.addWidget(self.invert_btn)
        root.addLayout(top_row)

        self._loading_frames = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏']
        self._loading_index = 0
        self._show_loading_indicator = self._should_show_loading_indicator()

        self.status = QLabel(self)
        self.status.setStyleSheet('color: gray; font-size: 9pt;')
        root.addWidget(self.status)

        if self._show_loading_indicator:
            self._loading_timer = QTimer(self)
            self._loading_timer.timeout.connect(self._update_loading_indicator)
            self._loading_timer.start(100)
            self._update_loading_indicator()
        else:
            self.status.setText(_('Loading…'))

        hint_lines = [_('Cached rows are shown dimmed.')]
        if last_run:
            status = str(last_run.get('status') or '')
            summarized = int(last_run.get('summarized_nodes') or 0)
            total = int(last_run.get('total_nodes') or 0)
            if status == 'running' or (status == 'stopped' and summarized < total):
                hint_lines.append(_('Last run: %d/%d chapters — use “Select uncached” to resume.') % (summarized, total))
        self.cache_hint = QLabel('\n'.join(hint_lines))
        self.cache_hint.setWordWrap(True)
        self.cache_hint.setStyleSheet('color: palette(mid); font-size: 9pt;')
        root.addWidget(self.cache_hint)

        self.tree = TocTreeWidget(self)
        self.tree.setColumnCount(2)
        self.tree.setHeaderLabels([_('Chapter'), _('Cached summary')])
        self.tree.setUniformRowHeights(True)
        try:
            self.tree.setColumnWidth(0, 520)
            self.tree.setColumnWidth(1, 120)
        except Exception:
            pass
        try:
            hdr = self.tree.header()
            hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
            hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
            hdr.setStretchLastSection(False)
            hdr.setMinimumSectionSize(80)
        except Exception:
            pass
        install_ctrl_wheel_zoom(self.tree)
        try:
            self.tree.itemChanged.connect(self._on_item_changed)
        except Exception:
            pass
        root.addWidget(self.tree, 1)

        self.selection_status = QLabel('', self)
        self.selection_status.setStyleSheet('color: palette(mid); font-size: 9pt;')
        root.addWidget(self.selection_status)

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, parent=self)
        bb.accepted.connect(self._on_accept)
        bb.rejected.connect(self.reject)
        try:
            clear_btn = bb.addButton(_('Clear cache for selected'), QDialogButtonBox.ButtonRole.ActionRole)
            if clear_btn is not None:
                clear_btn.setToolTip(_('Delete cached summaries for the currently selected chapters.'))
                clear_btn.clicked.connect(self._clear_cache_for_selected)
        except Exception:
            pass
        root.addWidget(bb)

        self._signals = TocWorkerSignals()
        self._signals.finished.connect(self._on_loaded)
        self._signals.error.connect(self._on_error)
        self._thread = Thread(target=self._load, name='ChapterSummarizerChapterSelectorWorker', daemon=True)
        self._thread.start()

    @property
    def total_selectable(self) -> int:
        return len(self._selectable_ids)

    def selected_ids(self) -> set[str]:
        return set(self._selected_ids or set())

    def _should_show_loading_indicator(self) -> bool:
        try:
            ext = os.path.splitext(self.path or '')[1].lower()
            if ext == '.azw3':
                return True
            if ext in {'.epub', '.kepub'}:
                return os.path.getsize(self.path) / (1024 * 1024) > 5.0
        except Exception:
            pass
        return False

    def _update_loading_indicator(self):
        frame = self._loading_frames[self._loading_index % len(self._loading_frames)]
        self._loading_index += 1
        self.status.setText(f'{frame} {_("Loading chapters…")}')

    def _stop_loading_indicator(self):
        if self._show_loading_indicator:
            try:
                self._loading_timer.stop()
            except Exception:
                pass

    def _load(self):
        try:
            items = extract_toc_tree(self.path)
            self._signals.finished.emit(items)
        except Exception as e:
            self._signals.error.emit(str(e), traceback.format_exc())

    def _make_item_id(self, title: str, dest: str, frag: str) -> str:
        dest = str(dest or '').strip()
        frag = str(frag or '').strip()
        if dest:
            return dest + (('#' + frag) if frag else '')
        m = re.match(r'^\s*(\d+)\s*\.', str(title or ''))
        if m:
            return 'index:' + m.group(1)
        return ''

    def _build_cache_entries_by_toc_index(self, cache_entries: dict[str, dict[str, object]]) -> dict[int, tuple[str, dict[str, object]]]:
        ans: dict[int, tuple[str, dict[str, object]]] = {}
        for chapter_id, entry in cache_entries.items():
            try:
                toc_index = int((entry or {}).get('toc_index') or -1)
            except Exception:
                toc_index = -1
            if toc_index >= 0:
                ans[toc_index] = (str(chapter_id or ''), entry)
        return ans

    def _cache_match(self, item_id: str, toc_index: int) -> tuple[str, dict[str, object]]:
        item_id = str(item_id or '').strip()
        if item_id:
            cache_entry = self._cache_entries.get(item_id)
            if cache_entry:
                return item_id, cache_entry
        return self._cache_entries_by_toc_index.get(int(toc_index), ('', {}))

    def _add_items(self, parent_item, items: list[dict[str, object]]):
        for entry in items:
            title = str((entry or {}).get('title') or '').strip() or _('(Untitled)')
            dest = str((entry or {}).get('dest') or '').strip()
            frag = str((entry or {}).get('frag') or '').strip()
            item_id = self._make_item_id(title, dest, frag)
            self._toc_item_counter += 1
            toc_index = self._toc_item_counter
            cache_key, cache_entry = self._cache_match(item_id, toc_index)
            cached_label = self._cache_label(cache_entry)
            node = QTreeWidgetItem([title, cached_label])
            node.setData(0, Qt.ItemDataRole.UserRole, title.casefold())
            node.setData(0, Qt.ItemDataRole.UserRole + 1, dest)
            node.setData(0, Qt.ItemDataRole.UserRole + 2, frag)
            node.setData(0, Qt.ItemDataRole.UserRole + 3, title)
            node.setData(0, Qt.ItemDataRole.UserRole + 4, item_id)
            node.setData(0, Qt.ItemDataRole.UserRole + 5, toc_index)
            node.setData(0, Qt.ItemDataRole.UserRole + 6, cache_key)
            if cache_entry:
                tooltip = self._cache_tooltip(title, cache_entry)
                node.setToolTip(0, tooltip)
                node.setToolTip(1, tooltip)
                dim = QColor(150, 150, 150)
                node.setForeground(0, dim)
                node.setForeground(1, dim)
            flags = node.flags()
            try:
                flags |= Qt.ItemFlag.ItemIsUserCheckable
                node.setFlags(flags)
            except Exception:
                try:
                    node.setFlags(flags | Qt.ItemIsUserCheckable)
                except Exception:
                    pass

            if parent_item is None:
                self.tree.addTopLevelItem(node)
            else:
                parent_item.addChild(node)

            children = (entry or {}).get('children') or []
            if isinstance(children, list) and children:
                self._add_items(node, children)

            if item_id:
                self._selectable_ids.add(item_id)

            should_check = True
            if self._selected_ids_initial is not None and item_id:
                should_check = item_id in self._selected_ids_initial
            if item_id:
                node.setCheckState(0, Qt.CheckState.Checked if should_check else Qt.CheckState.Unchecked)
            else:
                node.setCheckState(0, Qt.CheckState.Checked)

    def _cache_tooltip(self, title: str, cache_entry: dict[str, object]) -> str:
        lines = [str(title or '').strip()]
        created = str(cache_entry.get('created_at') or '').strip()
        provider = str(cache_entry.get('provider_name') or '').strip()
        model = str(cache_entry.get('model_used') or '').strip()
        requested = str(cache_entry.get('requested_model') or '').strip()
        req_count = int(cache_entry.get('request_count') or 0)
        req_attempts = int(cache_entry.get('request_attempts') or 0)
        duration = float(cache_entry.get('duration_seconds') or 0.0)
        highlight = str(cache_entry.get('highlight') or '').strip()
        preview = str(cache_entry.get('preview') or cache_entry.get('summary') or '').strip()
        if created:
            lines.append(_('Cached: %s') % created)
        if provider:
            lines.append(_('Provider: %s') % provider)
        if model:
            lines.append(_('Model used: %s') % model)
        if requested:
            lines.append(_('Requested model: %s') % requested)
        if req_count or req_attempts:
            lines.append(_('Requests: %d (attempts: %d)') % (req_count, req_attempts))
        if duration > 0:
            lines.append(_('Duration: %.1fs') % duration)
        if highlight:
            lines.append(_('Highlight: %s') % highlight)
        if preview:
            lines.append('')
            lines.append(preview[:360] + ('…' if len(preview) > 360 else ''))
        return '\n'.join(lines)

    def _on_loaded(self, items):
        self._stop_loading_indicator()
        self.tree.clear()
        self._selectable_ids = set()
        self._toc_item_counter = 0
        if not items:
            self.status.setText(_('No chapters found.'))
            self._update_selection_status()
            return
        self._updating_checks = True
        try:
            self._add_items(None, items)
            self._restore_expansion_state()
            self._refresh_parent_check_states()
        finally:
            self._updating_checks = False
        try:
            self.tree.itemExpanded.connect(self._on_item_expanded)
            self.tree.itemCollapsed.connect(self._on_item_collapsed)
        except Exception:
            pass
        try:
            # Guard against occasional header state weirdness leaving column 0 tiny.
            if self.tree.columnWidth(0) < 200:
                self.tree.setColumnWidth(0, max(320, int(self.tree.viewport().width() * 0.72)))
        except Exception:
            pass
        self.status.setText(_('%d item(s)') % self._count_items())
        self.apply_filter()
        self._update_selection_status()

    def _on_item_expanded(self, item: QTreeWidgetItem):
        path = str(item.data(0, Qt.ItemDataRole.UserRole + 4) or '') or str(item.data(0, Qt.ItemDataRole.UserRole + 3) or '')
        self._collapsed_paths.discard(path)

    def _on_item_collapsed(self, item: QTreeWidgetItem):
        path = str(item.data(0, Qt.ItemDataRole.UserRole + 4) or '') or str(item.data(0, Qt.ItemDataRole.UserRole + 3) or '')
        if path:
            self._collapsed_paths.add(path)

    def _count_items(self) -> int:
        count = 0

        def walk(item: QTreeWidgetItem):
            nonlocal count
            count += 1
            for i in range(item.childCount()):
                walk(item.child(i))

        for i in range(self.tree.topLevelItemCount()):
            walk(self.tree.topLevelItem(i))
        return count

    def _on_error(self, msg: str, details: str):
        self._stop_loading_indicator()
        self.status.setText(_('Failed to load chapters.'))
        friendly = msg
        try:
            ext = os.path.splitext(self.path or '')[1].lower()
            if ext not in {'.epub', '.kepub', '.azw3'}:
                friendly = _(
                    'This book format does not support chapter selection.\n\n'
                    'Tip: convert the book to EPUB or AZW3, then try “Select chapters…” again.'
                )
        except Exception:
            pass
        error_dialog(self, _('Chapter selection failed'), friendly, det_msg=details, show=True)

    def apply_filter(self):
        query = (self.filter_edit.text() or '').strip().casefold()
        status_filter = int(self.filter_status.currentIndex())

        def match_item(item: QTreeWidgetItem) -> bool:
            text_key = str(item.data(0, Qt.ItemDataRole.UserRole) or '')
            text_matched = (not query) or (query in text_key)
            item_id = str(item.data(0, Qt.ItemDataRole.UserRole + 4) or '')
            toc_index = int(item.data(0, Qt.ItemDataRole.UserRole + 5) or 0)
            _cache_key, cache_entry = self._cache_match(item_id, toc_index)
            is_cached = bool(cache_entry)
            if status_filter == 1 and not is_cached and item.childCount() == 0:
                status_matched = False
            elif status_filter == 2 and is_cached and item.childCount() == 0:
                status_matched = False
            else:
                status_matched = True
            any_child = False
            for i in range(item.childCount()):
                if match_item(item.child(i)):
                    any_child = True
            visible = (text_matched and status_matched) or any_child
            item.setHidden(not visible)
            return visible

        for i in range(self.tree.topLevelItemCount()):
            match_item(self.tree.topLevelItem(i))

    def _set_all_check_state(self, state: Qt.CheckState):
        self._updating_checks = True
        try:
            for i in range(self.tree.topLevelItemCount()):
                self._set_item_and_children(self.tree.topLevelItem(i), state)
            self._refresh_parent_check_states()
        finally:
            self._updating_checks = False
        self._update_selection_status()

    def _select_uncached(self):
        self._updating_checks = True
        try:
            for i in range(self.tree.topLevelItemCount()):
                self._set_cached_state(self.tree.topLevelItem(i), want_cached=False)
            self._refresh_parent_check_states()
        finally:
            self._updating_checks = False
        self._update_selection_status()

    def _unselect_cached(self):
        self._updating_checks = True
        try:
            for i in range(self.tree.topLevelItemCount()):
                self._set_cached_state(self.tree.topLevelItem(i), want_cached=True)
            self._refresh_parent_check_states()
        finally:
            self._updating_checks = False
        self._update_selection_status()

    def _invert_selection(self):
        self._updating_checks = True
        try:
            for i in range(self.tree.topLevelItemCount()):
                self._invert_item_and_children(self.tree.topLevelItem(i))
            self._refresh_parent_check_states()
        finally:
            self._updating_checks = False
        self._update_selection_status()

    def _set_cached_state(self, item: QTreeWidgetItem, want_cached: bool):
        item_id = str(item.data(0, Qt.ItemDataRole.UserRole + 4) or '')
        toc_index = int(item.data(0, Qt.ItemDataRole.UserRole + 5) or 0)
        _cache_key, cache_entry = self._cache_match(item_id, toc_index)
        is_cached = bool(cache_entry)
        if item_id:
            if want_cached and is_cached:
                item.setCheckState(0, Qt.CheckState.Unchecked)
            elif not want_cached and not is_cached:
                item.setCheckState(0, Qt.CheckState.Checked)
        for i in range(item.childCount()):
            self._set_cached_state(item.child(i), want_cached)

    def _invert_item_and_children(self, item: QTreeWidgetItem):
        item_id = str(item.data(0, Qt.ItemDataRole.UserRole + 4) or '')
        if not item_id and item.childCount() == 0:
            return
        current = item.checkState(0)
        if current == Qt.CheckState.Checked:
            item.setCheckState(0, Qt.CheckState.Unchecked)
        elif current == Qt.CheckState.Unchecked:
            item.setCheckState(0, Qt.CheckState.Checked)
        for i in range(item.childCount()):
            self._invert_item_and_children(item.child(i))

    def _set_item_and_children(self, item: QTreeWidgetItem, state: Qt.CheckState):
        item_id = str(item.data(0, Qt.ItemDataRole.UserRole + 4) or '')
        if not item_id and item.childCount() == 0:
            return
        item.setCheckState(0, state)
        for i in range(item.childCount()):
            self._set_item_and_children(item.child(i), state)

    def _on_item_changed(self, item: QTreeWidgetItem, _column: int = 0):
        if self._updating_checks:
            return
        self._updating_checks = True
        try:
            state = item.checkState(0)
            if state in {Qt.CheckState.Checked, Qt.CheckState.Unchecked}:
                for i in range(item.childCount()):
                    self._set_item_and_children(item.child(i), state)
            self._refresh_parent_check_states(start=item)
        finally:
            self._updating_checks = False
        self._update_selection_status()

    def _refresh_parent_check_states(self, start: QTreeWidgetItem | None = None):
        roots = []
        if start is not None:
            top = start
            try:
                while top.parent() is not None:
                    top = top.parent()
            except Exception:
                pass
            roots = [top]
        else:
            roots = [self.tree.topLevelItem(i) for i in range(self.tree.topLevelItemCount())]

        def refresh(item: QTreeWidgetItem):
            for i in range(item.childCount()):
                refresh(item.child(i))
            if item.childCount() <= 0:
                return
            checked = 0
            partial = 0
            for i in range(item.childCount()):
                st = item.child(i).checkState(0)
                if st == Qt.CheckState.PartiallyChecked:
                    partial += 1
                elif st == Qt.CheckState.Checked:
                    checked += 1
            if partial > 0 or (0 < checked < item.childCount()):
                item.setCheckState(0, Qt.CheckState.PartiallyChecked)
            elif checked == 0:
                item.setCheckState(0, Qt.CheckState.Unchecked)
            else:
                item.setCheckState(0, Qt.CheckState.Checked)

        for r in roots:
            refresh(r)

    def _selected_leaf_ids(self) -> set[str]:
        selected: set[str] = set()

        def walk(item: QTreeWidgetItem) -> bool:
            item_id = str(item.data(0, Qt.ItemDataRole.UserRole + 4) or '')
            has_checked_descendant = False
            for i in range(item.childCount()):
                if walk(item.child(i)):
                    has_checked_descendant = True
            is_checked = (item.checkState(0) == Qt.CheckState.Checked)
            if item_id and is_checked and not has_checked_descendant:
                selected.add(item_id)
            return bool((item_id and is_checked) or has_checked_descendant)

        for i in range(self.tree.topLevelItemCount()):
            walk(self.tree.topLevelItem(i))
        return selected

    def _update_selection_status(self):
        total = len(self._selectable_ids)
        selected = len(self._selected_leaf_ids()) if total else 0
        cached = 0
        for i in range(self.tree.topLevelItemCount()):
            cached += self._count_cached_selectable(self.tree.topLevelItem(i))
        if total:
            self.selection_status.setText(
                _('Selected: %d/%d  ·  Cached: %d  ·  Uncached: %d') % (selected, total, cached, total - cached)
            )
        else:
            self.selection_status.setText('')

    def _count_cached_selectable(self, item: QTreeWidgetItem) -> int:
        total = 0
        item_id = str(item.data(0, Qt.ItemDataRole.UserRole + 4) or '')
        if item_id:
            toc_index = int(item.data(0, Qt.ItemDataRole.UserRole + 5) or 0)
            _cache_key, cache_entry = self._cache_match(item_id, toc_index)
            if cache_entry:
                total += 1
        for i in range(item.childCount()):
            total += self._count_cached_selectable(item.child(i))
        return total

    def _selected_leaf_cache_keys(self) -> list[str]:
        selected: list[str] = []

        def walk(item: QTreeWidgetItem) -> bool:
            item_id = str(item.data(0, Qt.ItemDataRole.UserRole + 4) or '')
            cache_key = str(item.data(0, Qt.ItemDataRole.UserRole + 6) or '')
            has_checked_descendant = False
            for i in range(item.childCount()):
                if walk(item.child(i)):
                    has_checked_descendant = True
            is_checked = (item.checkState(0) == Qt.CheckState.Checked)
            if item_id and is_checked and not has_checked_descendant and cache_key:
                selected.append(cache_key)
            return bool((item_id and is_checked) or has_checked_descendant)

        for i in range(self.tree.topLevelItemCount()):
            walk(self.tree.topLevelItem(i))
        return selected

    def _on_accept(self):
        selected = self._selected_leaf_ids()
        if self._selectable_ids and not selected:
            error_dialog(self, _('No chapters selected'), _('Select at least one chapter to summarize.'), show=True)
            return
        self._selected_ids = selected
        self._save_collapsed_state()
        self.accept()

    def reject(self):
        self._save_collapsed_state()
        super().reject()

    def _make_path_key(self, path: str) -> str:
        try:
            return os.path.normcase(os.path.abspath(path or ''))
        except Exception:
            return str(path or '')

    def _load_collapsed_state(self):
        try:
            data = pref('selector_collapsed_paths', {})
            self._collapsed_paths = set(data.get(self._path_key, []))
        except Exception:
            self._collapsed_paths = set()

    def _save_collapsed_state(self):
        try:
            data = dict(pref('selector_collapsed_paths', {}))
            data[self._path_key] = sorted(self._collapsed_paths)
            set_pref('selector_collapsed_paths', data)
        except Exception:
            pass

    def _restore_expansion_state(self):
        def walk(item: QTreeWidgetItem):
            item_id = str(item.data(0, Qt.ItemDataRole.UserRole + 4) or '')
            path = item_id or str(item.data(0, Qt.ItemDataRole.UserRole + 3) or '')
            if path in self._collapsed_paths:
                self.tree.collapseItem(item)
            else:
                self.tree.expandItem(item)
            for i in range(item.childCount()):
                walk(item.child(i))
        for i in range(self.tree.topLevelItemCount()):
            walk(self.tree.topLevelItem(i))

    def _cache_label(self, cache_entry: dict[str, object]) -> str:
        if not cache_entry:
            return ''
        created = str(cache_entry.get('created_at') or '').strip()
        if not created:
            return _('Cached')
        try:
            dt = datetime.strptime(created, '%Y-%m-%d %H:%M:%S')
            delta = datetime.now() - dt
            if delta.days >= 1:
                return _('Cached · %dd ago') % delta.days
            hours = delta.seconds // 3600
            if hours >= 1:
                return _('Cached · %dh ago') % hours
            minutes = delta.seconds // 60
            if minutes >= 1:
                return _('Cached · %dm ago') % minutes
            return _('Cached · just now')
        except Exception:
            return _('Cached')

    def _clear_cache_for_selected(self):
        from calibre.gui2 import question_dialog
        to_clear = list(dict.fromkeys(self._selected_leaf_cache_keys()))
        if not to_clear:
            info_dialog(self, _('Nothing to clear'), _('None of the selected chapters have a cached summary.'), show=True)
            return
        if not question_dialog(
            self,
            _('Clear cache for selected'),
            _('Delete cached summaries for %d selected chapter(s)?\n\nThis cannot be undone.') % len(to_clear),
            default_yes=False,
        ):
            return
        cleared = 0
        for profile_match in _safe_cache_find_by_source(self.path):
            key = str(profile_match.get('profile_key') or '')
            if not key:
                continue
            cache = ChapterSummaryCache(key)
            try:
                cleared += cache.delete_chapters(to_clear)
            finally:
                _safe_cache_close(cache)
        for cid in to_clear:
            self._cache_entries.pop(cid, None)
        self.tree.clear()
        self._selectable_ids = set()
        self._updating_checks = True
        try:
            self._add_items(None, extract_toc_tree(self.path))
            self._restore_expansion_state()
            self._refresh_parent_check_states()
        finally:
            self._updating_checks = False
        self.apply_filter()
        self._update_selection_status()
        info_dialog(self, _('Cleared'), _('Removed %d cached summary(s).') % cleared, show=True)


def _friendly_ai_error_message(message: str) -> str:
    lowered = (message or '').lower()
    if 'free-models-per-min' in lowered or 'rate limit exceeded' in lowered:
        return _('Rate limit exceeded for the selected free model. Wait a minute and try again, or switch to another model/provider.')
    if 'not configured' in lowered:
        return _('The selected AI provider is not configured yet. Use Configure provider first.')
    if 'getaddrinfo' in lowered or 'name or service not known' in lowered or 'nodename nor servname provided' in lowered or 'temporary failure in name resolution' in lowered:
        return _('Network/DNS error contacting the AI provider. Check your internet connection, DNS, firewall, or VPN and try again.')
    if 'timed out' in lowered or 'timeout' in lowered:
        return _('The AI provider request timed out. Try again, use a smaller chunk size, or use a faster model/provider.')
    return message or _('Unknown AI error')


class SummaryWorker:

    def __init__(self, source_path: str, source_title: str, options: SummaryOptions, selected_chapter_ids: set[str] | None = None, flash_mode: bool = False):
        self.source_path = source_path
        self.source_title = source_title
        self.options = options
        self.selected_chapter_ids = None if selected_chapter_ids is None else set(selected_chapter_ids)
        self.flash_mode = flash_mode
        self.signals = WorkerSignals()
        self._cancel_event = Event()
        self._thread = None

    def start(self):
        self._thread = Thread(target=self.run, name='ChapterSummarizerWorker', daemon=True)
        self._thread.start()

    def cancel(self):
        self._cancel_event.set()

    def is_cancelled(self) -> bool:
        return self._cancel_event.is_set()

    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def run(self):
        try:
            self._log(f'Starting summarization for: {self.source_path}')
            self._log(f'Provider: {self.options.provider_name} | Model: {self.options.requested_model_label or self.options.model_name or "(provider default)"}')
            document = parse_document(self.source_path, self.source_title, selected_chapter_ids=self.selected_chapter_ids)
            self._log(f'Parser detected {len(document.chapters)} chapters/sections.')
            detected_code, detected_name = infer_language_from_text(getattr(document, 'raw_text', '') or '')
            configured_code = str(getattr(self.options, 'source_language_code', '') or '').strip().lower()
            configured_name = str(getattr(self.options, 'source_language_name', '') or '').strip()
            if detected_code and detected_name:
                configured_unknown = (not configured_code and not configured_name) or ('unknown' in configured_name.lower())
                if configured_unknown or (configured_code and configured_code != detected_code):
                    self.options.source_language_code = detected_code
                    self.options.source_language_name = detected_name
                    self._log(f'Source language inferred from text: {detected_name} ({detected_code}).')
            if self.flash_mode:
                result = flash_summarize_document(document, self.options, self._progress, self._log, self._status, self.is_cancelled, self._preview)
            else:
                result = summarize_document(document, self.options, self._progress, self._log, self._status, self.is_cancelled, self._preview)
            self.signals.finished.emit(result)
        except SummaryCancelledError as e:
            self._log(f'Cancelled: {e}')
            self.signals.cancelled.emit(e.result)
        except PartialSummaryError as e:
            self._log(f'Failure after partial result: {e}')
            self.signals.error.emit(_friendly_ai_error_message(str(e)), traceback.format_exc(), e.result)
        except Exception as e:
            self._log(f'Failure: {e}')
            self.signals.error.emit(_friendly_ai_error_message(str(e)), traceback.format_exc(), None)

    def _progress(self, message: str, current: int, total: int):
        self.signals.progress.emit(message, current, total)

    def _log(self, message: str):
        self.signals.log.emit(f'[{_now_stamp()}] {message}')

    def _status(self, message: str):
        self.signals.status.emit(message)

    def _preview(self, text: str):
        self.signals.preview.emit(text)


class ChapterSummarizerCacheManagerDialog(QDialog):
    """Dialog for inspecting and managing the persistent chapter summary cache."""

    def __init__(self, parent=None):
        QDialog.__init__(self, parent)
        self.setWindowTitle(_('Chapter Summary Cache Manager'))
        self.setModal(False)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.resize(900, 520)
        try:
            if parent is not None:
                self.setWindowIcon(parent.windowIcon())
        except Exception:
            pass
        self._build_ui()
        self._refresh()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(8)
        root.setContentsMargins(10, 10, 10, 10)

        # Location row
        loc_row = QHBoxLayout()
        loc_row.setSpacing(6)
        loc_row.addWidget(QLabel(_('Cache folder:')))
        self._path_edit = QLineEdit()
        self._path_edit.setReadOnly(True)
        try:
            self._path_edit.setText(default_cache_path())
        except Exception:
            pass
        loc_row.addWidget(self._path_edit, 1)
        self._reveal_btn = QPushButton(_('Reveal…'))
        self._reveal_btn.setToolTip(_('Open the cache folder in the system file manager.'))
        self._reveal_btn.clicked.connect(self._reveal)
        loc_row.addWidget(self._reveal_btn)
        root.addLayout(loc_row)

        filter_row = QHBoxLayout()
        filter_row.setSpacing(6)
        filter_row.addWidget(QLabel(_('Filter:')))
        self._filter_edit = QLineEdit(self)
        self._filter_edit.setPlaceholderText(_('Filter by title, source path, provider, model, filename, or book ID'))
        try:
            self._filter_edit.setClearButtonEnabled(True)
        except Exception:
            pass
        self._filter_edit.textChanged.connect(self._apply_filter)
        filter_row.addWidget(self._filter_edit, 1)
        root.addLayout(filter_row)

        # Table
        self._table = QTreeWidget(self)
        self._table.setRootIsDecorated(False)
        self._table.setAlternatingRowColors(True)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._table.setColumnCount(7)
        self._table.setHeaderLabels([
            _('Book / source title'),
            _('Provider'),
            _('Model'),
            _('Chapters'),
            _('Size'),
            _('Filename'),
            _('Last updated'),
        ])
        try:
            self._table.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
            self._table.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
            self._table.header().setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
            self._table.header().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
            self._table.header().setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
            self._table.header().setSectionResizeMode(5, QHeaderView.ResizeMode.Interactive)
            self._table.header().setSectionResizeMode(6, QHeaderView.ResizeMode.ResizeToContents)
            self._table.header().resizeSection(1, 120)
            self._table.header().resizeSection(2, 180)
            self._table.header().resizeSection(5, 220)
        except Exception:
            pass
        install_ctrl_wheel_zoom(self._table)
        self._table.itemSelectionChanged.connect(self._on_selection_changed)
        root.addWidget(self._table, 1)

        # Tooltip / preview label
        self._preview_label = QLabel('')
        self._preview_label.setWordWrap(True)
        self._preview_label.setStyleSheet(
            'color: palette(mid); font-size: 8pt; padding: 2px 4px;')
        root.addWidget(self._preview_label)

        # Bottom button bar
        btn_bar = QHBoxLayout()
        btn_bar.setSpacing(6)
        self._size_label = QLabel('')
        self._size_label.setStyleSheet('color: palette(mid); font-size: 8pt;')
        btn_bar.addWidget(self._size_label)
        btn_bar.addStretch(1)
        self._show_in_library_btn = QPushButton(QIcon.ic('lt.png'), _('Show in library'))
        self._show_in_library_btn.setToolTip(_('Select the source book in the calibre library.'))
        self._show_in_library_btn.setEnabled(False)
        self._show_in_library_btn.clicked.connect(self._show_in_library)
        btn_bar.addWidget(self._show_in_library_btn)
        self._load_summary_btn = QPushButton(_('Load Summary'))
        self._load_summary_btn.setToolTip(_('Load the selected cached summary into the Chapter Summarizer dialog.'))
        self._load_summary_btn.setEnabled(False)
        self._load_summary_btn.clicked.connect(self._load_selected_summary)
        btn_bar.addWidget(self._load_summary_btn)
        self._delete_btn = QPushButton(_('Delete selected'))
        self._delete_btn.setToolTip(_('Delete the cache entries for the selected books.'))
        self._delete_btn.setEnabled(False)
        self._delete_btn.clicked.connect(self._delete_selected)
        btn_bar.addWidget(self._delete_btn)
        self._clear_btn = QPushButton(_('Clear all'))
        self._clear_btn.setToolTip(_('Delete every cached summary in the cache folder.'))
        self._clear_btn.setEnabled(False)
        self._clear_btn.clicked.connect(self._clear_all)
        btn_bar.addWidget(self._clear_btn)
        self._close_btn = QPushButton(_('Close'))
        self._close_btn.clicked.connect(self.accept)
        btn_bar.addWidget(self._close_btn)
        root.addLayout(btn_bar)

    # ------------------------------------------------------------------
    # Data helpers
    # ------------------------------------------------------------------

    def _refresh(self):
        self._table.setSortingEnabled(False)
        self._table.clear()
        try:
            entries = _safe_cache_get_list()
        except Exception:
            entries = []
        for entry in entries:
            title = entry.get('title') or _('[Unknown]')
            provider = entry.get('provider') or ''
            model = entry.get('model') or ''
            chapters = str(entry.get('chapter_count') or 0)
            size_kb = entry.get('size_bytes', 0) / 1024.0
            size_str = '%.1f KB' % size_kb if size_kb < 1024 else '%.2f MB' % (size_kb / 1024.0)
            filename = entry.get('filename') or ''
            updated = entry.get('updated_at') or ''
            item = QTreeWidgetItem([title, provider, model, chapters, size_str, filename, updated])
            item.setData(0, Qt.ItemDataRole.UserRole, entry.get('profile_key') or '')
            item.setData(0, Qt.ItemDataRole.UserRole + 1, entry.get('source_path') or '')
            item.setData(0, Qt.ItemDataRole.UserRole + 2, entry.get('file_path') or '')
            item.setData(0, Qt.ItemDataRole.UserRole + 3, entry.get('source_book_id') or '')
            item.setData(0, Qt.ItemDataRole.UserRole + 4, entry.get('title') or '')
            item.setData(0, Qt.ItemDataRole.UserRole + 5, (entry.get('provider') or '', entry.get('model') or '', entry.get('filename') or ''))
            item.setData(0, Qt.ItemDataRole.UserRole + 6, entry.get('source_library_path') or '')
            item.setToolTip(0, entry.get('source_path') or title)
            self._table.addTopLevelItem(item)
        self._table.setSortingEnabled(True)
        self._table.sortByColumn(6, Qt.SortOrder.DescendingOrder)
        total_bytes = _safe_cache_total_size_bytes()
        total_str = '%.1f KB' % (total_bytes / 1024.0) if total_bytes < 1024 * 1024 else '%.2f MB' % (total_bytes / (1024.0 * 1024.0))
        total_entries = self._table.topLevelItemCount()
        self._size_label.setText(_('Total: %s across %d cache entry(ies)') % (total_str, total_entries))
        self._clear_btn.setEnabled(total_entries > 0)
        self._delete_btn.setEnabled(False)
        self._show_in_library_btn.setEnabled(False)
        self._load_summary_btn.setEnabled(False)
        self._preview_label.setText('')
        self._apply_filter()

    def _apply_filter(self):
        query = str(getattr(self, '_filter_edit', None).text() if getattr(self, '_filter_edit', None) is not None else '').strip().casefold()
        visible_count = 0
        for i in range(self._table.topLevelItemCount()):
            item = self._table.topLevelItem(i)
            haystack_parts = [
                item.text(0), item.text(1), item.text(2), item.text(5), item.text(6),
                str(item.data(0, Qt.ItemDataRole.UserRole + 1) or ''),
                str(item.data(0, Qt.ItemDataRole.UserRole + 3) or ''),
            ]
            hidden = bool(query) and query not in ' '.join(str(x or '') for x in haystack_parts).casefold()
            item.setHidden(hidden)
            if not hidden:
                visible_count += 1
        total_bytes = _safe_cache_total_size_bytes()
        total_str = '%.1f KB' % (total_bytes / 1024.0) if total_bytes < 1024 * 1024 else '%.2f MB' % (total_bytes / (1024.0 * 1024.0))
        total_entries = self._table.topLevelItemCount()
        if query:
            self._size_label.setText(_('Showing %d of %d cache entry(ies) — total cache size: %s') % (visible_count, total_entries, total_str))
        else:
            self._size_label.setText(_('Total: %s across %d cache entry(ies)') % (total_str, total_entries))
        self._on_selection_changed()

    def _on_selection_changed(self):
        sel = [item for item in self._table.selectedItems() if not item.isHidden()]
        self._load_summary_btn.setEnabled(len(sel) == 1)
        self._delete_btn.setEnabled(len(sel) > 0)
        self._show_in_library_btn.setEnabled(len(sel) == 1)
        if len(sel) == 1:
            tip = sel[0].toolTip(0) or ''
            if tip:
                self._preview_label.setText(_('Source: ') + tip)
            else:
                self._preview_label.setText('')
        else:
            self._preview_label.setText('')

    def _delete_selected(self):
        sel = self._table.selectedItems()
        if not sel:
            return
        if not question_dialog(
            self,
            _('Delete cache'),
            _('Delete the cached summaries for %d selected book(s)?  This cannot be undone.') % len(sel),
        ):
            return
        for item in sel:
            profile_key = item.data(0, Qt.ItemDataRole.UserRole) or ''
            if profile_key:
                _safe_cache_delete_profile(profile_key)
        self._refresh()

    def _clear_all(self):
        if not question_dialog(
            self,
            _('Clear all cache'),
            _('Delete ALL cached summaries?  This cannot be undone.'),
        ):
            return
        _safe_cache_clear_all()
        self._refresh()

    def _reveal(self):
        """Open the cache folder — or highlight the selected DB file — in the system file manager."""
        # If a single row is selected, reveal that specific file with highlighting
        sel = self._table.selectedItems()
        if len(sel) == 1:
            file_path = str(sel[0].data(0, Qt.ItemDataRole.UserRole + 2) or '').strip()
            if file_path and os.path.isfile(file_path):
                try:
                    import subprocess
                    import sys
                    if sys.platform == 'win32':
                        subprocess.Popen(['explorer', '/select,', file_path])
                    elif sys.platform == 'darwin':
                        subprocess.Popen(['open', '-R', file_path])
                    else:
                        subprocess.Popen(['xdg-open', os.path.dirname(file_path)])
                except Exception:
                    pass
                return
        # Fallback: open the cache folder
        path = default_cache_path()
        try:
            import subprocess
            import sys
            if sys.platform == 'win32':
                subprocess.Popen(['explorer', path])
            elif sys.platform == 'darwin':
                subprocess.Popen(['open', path])
            else:
                subprocess.Popen(['xdg-open', path])
        except Exception:
            try:
                from qt.core import QDesktopServices, QUrl
                QDesktopServices.openUrl(QUrl.fromLocalFile(path))
            except Exception:
                pass

    def _show_in_library(self):
        """Select the source book in the calibre library view (FTS+ pattern)."""
        sel = self._table.selectedItems()
        if len(sel) != 1:
            return
        source_path = str(sel[0].data(0, Qt.ItemDataRole.UserRole + 1) or '').strip()
        source_book_id_raw = str(sel[0].data(0, Qt.ItemDataRole.UserRole + 3) or '').strip()
        source_title = str(sel[0].data(0, Qt.ItemDataRole.UserRole + 4) or '').strip()
        source_library_path = str(sel[0].data(0, Qt.ItemDataRole.UserRole + 6) or '').strip()
        if not source_path:
            error_dialog(self, _('Show in library'), _('No source path recorded for this cache entry.'), show=True)
            return

        # Walk through the parent chain to find the ChapterSummarizerDialog → gui
        dialog_parent = None
        parent = self.parent()
        while parent is not None:
            if hasattr(parent, 'gui') and hasattr(parent, '_switch_library_and_queue_action'):
                dialog_parent = parent
                break
            parent = getattr(parent, 'parent', lambda: None)()
        if dialog_parent is None:
            error_dialog(self, _('Show in library'), _('Could not find the Chapter Summarizer dialog.'), show=True)
            return

        gui = dialog_parent.gui

        # Check if we need to switch libraries
        try:
            current_library_path = str(getattr(gui.current_db, 'library_path', '') or '').strip()
        except Exception:
            current_library_path = ''

        source_library_path = _canonical_library_path(source_library_path)
        if not source_library_path and source_path:
            derived_library_path = _canonical_library_path(_derive_library_path_from_source_path(source_path))
            if derived_library_path:
                source_library_path = derived_library_path
                _cs_debug_log(f'Cache Manager show_in_library: derived target library from source path target_library={source_library_path!r}')
        current_library_path = _canonical_library_path(current_library_path)
        library_mismatch = bool(source_library_path and current_library_path and not _same_library_path(source_library_path, current_library_path))
        _cs_debug_log(
            'Cache Manager show_in_library: '
            f'current_library={current_library_path!r} target_library={source_library_path!r} '
            f'library_mismatch={library_mismatch!r} raw_source_book_id={source_book_id_raw!r} '
            f'source_path={source_path!r} source_title={source_title!r}'
        )

        if library_mismatch:
            success = dialog_parent._switch_library_and_queue_action(
                source_library_path,
                action='locate',
                book_id=None,
                source_book_id_raw=source_book_id_raw,
                source_path=source_path,
                source_title=source_title,
            )
            if not success:
                error_dialog(
                    self,
                    _('Show in library'),
                    _('Could not switch to the library:\n%s') % source_library_path,
                    show=True,
                )
            return

        db = getattr(gui.current_db, 'new_api', gui.current_db)
        book_id = _resolve_cached_book_id(source_path, source_title, source_book_id_raw, db=db)
        if book_id is None:
            error_dialog(
                self, _('Show in library'),
                _('Could not find the source book in the current calibre library.\n\nSource: %s') % source_path,
                show=True,
            )
            return

        def _select_visible_book() -> bool:
            try:
                if hasattr(dialog_parent, '_select_library_book') and callable(dialog_parent._select_library_book):
                    if dialog_parent._select_library_book(book_id, preserve_selection=False):
                        try:
                            gui.raise_()
                            gui.activateWindow()
                        except Exception:
                            pass
                        return True
            except Exception:
                pass
            try:
                gui.show_library_view()
            except Exception:
                pass
            try:
                gui.library_view.select_rows([book_id], using_ids=True)
                try:
                    selected_ids = [int(x) for x in list(gui.library_view.get_selected_ids())]
                except Exception:
                    selected_ids = []
                if selected_ids and book_id not in selected_ids:
                    return False
                try:
                    gui.raise_()
                    gui.activateWindow()
                except Exception:
                    pass
                return True
            except Exception:
                pass
            return False

        if _select_visible_book():
            return

        proceed = question_dialog(
            self,
            _('Show in library'),
            _('The book was found (ID %d) but is not currently visible because a search or Virtual library filter is active.\n\nDo you want Chapter Summarizer to clear the current search and visible library restrictions, then try again?') % book_id,
        )
        if not proceed:
            return
        try:
            if hasattr(gui, 'search') and gui.search is not None:
                try:
                    gui.search.clear()
                except Exception:
                    try:
                        gui.search.setText('')
                    except Exception:
                        pass
            try:
                if hasattr(gui, 'apply_named_search_restriction'):
                    gui.apply_named_search_restriction('')
            except Exception:
                pass
            try:
                if hasattr(gui, 'apply_virtual_library'):
                    gui.apply_virtual_library()
            except Exception:
                pass
            try:
                QApplication.processEvents()
            except Exception:
                pass
            if _select_visible_book():
                return
            error_dialog(
                self, _('Show in library'),
                _('The book is still not visible after clearing the current search and library restrictions.'),
                show=True,
            )
        except Exception as e:
            error_dialog(self, _('Show in library'), str(e), show=True)

    def _load_selected_summary(self):
        sel = [item for item in self._table.selectedItems() if not item.isHidden()]
        if len(sel) != 1:
            return
        item = sel[0]
        profile_key = str(item.data(0, Qt.ItemDataRole.UserRole) or '').strip()
        source_path = str(item.data(0, Qt.ItemDataRole.UserRole + 1) or '').strip()
        source_book_id_str = str(item.data(0, Qt.ItemDataRole.UserRole + 3) or '').strip()
        source_title = str(item.data(0, Qt.ItemDataRole.UserRole + 4) or '').strip()
        source_library_path = str(item.data(0, Qt.ItemDataRole.UserRole + 6) or '').strip()

        if not profile_key:
            error_dialog(self, _('Load summary'), _('This cache entry has no summary key and cannot be loaded.'), show=True)
            return

        parent = self.parent()
        if not hasattr(parent, 'gui'):
            error_dialog(self, _('Load summary'), _('Could not find the Chapter Summarizer dialog to receive the cached summary.'), show=True)
            return

        gui = parent.gui

        # Check if we need to switch libraries
        try:
            current_library_path = str(getattr(gui.current_db, 'library_path', '') or '').strip()
        except Exception:
            current_library_path = ''

        source_library_path = _canonical_library_path(source_library_path)
        if not source_library_path and source_path:
            derived_library_path = _canonical_library_path(_derive_library_path_from_source_path(source_path))
            if derived_library_path:
                source_library_path = derived_library_path
                _cs_debug_log(f'Cache Manager load_summary: derived target library from source path target_library={source_library_path!r}')
        current_library_path = _canonical_library_path(current_library_path)
        library_mismatch = bool(source_library_path and current_library_path and not _same_library_path(source_library_path, current_library_path))
        _cs_debug_log(
            'Cache Manager load_summary: '
            f'current_library={current_library_path!r} target_library={source_library_path!r} '
            f'library_mismatch={library_mismatch!r} raw_source_book_id={source_book_id_str!r} '
            f'profile_key={profile_key!r} source_path={source_path!r} source_title={source_title!r}'
        )

        if library_mismatch:
            # Queue the library switch and load action
            switcher = getattr(parent, '_switch_library_and_queue_action', None)
            if not callable(switcher):
                error_dialog(self, _('Load summary'), _('Library switching not supported.'), show=True)
                return

            success = switcher(
                source_library_path,
                action='load_summary',
                book_id=None,
                source_book_id_raw=source_book_id_str,
                profile_key=profile_key,
                source_path=source_path,
                source_title=source_title,
            )

            if success:
                self._preview_label.setText(_('Switching library and loading summary...'))
                try:
                    parent.show()
                    parent.raise_()
                    parent.activateWindow()
                except Exception:
                    pass
            return

        # Same library - load directly
        loader = getattr(parent, 'load_summary_from_cache_profile', None)
        if not callable(loader):
            error_dialog(self, _('Load summary'), _('Could not find the Chapter Summarizer dialog to receive the cached summary.'), show=True)
            return

        source_book_id = _resolve_cached_book_id(source_path, source_title, source_book_id_str, db=getattr(gui, 'current_db', None))
        if source_book_id is not None:
            try:
                if hasattr(parent, '_select_library_book') and callable(parent._select_library_book):
                    parent._select_library_book(source_book_id, preserve_selection=False)
                else:
                    gui.library_view.select_rows([source_book_id], using_ids=True)
                    parent.refresh_selected_book()
            except Exception:
                pass

        loaded = False
        try:
            loaded = bool(loader(profile_key, source_path=source_path, source_title=source_title))
        except Exception as e:
            error_dialog(self, _('Load summary'), _('Failed to load the selected cached summary.'), det_msg=str(e), show=True)
            return
        if not loaded:
            error_dialog(self, _('Load summary'), _('The selected cache entry does not contain a loadable summary yet.'), show=True)
            return
        self._preview_label.setText(_('Loaded selected summary into Chapter Summarizer.'))
        try:
            parent.show()
            parent.raise_()
            parent.activateWindow()
        except Exception:
            pass


class ChapterSummarizerHistoryDialog(QDialog):
    """Dialog showing a per-source history of plugin runs."""

    def __init__(self, source_path: str, source_title: str = '', source_book_id: str = '', parent=None):
        super().__init__(parent)
        self._source_path = str(source_path or '').strip()
        self._source_title = str(source_title or '').strip()
        self._source_book_id = str(source_book_id or '').strip()
        self.setWindowTitle(_('History') + (f' — {self._source_title}' if self._source_title else ''))
        self.setModal(True)
        self.resize(980, 560)
        try:
            if parent is not None:
                self.setWindowIcon(parent.windowIcon())
        except Exception:
            pass
        self._build_ui()
        self._refresh()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(8)
        root.setContentsMargins(10, 10, 10, 10)

        hint = QLabel('', self)
        hint.setWordWrap(True)
        hint.setStyleSheet('color: palette(mid); font-size: 9pt;')
        src = self._source_path or self._source_title or ''
        hint.setText(_('Runs stored for: %s') % (src or _('Unknown')))
        root.addWidget(hint)

        self._table = QTreeWidget(self)
        self._table.setRootIsDecorated(False)
        self._table.setAlternatingRowColors(True)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._table.setColumnCount(9)
        self._table.setHeaderLabels([
            _('Started'),
            _('Status'),
            _('Provider'),
            _('Model'),
            _('Output'),
            _('Selected'),
            _('Summarized'),
            _('Duration'),
            _('Notes'),
        ])
        try:
            hdr = self._table.header()
            hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
            hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
            hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
            hdr.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
            hdr.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
            hdr.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
            hdr.setSectionResizeMode(6, QHeaderView.ResizeMode.ResizeToContents)
            hdr.setSectionResizeMode(7, QHeaderView.ResizeMode.ResizeToContents)
            hdr.setSectionResizeMode(8, QHeaderView.ResizeMode.Stretch)
        except Exception:
            pass
        install_ctrl_wheel_zoom(self._table)
        root.addWidget(self._table, 1)

        btn_bar = QHBoxLayout()
        btn_bar.setSpacing(6)
        self._count_label = QLabel('', self)
        self._count_label.setStyleSheet('color: palette(mid); font-size: 8pt;')
        btn_bar.addWidget(self._count_label)
        btn_bar.addStretch(1)

        self._refresh_btn = QPushButton(_('Refresh'))
        self._refresh_btn.clicked.connect(self._refresh)
        btn_bar.addWidget(self._refresh_btn)

        self._copy_btn = QPushButton(_('Copy'))
        self._copy_btn.setToolTip(_('Copy the selected rows to the clipboard.'))
        self._copy_btn.clicked.connect(self._copy)
        btn_bar.addWidget(self._copy_btn)

        self._clear_btn = QPushButton(_('Clear history'))
        self._clear_btn.setToolTip(_('Delete all stored history rows for this title/source.'))
        self._clear_btn.clicked.connect(self._clear)
        btn_bar.addWidget(self._clear_btn)

        self._close_btn = QPushButton(_('Close'))
        self._close_btn.clicked.connect(self.accept)
        btn_bar.addWidget(self._close_btn)
        root.addLayout(btn_bar)

    def _fetch(self) -> list[dict]:
        return _safe_history_list_for_source(source_path=self._source_path, source_book_id=self._source_book_id, limit=500)

    def _refresh(self):
        self._table.setSortingEnabled(False)
        self._table.clear()
        try:
            rows = self._fetch()
        except Exception:
            rows = []
        for row in rows:
            started = str(row.get('started_at') or '').strip()
            status_raw = str(row.get('status') or '').strip()
            status_map = {
                'success': _('Success'),
                'cancelled': _('Cancelled'),
                'error': _('Error'),
                'running': _('Running'),
            }
            status = status_map.get(status_raw.lower(), status_raw)
            provider = str(row.get('provider_name') or '').strip()
            model = str(row.get('requested_model') or row.get('effective_model_label') or '').strip()
            out_lang_code = str(row.get('target_language') or '').strip()
            out_lang = display_language_name(out_lang_code) if out_lang_code else ''
            selected = row.get('selected_nodes', None)
            total = row.get('total_nodes', None)
            if selected is None and total is None:
                selected_txt = ''
            elif total not in (None, '', 0, '0'):
                try:
                    selected_txt = f"{int(selected)}/{int(total)}"
                except Exception:
                    selected_txt = str(selected or '')
            else:
                selected_txt = str(selected if selected is not None else '')
            summarized = row.get('summarized_nodes', None)
            summarized_txt = '' if summarized is None else str(summarized)
            duration = row.get('duration_seconds', None)
            duration_txt = ''
            try:
                if duration is not None:
                    duration_txt = '%.1fs' % float(duration)
            except Exception:
                duration_txt = ''
            note = str(row.get('error_message') or '').strip()
            item = QTreeWidgetItem([
                started,
                status,
                provider,
                model,
                out_lang,
                selected_txt,
                summarized_txt,
                duration_txt,
                note,
            ])
            item.setData(0, Qt.ItemDataRole.UserRole, str(row.get('id') or ''))
            self._table.addTopLevelItem(item)
        self._table.setSortingEnabled(True)
        try:
            self._table.sortByColumn(0, Qt.SortOrder.DescendingOrder)
        except Exception:
            pass
        self._count_label.setText(_('%d run(s)') % self._table.topLevelItemCount())

    def _copy(self):
        sel = self._table.selectedItems() or []
        if not sel:
            return
        lines = []
        for item in sel:
            parts = [item.text(i) for i in range(self._table.columnCount())]
            lines.append('\t'.join(parts))
        text = '\n'.join(lines)
        try:
            QApplication.clipboard().setText(text)
        except Exception:
            pass

    def _clear(self):
        if not question_dialog(self, _('Clear history'), _('Delete all stored history for this title/source?  This cannot be undone.')):
            return
        try:
            _safe_history_clear_for_source(source_path=self._source_path, source_book_id=self._source_book_id)
        except Exception:
            pass
        self._refresh()


class ChapterSummarizerDialog(Dialog):
    must_restart_before_config = False

    def __init__(self, gui, icon=None):
        _perf_started = time.monotonic()
        self.gui = gui
        self.db = getattr(gui.current_db, 'new_api', gui.current_db)
        self.icon = icon
        self._worker = None
        self._last_result = None
        self._last_model_used = ''
        self._last_provider_used = ''
        self._last_book_id = None
        self._last_source_path = ''
        self._last_source_title = ''
        self._last_cache_profile_key = ''
        self._last_cache_source_hash = ''
        self._last_cache_requested_model = ''
        self._last_cache_provider = ''
        self._source_cache_matches_path = ''
        self._source_cache_matches: list[dict] | None = None
        self._lazy_cache_population_token = 0
        self._building = False
        self._session_ai_log: list[str] = []
        self._external_metadata_cache: dict[str, dict] = {}
        self._cover_native_size = None
        self._cover_bytes_len = 0
        self._toc_nodes_count = None
        self._toc_nodes_path = ''
        self._toc_nodes_pending_path = ''
        self._toc_nodes_token = 0
        self._chapter_selection_ids: set[str] | None = None
        self._chapter_selection_source_path = ''
        self._chapter_selection_total = 0
        self._history_run_id = 0
        self._history_run_started_monotonic = None
        self._palette_signal_connected = False
        self._main_splitter = None
        self._settings_scroll = None
        self._bottom_host = None
        self._splitter_user_adjusted = False
        self._applying_splitter_sizes = False
        self.summary_style_buttons = {}
        self._about_link_specs: list[tuple[QLabel, str, str, str, int]] = []
        self._multi_queue: list[dict] = []
        self._multi_total = 0
        self._multi_current = 0
        self._multi_results: list[object] = []
        self._multi_batch_cancelled = False
        self._pending_library_action = None
        # parent=None → true top-level window with its own taskbar entry (CCR pattern)
        _dialog_init_started = time.monotonic()
        Dialog.__init__(self, _('Chapter Summarizer'), 'chapter_summarizer_dialog', parent=None)
        _cs_perf_log('dialog base init finished in %.1f ms' % _elapsed_ms(_dialog_init_started))
        # Set modeless + window hints AFTER Dialog.__init__ so they are not overridden
        try:
            self.setWindowModality(Qt.WindowModality.NonModal)
        except Exception:
            try:
                self.setWindowModality(Qt.NonModal)
            except Exception:
                pass
        try:
            flags = self.windowFlags()
            try:
                flags |= Qt.WindowType.Window
                flags |= Qt.WindowType.WindowMinimizeButtonHint
                flags |= Qt.WindowType.WindowMaximizeButtonHint
                flags |= Qt.WindowType.WindowSystemMenuHint
            except Exception:
                flags |= Qt.Window
                flags |= Qt.WindowMinimizeButtonHint
                flags |= Qt.WindowMaximizeButtonHint
                flags |= Qt.WindowSystemMenuHint
            self.setWindowFlags(flags)
        except Exception:
            pass
        try:
            if icon is not None:
                self.setWindowIcon(icon)
        except Exception:
            pass
        _refresh_started = time.monotonic()
        self.refresh_selected_book()
        _cs_perf_log('initial selected-book refresh finished in %.1f ms; dialog init total %.1f ms' % (_elapsed_ms(_refresh_started), _elapsed_ms(_perf_started)))

    def sizeHint(self):
        return QSize(1040, 730)

    def setup_ui(self):
        self.resize(1040, 730)
        try:
            # Increased minimum width to prevent clipping with the two-column top section
            self.setMinimumWidth(860)
        except Exception:
            pass
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        settings_host = QWidget(self)
        settings_layout = QVBoxLayout(settings_host)
        settings_layout.setContentsMargins(0, 0, 0, 0)
        settings_layout.setSpacing(6)

        top_row = QGridLayout()
        top_row.setContentsMargins(0, 0, 0, 0)
        top_row.setHorizontalSpacing(8)
        top_row.setVerticalSpacing(4)
        top_row.setColumnStretch(0, 1)
        top_row.setColumnStretch(1, 1)

        source_box = QWidget(self)
        try:
            source_box.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        except Exception:
            pass
        source_layout = QGridLayout(source_box)
        source_layout.setContentsMargins(0, 0, 0, 0)
        source_layout.setColumnStretch(0, 1)
        source_layout.setColumnStretch(1, 0)
        source_layout.setColumnStretch(2, 0)
        source_layout.setHorizontalSpacing(6)
        source_layout.setVerticalSpacing(4)
        self.selected_book_label = QLabel('')
        self.selected_book_label.setWordWrap(True)
        try:
            self.selected_book_label.setTextFormat(Qt.TextFormat.PlainText)
        except Exception:
            pass
        try:
            self.selected_book_label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        except Exception:
            pass
        try:
            self.selected_book_label.setMaximumHeight(44)
        except Exception:
            pass
        self.selected_meta_label = QLabel('')
        self.selected_meta_label.setWordWrap(True)
        self.selected_path_label = QLabel('')
        self.selected_path_label.setWordWrap(True)
        try:
            self.selected_path_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        except Exception:
            pass
        self.cover_label = QLabel('')
        self.cover_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.cover_label.setMinimumSize(72, 102)
        self.cover_label.setMaximumSize(87, 124)
        self.cover_label.setStyleSheet('background-color: palette(base); border: 1px solid palette(mid); border-radius: 5px; padding: 2px;')
        self.cover_label.setText(_('No cover'))
        self.cover_panel = QFrame(self)
        self.cover_panel.setObjectName('coverPanel')
        cover_panel_layout = QHBoxLayout(self.cover_panel)
        cover_panel_layout.setContentsMargins(4, 2, 4, 2)
        cover_panel_layout.setSpacing(2)
        try:
            self.cover_panel.setMinimumWidth(480)
            self.cover_panel.setMaximumWidth(540)
            self.cover_panel.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        except Exception:
            pass
        cover_panel_layout.addWidget(self.cover_label, 0, Qt.AlignmentFlag.AlignTop)
        self.cover_meta_label = QLabel('', self.cover_panel)
        self.cover_meta_label.setObjectName('coverMeta')
        self.cover_meta_label.setWordWrap(True)
        self.cover_meta_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        try:
            self.cover_meta_label.setMinimumWidth(280)
            self.cover_meta_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        except Exception:
            pass
        try:
            self.cover_meta_label.setMaximumHeight(120)
        except Exception:
            pass
        cover_panel_layout.addWidget(self.cover_meta_label, 1, Qt.AlignmentFlag.AlignTop)
        self.callout_tools = QWidget(self.cover_panel)
        try:
            self.callout_tools.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        except Exception:
            pass
        callout_tools_layout = QVBoxLayout(self.callout_tools)
        callout_tools_layout.setContentsMargins(0, 0, 0, 0)
        callout_tools_layout.setSpacing(6)

        self.book_navigation_panel = QWidget(self)
        book_navigation_layout = QHBoxLayout(self.book_navigation_panel)
        book_navigation_layout.setContentsMargins(0, 2, 0, 2)
        book_navigation_layout.setSpacing(6)
        book_navigation_layout.addStretch(1)
        self.first_book_button = QToolButton(self.book_navigation_panel)
        self.first_book_button.clicked.connect(self.show_first_book)
        self.first_book_button.setEnabled(False)
        self.first_book_button.setToolTip(_('Jump to the first compatible book in the current library view order.'))
        try:
            _icon = _load_bundled_icon('images/arrow-double-left.png')
            if _icon is not None:
                self.first_book_button.setIcon(_icon)
        except Exception:
            pass
        self.prev_book_button = QToolButton(self.book_navigation_panel)
        self.prev_book_button.clicked.connect(self.show_previous_book)
        self.prev_book_button.setEnabled(False)
        self.prev_book_button.setToolTip(_('Go to the previous compatible book in the current library view order.'))
        try:
            _icon = _load_bundled_icon('images/arrow-previous.png')
            if _icon is not None:
                self.prev_book_button.setIcon(_icon)
        except Exception:
            pass
        self.next_book_button = QToolButton(self.book_navigation_panel)
        self.next_book_button.clicked.connect(self.show_next_book)
        self.next_book_button.setEnabled(False)
        self.next_book_button.setToolTip(_('Go to the next compatible book in the current library view order.'))
        try:
            _icon = _load_bundled_icon('images/arrow-next.png')
            if _icon is not None:
                self.next_book_button.setIcon(_icon)
        except Exception:
            pass
        self.last_book_button = QToolButton(self.book_navigation_panel)
        self.last_book_button.clicked.connect(self.show_last_book)
        self.last_book_button.setEnabled(False)
        self.last_book_button.setToolTip(_('Jump to the last compatible book in the current library view order.'))
        try:
            _icon = _load_bundled_icon('images/arrow-double-right.png')
            if _icon is not None:
                self.last_book_button.setIcon(_icon)
        except Exception:
            pass
        for b in (self.first_book_button, self.prev_book_button, self.next_book_button, self.last_book_button):
            try:
                b.setAutoRaise(True)
            except Exception:
                pass
            try:
                b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
            except Exception:
                try:
                    b.setToolButtonStyle(Qt.ToolButtonIconOnly)
                except Exception:
                    pass
            try:
                b.setMinimumSize(30, 30)
            except Exception:
                pass
            try:
                b.setIconSize(QPixmap(20, 20).size())
            except Exception:
                pass
            book_navigation_layout.addWidget(b)
        book_navigation_layout.addStretch(1)
        callout_tools_layout.addStretch(1)

        self.refresh_selection_button = QToolButton(self.callout_tools)
        self.refresh_selection_button.setToolTip(_('Refresh selection'))
        try:
            self.refresh_selection_button.setIcon(QIcon.ic('view-refresh.png'))
        except Exception:
            try:
                self.refresh_selection_button.setIcon(QIcon.ic('refresh.png'))
            except Exception:
                pass
        try:
            self.refresh_selection_button.setAutoRaise(True)
        except Exception:
            pass
        try:
            self.refresh_selection_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        except Exception:
            try:
                self.refresh_selection_button.setToolButtonStyle(Qt.ToolButtonIconOnly)
            except Exception:
                pass
        try:
            self.refresh_selection_button.setMinimumSize(30, 30)
        except Exception:
            pass
        try:
            self.refresh_selection_button.setIconSize(QPixmap(18, 18).size())
        except Exception:
            pass
        self.refresh_selection_button.clicked.connect(self.refresh_library_selection)
        self.refresh_selection_button.setToolTip(_('Clear the external-file override, if active, and refresh the selected calibre book.'))
        callout_tools_layout.addWidget(self.refresh_selection_button, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignBottom)
        tools_column = QWidget(self.cover_panel)
        tools_column_layout = QVBoxLayout(tools_column)
        tools_column_layout.setContentsMargins(0, 0, 0, 0)
        tools_column_layout.setSpacing(4)
        tools_column_layout.addWidget(self.callout_tools, 0, Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignHCenter)
        cover_panel_layout.addWidget(tools_column, 0, Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignRight)

        # Keyboard navigation (scoped to this dialog)
        try:
            sc_prev = QShortcut(self)
            sc_prev.setKey(Qt.ALT | Qt.Key_Left)
            sc_prev.activated.connect(self.show_previous_book)
            sc_next = QShortcut(self)
            sc_next.setKey(Qt.ALT | Qt.Key_Right)
            sc_next.activated.connect(self.show_next_book)
            sc_first = QShortcut(self)
            sc_first.setKey(Qt.ALT | Qt.Key_Up)
            sc_first.activated.connect(self.show_first_book)
            sc_last = QShortcut(self)
            sc_last.setKey(Qt.ALT | Qt.Key_Down)
            sc_last.activated.connect(self.show_last_book)
            self._nav_shortcuts = (sc_prev, sc_next, sc_first, sc_last)
        except Exception:
            self._nav_shortcuts = ()
        self.selected_book_panel = QWidget(self)
        selected_book_panel_layout = QVBoxLayout(self.selected_book_panel)
        selected_book_panel_layout.setContentsMargins(0, 0, 0, 0)
        selected_book_panel_layout.setSpacing(2)
        selected_book_panel_layout.addWidget(self.selected_book_label)
        cover_row = QWidget(self)
        cover_row_layout = QHBoxLayout(cover_row)
        cover_row_layout.setContentsMargins(0, 0, 0, 0)
        cover_row_layout.setSpacing(2)
        cover_row_layout.addWidget(self.cover_panel, 0)
        cover_row_layout.addStretch(1)
        selected_book_panel_layout.addWidget(cover_row)
        legacy_external = str(pref('external_file_path', '') or '').strip()
        self.external_path = QLineEdit('')
        self.external_path.setPlaceholderText(_('Optional external file for testing or ad-hoc summaries'))
        if legacy_external:
            set_pref('external_file_path', '')
        try:
            self.external_path.setClearButtonEnabled(True)
        except Exception:
            pass
        try:
            self.external_path.setMinimumWidth(320)
        except Exception:
            pass
        try:
            self.external_path.setCursorPosition(0)
        except Exception:
            pass
        self.external_path.textChanged.connect(self._on_external_path_changed)
        browse = QPushButton(_('Browse…'))
        browse.clicked.connect(self.choose_external_file)
        browse.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        try:
            browse.setMaximumWidth(110)
        except Exception:
            pass
        try:
            browse.setIcon(QIcon.ic('document_open.png'))
        except Exception:
            pass
        clear_external = QPushButton(_('Clear'))
        clear_external.clicked.connect(self.clear_external_file)
        clear_external.setToolTip(_('Clear the external-file override and go back to the selected calibre book.'))
        try:
            clear_external.setIcon(QIcon.ic('clear_left.png'))
        except Exception:
            pass
        try:
            f = clear_external.font()
            f.setPointSize(8)
            clear_external.setFont(f)
        except Exception:
            pass
        try:
            clear_external.setStyleSheet('QPushButton { padding: 2px 8px; }')
        except Exception:
            pass
        try:
            clear_external.setMaximumWidth(70)
        except Exception:
            pass
        try:
            self.external_path.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        except Exception:
            pass
        self.run_button = QPushButton(_('Summarize now'))
        self.run_button.setObjectName('primaryAction')
        self.run_button.clicked.connect(self._on_primary_action_button_clicked)
        try:
            _run_icon = self.icon if isinstance(self.icon, QIcon) and not self.icon.isNull() else _plugin_theme_icon()
            if _run_icon is not None:
                self.run_button.setIcon(_run_icon)
        except Exception:
            pass
        self.view_button = QPushButton(_('View'))
        self.view_button.clicked.connect(self.view_selected_book)
        self.view_button.setEnabled(False)
        self.view_button.setToolTip(_('Open the selected library book in calibre viewer.'))
        try:
            self.view_button.setIcon(QIcon.ic('viewer.png'))
        except Exception:
            try:
                self.view_button.setIcon(QIcon.ic('view.png'))
            except Exception:
                pass
        self.toc_fast_btn = QPushButton(_('Select chapters…'))
        self.toc_fast_btn.clicked.connect(self.select_chapters)
        self.toc_fast_btn.setToolTip(_('Choose which chapters/sections will be summarized. Uncheck items like References, Notes, Acknowledgements, etc.'))
        try:
            toc_icon = QIcon.ic('toc.png')
            if not toc_icon.isNull():
                self.toc_fast_btn.setIcon(toc_icon)
        except Exception:
            pass
        self.source_summary_label = QLabel('')
        self.source_summary_label.setWordWrap(True)
        self.source_summary_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.source_summary_label.setTextFormat(Qt.TextFormat.RichText)
        self.source_summary_label.setStyleSheet('padding: 4px 8px; border: 1px solid palette(mid); border-radius: 4px; background-color: palette(base); color: palette(text); font-size: 9pt;')
        source_layout.addWidget(self.selected_book_panel, 0, 0, 1, 2)
        browse_row = QWidget(self)
        browse_row_layout = QHBoxLayout(browse_row)
        browse_row_layout.setContentsMargins(0, 0, 0, 0)
        browse_row_layout.setSpacing(6)
        browse_row_layout.addWidget(browse, 0)
        browse_row_layout.addWidget(self.external_path, 1)
        browse_row_layout.addWidget(clear_external, 0)
        source_layout.addWidget(browse_row, 1, 0, 1, 2, Qt.AlignmentFlag.AlignLeft)
        # Ensure extra height goes to the bottom row, avoiding large gaps between the rows above.
        actions_row = QWidget(self)
        actions_layout = QHBoxLayout(actions_row)
        actions_layout.setContentsMargins(0, 0, 0, 0)
        actions_layout.setSpacing(6)
        actions_layout.addWidget(self.view_button)
        actions_layout.addWidget(self.toc_fast_btn)
        self.flash_summary_check = QCheckBox(_('Flash summary'))
        self.flash_summary_check.setToolTip(_('Generate a quick overview summary instead of chapter-by-chapter. Faster but less detailed.'))
        self.flash_summary_check.setChecked(pref_bool('flash_summary_mode', False))
        actions_layout.addWidget(self.flash_summary_check)
        actions_layout.addWidget(self.run_button)
        self.chapter_selection_badge = QLabel('', self)
        self.chapter_selection_badge.setStyleSheet(
            'color: palette(link); '
            'font-size: 9pt; '
            'font-weight: 600; '
            'padding-left: 6px; '
            'text-decoration: underline;'
        )
        actions_layout.addWidget(self.chapter_selection_badge)
        actions_layout.addStretch(0)
        source_layout.addWidget(actions_row, 3, 0, 1, 2, Qt.AlignmentFlag.AlignTop)
        top_row.addWidget(source_box, 0, 0, 1, 1, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)

        options_box = QWidget(self)
        try:
            options_box.setMinimumWidth(380)
        except Exception:
            pass
        form = QFormLayout(options_box)
        form.setContentsMargins(0, 0, 0, 0)
        form.setVerticalSpacing(6)
        try:
            form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        except Exception:
            pass
        try:
            form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        except Exception:
            pass
        try:
            form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        except Exception:
            pass
        self.provider_combo = QComboBox(self)
        self.effective_model_label = QLabel('')
        self.effective_model_label.setObjectName('modelBadge')
        self.effective_model_label.setWordWrap(True)
        self.effective_model_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        try:
            self.effective_model_label.setMinimumWidth(320)
            self.effective_model_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        except Exception:
            pass
        self.base_paragraphs = QSpinBox(self)
        self.base_paragraphs.setRange(1, 6)
        self.base_paragraphs.setValue(int(pref('base_paragraphs', 2)))
        self.include_highlight = QCheckBox('')
        self.include_highlight.setChecked(pref_bool('include_highlight', True))
        self.include_highlight_label = QLabel(_('Add a key event / key idea / key finding per chapter'))
        self.include_highlight_label.setWordWrap(True)
        try:
            self.include_highlight_label.setMinimumWidth(280)
            self.include_highlight_label.setMinimumHeight(32)
            self.include_highlight_label.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Preferred)
        except Exception:
            pass
        try:
            self.include_highlight_label.setBuddy(self.include_highlight)
        except Exception:
            pass
        self.auto_add_to_data_files = QCheckBox(_('to data files'))
        self.auto_add_to_data_files.setChecked(pref_bool('auto_add_to_data_files', False))
        self.auto_add_to_data_files.setToolTip(_('Automatically add the finished summary to the selected book data files when summarization completes.'))
        self.target_language = QComboBox(self)
        self.populate_target_languages()
        self.source_language_label = QLabel('')
        self.source_language_label.setWordWrap(True)
        self.source_language_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.source_language_label.setStyleSheet('padding: 4px 8px; border: 1px solid palette(mid); border-radius: 4px; background-color: palette(base); color: palette(text); font-size: 9pt; font-weight: 600;')
        try:
            self.source_summary_label.setMinimumWidth(280)
            self.source_language_label.setMinimumWidth(280)
            self.source_summary_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
            self.source_language_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        except Exception:
            pass
        self.configure_provider_button = QPushButton(_('Configure provider…'))
        self._configure_provider_default_tooltip = _('Open the configuration dialog for the selected AI provider.')
        self._configure_provider_zero_config_tooltip = _('This zero-config demo provider is managed automatically.\n\nThere is nothing to configure. Requests may be rate-limited or temporarily unavailable.')
        self.configure_provider_button.setToolTip(self._configure_provider_default_tooltip)
        self.configure_provider_button.clicked.connect(self.configure_provider)
        try:
            self.configure_provider_button.setIcon(QIcon.ic('ai.png'))
        except Exception:
            pass
        provider_row = QWidget(self)
        provider_layout = QHBoxLayout(provider_row)
        provider_layout.setContentsMargins(0, 0, 0, 0)
        provider_layout.setSpacing(6)
        provider_layout.addWidget(self.provider_combo)
        provider_layout.addWidget(self.configure_provider_button)
        provider_layout.addStretch(1)
        form.addRow(_('Source language:'), self.source_language_label)
        form.addRow(_('Source at a glance:'), self.source_summary_label)
        form.addRow(_('AI provider:'), provider_row)
        form.addRow(_('Effective model:'), self.effective_model_label)
        form.addRow(_('Output language:'), self.target_language)

        self.summary_style_panel = QWidget(self)
        summary_style_layout = QHBoxLayout(self.summary_style_panel)
        summary_style_layout.setContentsMargins(0, 0, 0, 0)
        summary_style_layout.setSpacing(4)
        self.summary_style_group = QButtonGroup(self)
        try:
            self.summary_style_group.setExclusive(True)
        except Exception:
            pass
        style_buttons = (
            (_('Auto'), 'auto', _('Use the current calibre window palette for HTML/EPUB output. This may not match the calibre viewer theme.')),
            (_('Light'), 'light', _('Use a fixed light preset for HTML/EPUB output.')),
            (_('Dark'), 'dark', _('Use a fixed dark preset for HTML/EPUB output.')),
            (_('Sepia'), 'sepia', _('Use a fixed sepia preset for HTML/EPUB output.')),
            (_('Custom'), 'custom', _('Use your saved custom CSS preset for HTML/EPUB output.')),
        )
        for label, style_name, tip in style_buttons:
            button = QToolButton(self)
            button.setText(label)
            button.setCheckable(True)
            button.setToolTip(_('Applies to the formatted preview, Save as HTML, and Open in browser.') + '\n\n' + tip)
            button.clicked.connect(lambda _checked=False, style_name=style_name: self.set_summary_output_style(style_name))
            self.summary_style_group.addButton(button)
            self.summary_style_buttons[style_name] = button
            summary_style_layout.addWidget(button)
        self.summary_style_edit_button = QToolButton(self)
        self.summary_style_edit_button.setText(_('Edit custom…'))
        self.summary_style_edit_button.setToolTip(_('Edit the custom CSS preset used by the main-dialog HTML/browser style.'))
        self.summary_style_edit_button.clicked.connect(self.edit_custom_summary_style)
        summary_style_layout.addWidget(self.summary_style_edit_button)
        summary_style_layout.addStretch(0)

        base_row_widget = QWidget(self)
        base_row_layout = QHBoxLayout(base_row_widget)
        base_row_layout.setContentsMargins(0, 0, 0, 0)
        base_row_layout.setSpacing(10)
        highlight_widget = QWidget(self)
        highlight_layout = QHBoxLayout(highlight_widget)
        highlight_layout.setContentsMargins(0, 0, 0, 0)
        highlight_layout.setSpacing(3)
        highlight_layout.addWidget(self.include_highlight, 0, Qt.AlignmentFlag.AlignVCenter)
        highlight_layout.addWidget(self.include_highlight_label, 1, Qt.AlignmentFlag.AlignVCenter)
        base_row_layout.addWidget(self.base_paragraphs, 0, Qt.AlignmentFlag.AlignVCenter)
        base_row_layout.addWidget(highlight_widget, 1, Qt.AlignmentFlag.AlignVCenter)
        form.addRow(_('Base paragraphs:'), base_row_widget)
        # Persist these two settings the moment the user changes them, not only
        # when a run starts. The main dialog is cached for the calibre session,
        # so without live persistence a dialog recreation (e.g. calibre restart)
        # could silently revert the spinbox/checkbox to the value held at
        # construction time, causing runs to use stale settings.
        try:
            self.base_paragraphs.valueChanged.connect(
                lambda _v: set_pref('base_paragraphs', int(_v))
            )
            self.include_highlight.stateChanged.connect(
                lambda _s: set_pref('include_highlight', bool(_s))
            )
        except Exception:
            pass
        top_row.addWidget(options_box, 0, 1)
        settings_layout.addLayout(top_row)

        settings_scroll = QScrollArea(self)
        settings_scroll.setWidgetResizable(True)
        settings_scroll.setFrameShape(QFrame.Shape.NoFrame)
        settings_scroll.setWidget(settings_host)

        button_row = QHBoxLayout()
        button_row.setSpacing(6)
        self.stop_button = QPushButton(_('Stop'))
        self.stop_button.clicked.connect(self.stop_summary)
        self.stop_button.setEnabled(False)
        self.stop_button.setVisible(False)
        try:
            _stop_ic = QIcon.ic('window-close.png')
            if not _stop_ic.isNull():
                self.stop_button.setIcon(_stop_ic)
        except Exception:
            pass
        self.save_button = QToolButton(self)
        self.save_button.setText(_('Save'))
        self.save_button.setToolTip(_('Save/export the current summary (Markdown, HTML, EPUB)'))
        try:
            self.save_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        except Exception:
            try:
                self.save_button.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
            except Exception:
                pass
        try:
            self.save_button.setIcon(QIcon.ic('save.png'))
        except Exception:
            pass
        save_menu = QMenu(self.save_button)
        save_menu.addAction(_('Save as Markdown…'), self.save_result)
        save_menu.addAction(_('Export as HTML…'), self.export_result_html)
        save_menu.addAction(_('Export as EPUB…'), self.export_result_epub)
        self.save_button.setMenu(save_menu)
        try:
            self.save_button.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        except Exception:
            self.save_button.setPopupMode(QToolButton.MenuButtonPopup)
        try:
            self.save_button.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            self.save_button.customContextMenuRequested.connect(lambda p: (self.save_button.menu() or QMenu(self.save_button)).exec(self.save_button.mapToGlobal(p)))
            self.save_button.clicked.connect(self._on_save_clicked)
        except Exception:
            self.save_button.clicked.connect(self._on_save_clicked)
        self.save_button.setEnabled(False)
        self.add_to_data_files_button = QPushButton(_('Add to data files'))
        self.add_to_data_files_button.clicked.connect(self.add_result_to_book_data_files)
        self.add_to_data_files_button.setEnabled(False)
        try:
            self.add_to_data_files_button.setIcon(QIcon.ic('unpack-book.png'))
        except Exception:
            pass
        self.add_to_library_button = QPushButton(_('Add as new book'))
        self.add_to_library_button.clicked.connect(self.add_result_to_library)
        self.add_to_library_button.setEnabled(False)
        try:
            self.add_to_library_button.setIcon(QIcon.ic('add_book.png'))
        except Exception:
            pass
        self.summary_browser_button = QToolButton(self)
        self.summary_browser_button.setText(_('Open in browser'))
        self.summary_browser_button.setToolTip(_('Open the current summary (live/partial if still running) in your default browser'))
        try:
            self.summary_browser_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        except Exception:
            try:
                self.summary_browser_button.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
            except Exception:
                pass
        try:
            self.summary_browser_button.setIcon(QIcon.ic('mimetypes/html.png'))
        except Exception:
            pass
        self.summary_browser_button.clicked.connect(self.open_result_in_browser)
        self.comments_button = QPushButton(_('Append to comments'))
        self.comments_button.clicked.connect(self.append_result_to_comments)
        self.comments_button.setEnabled(False)
        try:
            self.comments_button.setIcon(QIcon.ic('edit_input.png'))
        except Exception:
            pass
        self.add_to_library_checkbox = QCheckBox(_('as new book'))
        self.add_to_library_checkbox.setChecked(pref_bool('add_to_library', False))
        self.add_to_library_checkbox.setToolTip(_('Automatically add the finished summary to the calibre library as a new book entry.'))
        self.manage_cache_button = QToolButton(self)
        self.manage_cache_button.setText(_('Cache'))
        self.manage_cache_button.setToolTip(_('Manage cache'))
        try:
            self.manage_cache_button.setIcon(QIcon.ic('folder_saved_search.png'))
        except Exception:
            try:
                self.manage_cache_button.setIcon(QIcon.ic('reports.png'))
            except Exception:
                pass
        try:
            self.manage_cache_button.setAutoRaise(True)
        except Exception:
            pass
        try:
            self.manage_cache_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        except Exception:
            try:
                self.manage_cache_button.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
            except Exception:
                pass
        try:
            self.manage_cache_button.setMinimumHeight(26)
        except Exception:
            pass
        self.manage_cache_button.clicked.connect(self.open_cache_manager)

        self.history_button = QToolButton(self)
        self.history_button.setText(_('History'))
        self.history_button.setToolTip(_('History'))
        try:
            self.history_button.setIcon(QIcon.ic('scheduler.png'))
        except Exception:
            pass
        try:
            self.history_button.setAutoRaise(True)
        except Exception:
            pass
        try:
            self.history_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        except Exception:
            try:
                self.history_button.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
            except Exception:
                pass
        try:
            self.history_button.setMinimumHeight(26)
        except Exception:
            pass
        self.history_button.clicked.connect(self.open_history)
        auto_add_row = QHBoxLayout()
        auto_add_row.setSpacing(6)
        auto_add_row.addWidget(QLabel(_('Auto-add:'), self))
        auto_add_row.addWidget(self.auto_add_to_data_files)
        auto_add_row.addWidget(self.add_to_library_checkbox)
        auto_add_row.addStretch(1)

        button_row.addLayout(auto_add_row)
        button_row.addWidget(self.comments_button)
        button_row.addWidget(self.add_to_data_files_button)
        button_row.addWidget(self.add_to_library_button)
        button_row.addWidget(self.save_button)
        button_row.addWidget(self.summary_browser_button)
        self.dialog_config_button = QPushButton('')
        self.dialog_config_button.setToolTip(_('Open Chapter Summarizer settings'))
        self.dialog_config_button.clicked.connect(self.open_plugin_config_dialog)
        try:
            self.dialog_config_button.setIcon(QIcon.ic('config.png'))
        except Exception:
            pass
        self.dialog_config_button.setStyleSheet('QPushButton { padding-left: 10px; padding-right: 10px; }')
        button_row.addWidget(self.dialog_config_button)

        button_row.addStretch(1)
        self._sync_primary_action_button(False)

        self.output_tabs = QTabWidget(self)
        self.output_tabs.setTabPosition(QTabWidget.TabPosition.North)
        summary_text_tab = QWidget(self.output_tabs)
        summary_text_layout = QVBoxLayout(summary_text_tab)
        summary_text_layout.setContentsMargins(0, 0, 0, 0)
        self.result_edit = QPlainTextEdit(self)
        self.result_edit.setPlaceholderText(_('Summaries will appear here'))
        install_ctrl_wheel_zoom(self.result_edit, min_pt=7.0, max_pt=22.0, step=1.0)
        summary_text_layout.addWidget(self.result_edit)
        self.output_tabs.addTab(summary_text_tab, _('Summary'))

        formatted_tab = QWidget(self.output_tabs)
        formatted_layout = QVBoxLayout(formatted_tab)
        formatted_layout.setContentsMargins(0, 0, 0, 0)
        self.result_pretty = QTextBrowser(self)
        self.result_pretty.setOpenExternalLinks(True)
        self.result_pretty.setPlaceholderText(_('Formatted summary view will appear here'))
        install_ctrl_wheel_zoom(self.result_pretty, min_pt=7.0, max_pt=22.0, step=1.0)
        formatted_layout.addWidget(self.result_pretty)
        self.output_tabs.addTab(formatted_tab, _('Summary formatted'))

        log_tab = QWidget(self.output_tabs)
        log_layout = QVBoxLayout(log_tab)
        log_layout.setContentsMargins(0, 0, 0, 0)
        self.ai_log_info = QLabel(_('Shows AI activity for this window plus stored backend failures for troubleshooting. Stored errors are historical and remain until you clear them.'))
        self.ai_log_info.setWordWrap(True)
        log_layout.addWidget(self.ai_log_info)
        log_controls = QHBoxLayout()
        self.ai_log_count_lbl = QLabel('', self)
        log_controls.addWidget(self.ai_log_count_lbl)
        log_controls.addStretch(1)
        self.ai_log_refresh_btn = QPushButton(_('Refresh'))
        self.ai_log_refresh_btn.clicked.connect(self.refresh_ai_log_view)
        self.ai_log_copy_btn = QPushButton(_('Copy'))
        self.ai_log_copy_btn.clicked.connect(self.copy_ai_log)
        self.ai_log_save_btn = QPushButton(_('Save log…'))
        self.ai_log_save_btn.clicked.connect(self.save_ai_log)
        self.ai_log_clear_btn = QPushButton(_('Clear'))
        self.ai_log_clear_btn.clicked.connect(self.clear_ai_log)
        log_controls.addWidget(self.ai_log_refresh_btn)
        log_controls.addWidget(self.ai_log_copy_btn)
        log_controls.addWidget(self.ai_log_save_btn)
        log_controls.addWidget(self.ai_log_clear_btn)
        log_layout.addLayout(log_controls)
        self.ai_log_view = QPlainTextEdit(self)
        self.ai_log_view.setReadOnly(True)
        self.ai_log_view.setPlaceholderText(_('No AI activity yet'))
        install_ctrl_wheel_zoom(self.ai_log_view, min_pt=7.0, max_pt=22.0, step=1.0)
        log_layout.addWidget(self.ai_log_view, 1)
        self.output_tabs.addTab(log_tab, _('AI log'))

        more_tab = QWidget(self.output_tabs)
        more_layout = QVBoxLayout(more_tab)
        more_layout.setContentsMargins(0, 0, 0, 0)
        more_scroll = QScrollArea(more_tab)
        more_scroll.setWidgetResizable(True)
        try:
            more_scroll.setFrameShape(QFrame.Shape.NoFrame)
        except Exception:
            pass
        more_host = QWidget()
        more_host_layout = QVBoxLayout(more_host)
        more_host_layout.setContentsMargins(12, 12, 12, 12)
        more_host_layout.setSpacing(12)

        plugins_container = QWidget()
        plugins_container_layout = QHBoxLayout(plugins_container)
        plugins_container_layout.setContentsMargins(0, 0, 0, 0)
        plugins_container_layout.setSpacing(0)
        plugins_container_layout.addSpacing(120)

        plugins_data = [
            ('AI Config - Quick access to AI settings', 'https://www.mobileread.com/forums/showthread.php?t=371037'),
            ('BERTRAND Livreiros - Downloads metadata and covers from BERTRAND', 'https://www.mobileread.com/forums/showthread.php?t=374669'),
            ('Best Sellers - Book Catalogs, Blurb & Review Finders', 'https://www.mobileread.com/forums/showthread.php?t=374278'),
            ('Book List Plus - Book list view with cover thumbnails', 'https://www.mobileread.com/forums/showthread.php?t=373100'),
            ('Calibre Config Reports - Configuration reporting tool', 'https://www.mobileread.com/forums/showthread.php?t=369555'),
            ('Chapter Summarizer - Powerful document summarizer', 'https://www.mobileread.com/forums/showthread.php?t=374061'),
            ('Columns Helper - Custom columns management tools', 'https://www.mobileread.com/forums/showthread.php?t=372614'),
            ('Comfy Reminders - Create and manage reminders', 'https://www.mobileread.com/forums/showthread.php?t=367258'),
            ('Current Paths - Show config/library paths in title/status bar', 'https://www.mobileread.com/forums/showthread.php?t=371623'),
            ('Custom Action Button - Configurable toolbar button', 'https://www.mobileread.com/forums/showthread.php?t=374301'),
            ('DELFI knjižare - Downloads metadata and covers from DELFI', 'https://www.mobileread.com/forums/showthread.php?t=374478'),
            ('Extended Book Details - Toggle Extended Book Details in left pane', 'https://www.mobileread.com/forums/showthread.php?t=372675'),
            ('FTS+ - Full text search with additional features', 'https://www.mobileread.com/forums/showthread.php?t=372481'),
            ('Goldendict Bridge - Multiple dictionary local lookup', 'https://www.mobileread.com/forums/showthread.php?t=372546'),
            ('Input Order Swap - Swap top two preferred input formats', 'https://www.mobileread.com/forums/showthread.php?t=371116'),
            ('KoboTouch Config Button - Quick access to KoboTouch driver config', 'https://www.mobileread.com/forums/showthread.php?t=367728'),
            ('Language Switcher - Quick-switch calibre language', 'https://www.mobileread.com/forums/showthread.php?t=372153'),
            ('Look & Feel Button - Quick access to Look & Feel preferences', 'https://www.mobileread.com/forums/showthread.php?t=372670'),
            ('Metadata Sources Preview - Preview metadata sources in download dialog', 'https://www.mobileread.com/forums/showthread.php?t=373625'),
            ('Open Config Folder - Open calibre configuration folder', 'https://www.mobileread.com/forums/showthread.php?t=372023'),
            ('OPF Helper - Inspect, scan, and report on OPF data', 'https://www.mobileread.com/forums/showthread.php?t=371086'),
            ('Output Format Cycler - Cycle preferred output format', 'https://www.mobileread.com/forums/showthread.php?t=371117'),
            ('Quick Palette Customize - Quick access to palette configuration', 'https://www.mobileread.com/forums/showthread.php?t=372047'),
            ('RSS Reader - Read RSS and Atom feeds inside calibre', 'https://www.mobileread.com/forums/showthread.php?t=371755'),
            ('Shortcuts Helper - Quick access to Shortcuts preferences', 'https://www.mobileread.com/forums/showthread.php?t=372349'),
            ('Template Catalog - Manage and organize templates', 'https://www.mobileread.com/forums/showthread.php?t=366843'),
            ('Toggle Light & Dark - Toggle light and dark theme', 'https://www.mobileread.com/forums/showthread.php?t=371676'),
            ('Toolbars Button - Quick access to Toolbars and Menus preferences', 'https://www.mobileread.com/forums/showthread.php?t=372229'),
            ('VL from selection - Search using Virtual Libraries from selection', 'https://www.mobileread.com/forums/showthread.php?t=371238'),
        ]

        plugins_grid = QWidget()
        plugins_grid_layout = QVBoxLayout(plugins_grid)
        plugins_grid_layout.setContentsMargins(0, 0, 0, 0)
        plugins_grid_layout.setSpacing(6)

        for plugin_name, plugin_url in plugins_data:
            plugin_row = QWidget()
            plugin_row_layout = QHBoxLayout(plugin_row)
            plugin_row_layout.setContentsMargins(0, 0, 0, 0)
            plugin_row_layout.setSpacing(4)

            icon_label = QLabel('🔗')
            plugin_row_layout.addWidget(icon_label)

            parts = plugin_name.split(' - ', 1)
            if len(parts) == 2:
                name_part, desc_part = parts
                plugin_link = NoUnderlineLinkLabel(name_part, plugin_url)
                plugin_link.setStyleSheet('font-size: 10pt; color: palette(link);')
                desc_label = QLabel(f' - {desc_part}')
                desc_label.setStyleSheet('font-size: 10pt;')
                plugin_row_layout.addWidget(plugin_link)
                plugin_row_layout.addWidget(desc_label)
            else:
                plugin_link = NoUnderlineLinkLabel(plugin_name, plugin_url)
                plugin_link.setStyleSheet('font-size: 10pt; color: palette(link);')
                plugin_row_layout.addWidget(plugin_link)
            plugin_row_layout.addStretch(1)
            plugins_grid_layout.addWidget(plugin_row)

        plugins_grid_layout.addStretch(1)
        plugins_container_layout.addWidget(plugins_grid)
        plugins_container_layout.addStretch(1)
        more_host_layout.addWidget(plugins_container)
        more_host_layout.addStretch(1)
        more_scroll.setWidget(more_host)
        more_layout.addWidget(more_scroll)
        self.output_tabs.addTab(more_tab, _('More'))

        about_tab = QWidget(self.output_tabs)
        about_layout = QVBoxLayout(about_tab)
        about_layout.setContentsMargins(0, 0, 0, 0)
        about_scroll = QScrollArea(about_tab)
        about_scroll.setWidgetResizable(True)
        try:
            about_scroll.setFrameShape(QFrame.Shape.NoFrame)
        except Exception:
            pass
        about_host = QWidget()
        about_host_layout = QHBoxLayout(about_host)
        about_host_layout.setContentsMargins(6, 6, 6, 6)
        about_host_layout.setSpacing(14)

        about_desc_column = QWidget(about_host)
        about_desc_layout = QVBoxLayout(about_desc_column)
        about_desc_title = QLabel(_('<b>About Chapter Summarizer</b>'))
        try:
            about_desc_title.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        except Exception:
            pass
        about_desc_title.setStyleSheet('font-size: 12pt;')
        about_desc_layout.addWidget(about_desc_title)

        about_desc_label = QLabel(_(
            'Easily summarize books, articles, and news by chapter or section inside calibre — '
            'with AI-provider integration, chapter selection, cache/history tools, '
            'flash summaries, formatted preview, export options, plus open-in-browser and append-to-Comments capabilities'
        ))
        about_desc_label.setWordWrap(True)
        about_desc_label.setStyleSheet('font-size: 10pt;')
        about_desc_layout.addWidget(about_desc_label)

        about_features_title = QLabel(_('<b>Key features:</b>'))
        about_features_title.setStyleSheet('font-size: 12pt; margin-top: 6px;')
        about_desc_layout.addWidget(about_features_title)

        about_features_label = QLabel(_(
            '• Chapter-by-chapter and flash-summary modes<br>'
            '• Chapter picker for excluding Notes, References, and similar sections<br>'
            '• Reusable cache with per-source history tracking<br>'
            '• Works with selected calibre books and external files<br>'
            '• Plain and formatted summary views with Ctrl + mouse wheel zoom<br>'
            '• Save as Markdown, HTML, or EPUB<br>'
            '• Append to comments, add to data files, or add as a new library book'
        ))
        about_features_label.setTextFormat(Qt.TextFormat.RichText)
        about_features_label.setWordWrap(True)
        about_features_label.setMinimumWidth(200)
        about_features_label.setStyleSheet('font-size: 10pt;')
        about_desc_layout.addWidget(about_features_label)

        # Removed plugin/calibre version callout — redundant per UI request.
        about_desc_layout.addStretch(1)
        about_host_layout.addWidget(about_desc_column, 3)

        about_links_column = QWidget(about_host)
        about_links_layout = QVBoxLayout(about_links_column)
        about_links_title = QLabel(_('<b>Support & Resources</b>'))
        try:
            about_links_title.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        except Exception:
            pass
        about_links_title.setStyleSheet('font-size: 13pt;')
        about_links_layout.addWidget(about_links_title)

        self.free_api_guide_button = QPushButton(_('Free API guide'))
        self.free_api_guide_button.setToolTip(_('How to obtain free API keys for NVIDIA, OpenRouter, and LM Studio'))
        self.free_api_guide_button.clicked.connect(self.open_free_api_guide)
        try:
            self.free_api_guide_button.setIcon(QIcon.ic('help.png'))
        except Exception:
            pass
        free_api_guide_row = QHBoxLayout()
        free_api_guide_row.setContentsMargins(0, 0, 0, 0)
        free_api_guide_row.addStretch(1)
        free_api_guide_row.addWidget(self.free_api_guide_button)
        free_api_guide_row.addStretch(1)
        about_links_layout.addLayout(free_api_guide_row)
        try:
            about_links_layout.addSpacing(6)
        except Exception:
            pass

        self._about_link_specs = []

        def _wire_about_label(label):
            try:
                label.setOpenExternalLinks(False)
            except Exception:
                pass
            try:
                label.linkActivated.connect(open_url)
            except Exception:
                try:
                    from qt.core import QDesktopServices
                    label.linkActivated.connect(lambda u: QDesktopServices.openUrl(QUrl(u)))
                except Exception:
                    pass

        about_forum_link = QLabel('')
        _wire_about_label(about_forum_link)
        try:
            about_forum_link.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        except Exception:
            pass
        self._about_link_specs.append((
            about_forum_link,
            '🔗',
            'https://www.mobileread.com/forums/showthread.php?t=374587',
            _('MR help thread'),
            11,
        ))
        try:
            about_forum_link.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        except Exception:
            pass
        about_links_layout.addWidget(about_forum_link)
        try:
            about_links_layout.addSpacing(4)
        except Exception:
            pass

        about_support_link = QLabel('')
        _wire_about_label(about_support_link)
        try:
            about_support_link.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        except Exception:
            pass
        self._about_link_specs.append((
            about_support_link,
            '❤️',
            'https://ko-fi.com/comfy_n',
            _('Show appreciation'),
            11,
        ))
        try:
            about_support_link.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        except Exception:
            pass
        about_links_layout.addWidget(about_support_link)

        try:
            qr_icon = _load_bundled_icon('images/qrcode.png')
        except Exception:
            qr_icon = None
        try:
            if qr_icon is not None and not qr_icon.isNull():
                about_qr = QLabel('')
                try:
                    about_qr.setAlignment(Qt.AlignmentFlag.AlignHCenter)
                except Exception:
                    pass
                try:
                    about_qr.setPixmap(qr_icon.pixmap(170, 170))
                except Exception:
                    try:
                        about_qr.setPixmap(qr_icon.pixmap(170))
                    except Exception:
                        pass
                try:
                    about_qr.setCursor(Qt.CursorShape.PointingHandCursor)
                except Exception:
                    pass
                about_qr.mousePressEvent = lambda _event: open_url('https://ko-fi.com/comfy_n')
                about_links_layout.addSpacing(8)
                about_links_layout.addWidget(about_qr)
        except Exception:
            pass

        about_links_layout.addStretch(1)
        about_host_layout.addWidget(about_links_column, 2)
        about_scroll.setWidget(about_host)
        about_layout.addWidget(about_scroll)
        self._about_scroll = about_scroll
        self.output_tabs.addTab(about_tab, _('About'))

        # Bottom host: status + action buttons + outputs
        bottom_host = QWidget(self)
        bottom_layout = QVBoxLayout(bottom_host)
        bottom_layout.setContentsMargins(0, 0, 0, 0)
        bottom_layout.setSpacing(6)
        bottom_layout.addLayout(button_row)
        bottom_layout.addWidget(self.output_tabs, 1)
        summary_style_host = QWidget(self)
        summary_style_host_layout = QHBoxLayout(summary_style_host)
        summary_style_host_layout.setContentsMargins(0, 0, 0, 0)
        summary_style_host_layout.setSpacing(8)
        summary_style_host_layout.addWidget(QLabel(_('HTML / browser style:'), self))
        summary_style_host_layout.addWidget(self.summary_style_panel, 1)
        bottom_layout.addWidget(summary_style_host)

        upper_section = QWidget(self)
        upper_section_layout = QVBoxLayout(upper_section)
        upper_section_layout.setContentsMargins(0, 0, 0, 0)
        upper_section_layout.setSpacing(6)
        upper_section_layout.addWidget(settings_scroll, 1)
        upper_section_layout.addWidget(self.book_navigation_panel, 0, Qt.AlignmentFlag.AlignHCenter)

        splitter = QSplitter(Qt.Orientation.Vertical, self)
        splitter.addWidget(upper_section)
        splitter.addWidget(bottom_host)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)
        self._main_splitter = splitter
        self._settings_scroll = settings_scroll
        self._bottom_host = bottom_host
        try:
            splitter.splitterMoved.connect(self._on_main_splitter_moved)
        except Exception:
            pass
        self._schedule_splitter_adjustment()
        root.addWidget(splitter, 1)

        footer_row = QHBoxLayout()
        footer_row.setContentsMargins(0, 0, 0, 0)
        footer_row.setSpacing(6)

        self.versions_label = QLabel('Chapter Summarizer {0} — calibre {1}'.format(plugin_version_text(), calibre_version_text()))
        self.versions_label.setStyleSheet(_footer_version_label_stylesheet(self))
        footer_row.addWidget(self.versions_label)
        footer_row.addStretch(1)

        self.status_label = QLabel(_('Ready'))
        self.status_label.setStyleSheet('QLabel { color: palette(text); font-size: 9pt; }')
        footer_row.addWidget(self.status_label)

        # Cache callout (EBD feedback-callout style) — between status and progress
        self.cache_callout_label = QLabel('', self)
        self.cache_callout_label.setWordWrap(False)
        self.cache_callout_label.setMinimumHeight(24)
        try:
            _sp = self.cache_callout_label.sizePolicy()
            _sp.setRetainSizeWhenHidden(True)
            self.cache_callout_label.setSizePolicy(_sp)
        except Exception:
            pass
        self.cache_callout_label.setVisible(False)
        footer_row.addWidget(self.cache_callout_label)

        footer_row.addStretch(1)

        self.progress = QProgressBar(self)
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setTextVisible(True)
        self.progress.setFormat('%p%')
        self.progress.setFixedWidth(140)
        footer_row.addWidget(self.progress)

        # Cache/History pinned to the right edge so they never shift during task activity
        footer_row.addWidget(self.history_button)
        footer_row.addWidget(self.manage_cache_button)

        root.addLayout(footer_row)

        self._apply_runtime_theme()
        app = QApplication.instance()
        if app is not None and hasattr(app, 'palette_changed') and not self._palette_signal_connected:
            try:
                app.palette_changed.connect(self._apply_runtime_theme)
                self._palette_signal_connected = True
            except Exception:
                pass

        self.populate_providers()
        self.provider_combo.currentIndexChanged.connect(self.update_effective_model_label)
        self.provider_combo.currentIndexChanged.connect(self._sync_provider_controls)
        self.provider_combo.currentIndexChanged.connect(lambda _=None: set_pref('provider_name', self.provider_combo.currentData() or self.provider_combo.currentText().strip()))
        self.target_language.currentIndexChanged.connect(self.update_source_summary_label)
        self.refresh_ai_log_view()
        self.update_effective_model_label()
        self._sync_provider_controls()
        self.update_source_summary_label()
        self._sync_summary_style_buttons()
        self._refresh_about_link_styles()
        self._toc_editor_dialog = None
        self._toc_viewer_dialog = None
        self._toc_count_signals = TocWorkerSignals()
        self._toc_count_signals.finished.connect(self._on_lazy_toc_count_finished)
        self._toc_count_signals.error.connect(self._on_lazy_toc_count_error)
        try:
            self._refresh_cover_meta()
        except Exception:
            pass

    def _apply_runtime_theme(self):
        base = '''
QPushButton {
    padding: 5px 10px;
}
QFrame#coverPanel {
    background-color: palette(base);
    border: 1px solid palette(mid);
    border-radius: 6px;
}
QLabel#modelBadge {
    padding: 4px 8px;
    border: 1px solid palette(mid);
    border-left: 3px solid palette(link);
    border-radius: 4px;
    background-color: palette(base);
    color: palette(text);
    font-size: 9pt;
}

QLabel#coverMeta {
    color: palette(text);
    font-size: 10pt;
}

QTextBrowser {
    background-color: palette(base);
    color: palette(text);
    border: 1px solid palette(mid);
    border-radius: 6px;
    padding: 8px;
}
'''
        try:
            self.setStyleSheet(base + chapter_summarizer_tab_stylesheet(self) + ccr_scrollbar_stylesheet() + ccr_combo_stylesheet())
        except Exception:
            try:
                self.setStyleSheet(base)
            except Exception:
                pass
        try:
            self.output_tabs.setStyleSheet(chapter_summarizer_tab_stylesheet(self.output_tabs))
            combo_style = ccr_combo_stylesheet()
            self.provider_combo.setStyleSheet(combo_style)
            self.target_language.setStyleSheet(combo_style)
            self.external_path.setStyleSheet(combo_style)
        except Exception:
            pass
        try:
            self.versions_label.setStyleSheet(_footer_version_label_stylesheet(self))
        except Exception:
            pass
        try:
            self.run_button.setStyleSheet(_primary_action_button_stylesheet(self))
        except Exception:
            pass
        try:
            self._refresh_about_link_styles()
        except Exception:
            pass
        try:
            about_scroll = getattr(self, '_about_scroll', None)
            if about_scroll is not None:
                about_scroll.setStyleSheet(ccr_scrollbar_stylesheet())
        except Exception:
            pass
        try:
            self.update_source_summary_label()
        except Exception:
            pass
        try:
            self._refresh_result_preview()
        except Exception:
            pass

    def _refresh_about_link_styles(self):
        specs = getattr(self, '_about_link_specs', None) or []
        if not specs:
            return
        try:
            link_hex = self.palette().color(QPalette.ColorRole.Link).name()
        except Exception:
            link_hex = '#1e6bd6'
        for label, icon_text, url, caption, font_pt in specs:
            try:
                icon = _html_escape(icon_text)
                href = _html_escape(url)
                text = _html_escape(caption)
                label.setText(f'{icon} <a href="{href}" style="text-decoration:none; color:{link_hex};">{text}</a>')
                label.setStyleSheet('font-size: %dpt;' % int(font_pt or 11))
            except Exception:
                continue

    def changeEvent(self, event):
        try:
            event_type = event.type()
            if event_type in {QEvent.Type.PaletteChange, QEvent.Type.ApplicationPaletteChange}:
                self._apply_runtime_theme()
        except Exception:
            pass
        super().changeEvent(event)

    def populate_providers(self, select_provider_name: str | None = None):
        self._building = True
        try:
            self.provider_combo.clear()
            self.provider_combo.addItem(ZERO_CONFIG_DEMO_PROVIDER_A, ZERO_CONFIG_DEMO_PROVIDER_A)
            self.provider_combo.addItem(ZERO_CONFIG_DEMO_PROVIDER_B, ZERO_CONFIG_DEMO_PROVIDER_B)
            for plugin in sorted(available_ai_provider_plugins(), key=lambda p: p.name.lower()):
                if plugin.capabilities & AICapabilities.text_to_text == AICapabilities.text_to_text:
                    self.provider_combo.addItem(plugin.name, plugin.name)
            if pref_bool('show_nvidia_free_provider', False):
                self.provider_combo.addItem('NVIDIA Free', 'NVIDIA Free')
            # Keep API-based NVIDIA provider always available
            self.provider_combo.addItem('NVIDIA Free (API)', 'NVIDIA Free (API)')
            target = (select_provider_name or pref('provider_name', ZERO_CONFIG_DEMO_PROVIDER_A) or ZERO_CONFIG_DEMO_PROVIDER_A).strip()
            idx = self.provider_combo.findData(target)
            self.provider_combo.setCurrentIndex(max(0, idx))
        finally:
            self._building = False
        self.update_effective_model_label()

    def _zero_config_provider_notice(self) -> str:
        return _('Managed zero-config demo provider.\n\nChapter Summarizer rotates through a built-in curated model pool. Requests may be rate-limited or temporarily unavailable.')

    def _sync_provider_controls(self):
        provider_name = self.provider_combo.currentData() or self.provider_combo.currentText().strip()
        is_zero_config = is_zero_config_demo_provider(provider_name)
        try:
            self.configure_provider_button.setEnabled(not is_zero_config)
            self.configure_provider_button.setToolTip(self._configure_provider_zero_config_tooltip if is_zero_config else self._configure_provider_default_tooltip)
        except Exception:
            pass
        try:
            self.model_override_edit.setEnabled(not is_zero_config)
            self.model_override_edit.setToolTip(self._model_override_zero_config_tooltip if is_zero_config else self._model_override_default_tooltip)
        except Exception:
            pass
        try:
            self.provider_combo.setToolTip(self._zero_config_provider_notice() if is_zero_config else '')
        except Exception:
            pass

    def populate_target_languages(self):
        self.target_language.clear()
        self.target_language.addItem(_('Keep source language'), 'same-as-source')
        for code in TARGET_LANGUAGE_CODES:
            self.target_language.addItem(display_language_name(code), code)
        target = normalize_target_language(pref('target_language', 'same-as-source'))
        idx = self.target_language.findData(target)
        self.target_language.setCurrentIndex(max(0, idx))

    def _on_primary_action_button_clicked(self):
        if self._worker is not None and self._worker.is_running():
            self.stop_summary()
        else:
            self.start_summary()

    def _sync_primary_action_button(self, busy: bool):
        if busy:
            self.run_button.setText(_('Stop'))
            try:
                self.run_button.setIcon(QIcon.ic('dialog_cancel.png'))
            except Exception:
                pass
            self.run_button.setToolTip(_('Stop the current summarization'))
        else:
            self.run_button.setText(_('Summarize now'))
            try:
                _run_icon = self.icon if isinstance(self.icon, QIcon) and not self.icon.isNull() else _plugin_theme_icon()
                if _run_icon is not None:
                    self.run_button.setIcon(_run_icon)
            except Exception:
                pass
            self.run_button.setToolTip(_('Start chapter summarization'))

    def _configured_text_model(self, provider_name: str) -> tuple[str, str]:
        raw = pref_for_provider(provider_name, 'text_model', None)
        if isinstance(raw, tuple):
            model_id = str(raw[0] or '') if len(raw) > 0 else ''
            model_name = str(raw[1] or '') if len(raw) > 1 else ''
            return model_id, model_name
        if isinstance(raw, list) and raw:
            model_id = str(raw[0] or '') if len(raw) > 0 else ''
            model_name = str(raw[1] or '') if len(raw) > 1 else ''
            return model_id, model_name
        if isinstance(raw, dict):
            return str(raw.get('id') or ''), str(raw.get('name') or '')
        if isinstance(raw, str):
            text = raw.strip()
            return text, text
        return '', ''

    def _is_automatic_model_choice(self, model_id: str, model_name: str) -> bool:
        model_id = str(model_id or '').strip().lower()
        model_name = str(model_name or '').strip().lower()
        if model_id:
            return False
        return model_name in {'', 'automatic', 'automatic provider choice'} or 'automatic' in model_name

    def _is_github_provider(self, provider_name: str) -> bool:
        return str(provider_name or '').strip().lower() in {'github', 'githubai'}

    def _normalize_provider_model_id(self, provider_name: str, model_value: str) -> str:
        text = str(model_value or '').strip()
        if not text:
            return ''
        if not self._is_github_provider(provider_name):
            return text
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

    def _last_runtime_model_info(self, provider_name: str) -> tuple[str, str]:
        if not provider_name or provider_name != self._last_provider_used:
            return '', ''
        model_id = str(self._last_model_used or '').strip()
        if not model_id or model_id == '(provider default)':
            return '', ''
        readable = model_id
        try:
            provider = resolve_summary_provider(provider_name)
            readable = provider.human_readable_model_name(model_id) or readable
        except Exception:
            pass
        readable = self._preserve_model_flags(model_id, readable)
        return model_id, readable

    def _provider_strategy_model_info(self, provider_name: str) -> tuple[str, str, str]:
        if not self._is_github_provider(provider_name):
            return '', '', ''
        strategy = str(pref_for_provider(provider_name, 'model_choice_strategy', 'medium') or 'medium').strip().lower()
        if strategy not in {'high', 'medium', 'low'}:
            strategy = 'medium'
        try:
            safe_models = {
                'low': 'openai/gpt-4.1-mini',
                'medium': 'openai/gpt-4.1',
                'high': 'openai/gpt-4o',
            }
            model_id = self._normalize_provider_model_id(provider_name, safe_models.get(strategy, 'openai/gpt-4.1'))
            readable = model_id
            try:
                provider = plugin_for_name(provider_name)
                readable = provider.human_readable_model_name(model_id) or readable
            except Exception:
                pass
            readable = self._preserve_model_flags(model_id, readable)
            return model_id, readable, _('Provider strategy (%s)') % strategy
        except Exception:
            return '', '', ''

    def _current_requested_model_info(self, provider_name: str | None = None) -> tuple[str, str, str]:
        provider_name = provider_name or self.provider_combo.currentData() or self.provider_combo.currentText().strip()
        if provider_name == 'NVIDIA Free':
            from calibre_plugins.chapter_summarizer.nvidia_free import get_model
            model = get_model()
            if model:
                return model, model, _('NVIDIA Free settings')
            return '', '', ''
        if provider_name == 'NVIDIA Free (API)':
            from calibre_plugins.chapter_summarizer.nvidia_free import get_api_model
            model = get_api_model()
            if model:
                return model, model, _('NVIDIA Free (API) settings')
            return '', '', ''
        if is_zero_config_demo_provider(provider_name):
            model = planned_model_for_provider(provider_name)
            if model:
                readable = self._preserve_model_flags(model, model)
                return model, readable, _('Zero-config rotating pool')
            return '', '', ''
        manual_override = str(pref('model_name', '') or '').strip()
        if manual_override:
            manual_override = self._normalize_provider_model_id(provider_name, manual_override)
            try:
                provider = resolve_summary_provider(provider_name)
                readable = provider.human_readable_model_name(manual_override) or manual_override
            except Exception:
                readable = manual_override
            readable = self._preserve_model_flags(manual_override, readable)
            return manual_override, readable, _('Manual override')
        model_id, model_name = self._configured_text_model(provider_name)
        model_id = self._normalize_provider_model_id(provider_name, model_id)
        if not model_id and model_name:
            model_id = self._normalize_provider_model_id(provider_name, model_name)
        if (model_id or model_name) and not self._is_automatic_model_choice(model_id, model_name):
            readable = model_name or model_id
            try:
                provider = resolve_summary_provider(provider_name)
                readable = provider.human_readable_model_name(model_id or readable) or readable
            except Exception:
                pass
            readable = self._preserve_model_flags(model_id or model_name, readable)
            return model_id or model_name, readable, _('Provider settings')
        return self._provider_strategy_model_info(provider_name)

    def current_effective_model_info(self) -> tuple[str, str, str]:
        provider_name = self.provider_combo.currentData() or self.provider_combo.currentText().strip()
        model_id, readable, source = self._current_requested_model_info(provider_name)
        if model_id:
            return model_id, readable, source
        last_model_id, last_model_name = self._last_runtime_model_info(provider_name)
        if last_model_id:
            return last_model_id, last_model_name, _('Last automatic choice')
        return '', _('Auto'), _('Chosen when the request starts')

    def _preserve_model_flags(self, raw_model: str, readable: str) -> str:
        label = str(readable or raw_model or '').strip()
        raw = str(raw_model or '').strip().lower()
        if ':free' in raw and 'free' not in label.lower():
            label = _('%s (free)') % label
        return label

    def _effective_model_colors(self) -> tuple[str, str, str]:
        palette = self.palette()
        link = palette.color(QPalette.ColorRole.Link).name()
        text = palette.color(QPalette.ColorRole.Text).name()
        muted = palette.color(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text).name()
        return link, text, muted

    def update_effective_model_label(self):
        model_id, display_name, source = self.current_effective_model_info()
        provider_name = self.provider_combo.currentData() or self.provider_combo.currentText().strip()
        if provider_name == 'OpenRouter':
            reasoning = str(pref_for_provider(provider_name, 'reasoning_strategy', 'auto') or 'auto')
        link_color, text_color, muted_color = self._effective_model_colors()
        text = '<span style="font-weight:700; color:%s;">%s</span><span style="color:%s;"> — %s</span>' % (
            html.escape(link_color),
            html.escape(display_name),
            html.escape(muted_color),
            html.escape(source),
        )
        tooltip_lines = [
            _('Model: %s') % display_name,
            _('Source: %s') % source,
            _('Provider: %s') % (provider_name or _('Unknown provider')),
        ]
        if is_zero_config_demo_provider(provider_name):
            tooltip_lines.append(_('Managed pool: rotating zero-config demo models only'))
            tooltip_lines.append(_('Availability: rate-limited and may be temporarily unavailable'))
            last_model_id, last_model_name = self._last_runtime_model_info(provider_name)
            if last_model_id:
                tooltip_lines.append(_('Last used: %s') % last_model_name)
        if provider_name == 'OpenRouter':
            tooltip_lines.append(_('Reasoning: %s') % reasoning)
        if model_id and model_id != display_name:
            tooltip_lines.append(_('ID: %s') % model_id)
        if provider_name == 'OpenRouter' and model_id:
            try:
                from calibre.ai.open_router.backend import get_available_models
                models = get_available_models() or {}
                raw_id = str(model_id).split(',', 1)[0].strip()
                model = models.get(raw_id)
                if model is not None:
                    created_str = ''
                    if isinstance(getattr(model, 'created', None), datetime):
                        created_str = model.created.strftime('%Y-%m-%d')
                    elif isinstance(getattr(model, 'created', None), int):
                        created_str = datetime.fromtimestamp(model.created).strftime('%Y-%m-%d')
                    if created_str:
                        tooltip_lines.append(_('Created: %s') % created_str)
                    try:
                        tooltip_lines.append(_('Price: %s') % (_('Free') if model.pricing.is_free else _('Paid')))
                    except Exception:
                        pass
                    try:
                        tooltip_lines.append(_('Moderation: %s') % (_('Yes') if model.is_moderated else _('No')))
                    except Exception:
                        pass
                    try:
                        if model.context_length:
                            tooltip_lines.append(_('Context: %sk') % (model.context_length // 1024))
                    except Exception:
                        pass
            except Exception:
                pass
        self.effective_model_label.setText(text)
        self.effective_model_label.setToolTip('\n'.join(tooltip_lines))

    def current_summary_output_style(self) -> str:
        return self.current_html_output_style()

    def current_summary_custom_css(self) -> str:
        return self.current_html_custom_css()

    def current_html_output_style(self) -> str:
        return normalize_summary_output_style(pref('html_output_style', 'auto'))

    def current_html_custom_css(self) -> str:
        return str(pref('html_custom_css', '') or '').strip()

    def current_epub_output_style(self) -> str:
        return normalize_summary_output_style(pref('epub_output_style', 'auto'))

    def current_epub_custom_css(self) -> str:
        return str(pref('epub_custom_css', '') or '').strip()

    def _sync_summary_style_buttons(self, active_style: str | None = None):
        style = normalize_summary_output_style(active_style or self.current_html_output_style())
        for name, button in self.summary_style_buttons.items():
            try:
                is_active = (name == style)
                button.setChecked(is_active)
                if is_active:
                    button.setStyleSheet('QToolButton { border: 2px solid palette(highlight); font-weight: bold; }')
                else:
                    button.setStyleSheet('QToolButton { border: 1px solid palette(mid); }')
            except Exception:
                pass

    def set_summary_output_style(self, style_name: str):
        style = normalize_summary_output_style(style_name)
        if style == 'custom' and not self.current_html_custom_css():
            self.edit_custom_summary_style(select_custom=True)
            return
        set_pref('html_output_style', style)
        self._sync_summary_style_buttons(style)
        self._refresh_result_preview()

    def edit_custom_summary_style(self, select_custom: bool = False):
        dialog = SummaryStyleDialog(self.current_html_custom_css(), parent=self)
        try:
            dialog.setWindowIcon(self.windowIcon())
        except Exception:
            pass
        if dialog.exec() == QDialog.DialogCode.Accepted:
            set_pref('html_custom_css', dialog.value)
            if select_custom or self.current_html_output_style() == 'custom':
                set_pref('html_output_style', 'custom')
            self._sync_summary_style_buttons()
            self._refresh_result_preview()
            return
        self._sync_summary_style_buttons()

    def _current_summary_html(self, text: str, layout_mode: str = 'standard') -> str:
        return build_summary_html(
            text,
            palette=self.palette(),
            style_name=self.current_html_output_style(),
            custom_css=self.current_html_custom_css(),
            layout_mode=layout_mode,
        )

    def _current_epub_summary_html(self, text: str) -> str:
        return build_summary_html(
            text,
            palette=self.palette(),
            style_name=self.current_epub_output_style(),
            custom_css=self.current_epub_custom_css(),
            layout_mode='standard',
        )

    def _refresh_result_preview(self, text: str | None = None):
        if not hasattr(self, 'result_pretty'):
            return
        if text is None:
            text = self.result_edit.toPlainText()
        if not text:
            self.result_pretty.clear()
            return
        try:
            self.result_pretty.setHtml(self._current_summary_html(text))
        except Exception:
            pass

    def _on_main_splitter_moved(self, _pos: int, _index: int):
        if self._applying_splitter_sizes:
            return
        self._splitter_user_adjusted = True

    def _schedule_splitter_adjustment(self):
        if self._splitter_user_adjusted or self._main_splitter is None:
            return
        try:
            QTimer.singleShot(0, self._apply_initial_splitter_sizes)
        except Exception:
            pass

    def _apply_initial_splitter_sizes(self):
        splitter = self._main_splitter
        if splitter is None or self._splitter_user_adjusted:
            return
        try:
            total = sum(int(x) for x in splitter.sizes())
            if total <= 0:
                total = int(splitter.height())
            if total <= 0:
                return
            top_hint = 0
            if self._settings_scroll is not None and self._settings_scroll.widget() is not None:
                host = self._settings_scroll.widget()
                top_hint = max(int(host.sizeHint().height()), int(host.minimumSizeHint().height()))
            bottom_hint = 220
            if self._bottom_host is not None:
                bottom_hint = max(220, int(self._bottom_host.sizeHint().height()))
            desired_top = top_hint + 12 if top_hint else int(total * 0.46)
            bottom_reserve = max(260, min(bottom_hint, int(total * 0.55)))
            top_size = min(max(340, desired_top), max(220, total - bottom_reserve))
            if top_size <= 0 or top_size >= total:
                return
            self._applying_splitter_sizes = True
            splitter.setSizes([top_size, max(180, total - top_size)])
        except Exception:
            pass
        finally:
            self._applying_splitter_sizes = False

    def _pick_first_language_code(self, langs) -> str:
        if isinstance(langs, (list, tuple)):
            for item in langs:
                code = str(item or '').strip()
                if code and code.lower() != 'und':
                    return code
        if isinstance(langs, str):
            for piece in re.split(r'[,;\s]+', langs):
                code = str(piece or '').strip()
                if code and code.lower() != 'und':
                    return code
        return ''

    def _read_external_metadata(self, path: str) -> dict:
        path = os.path.abspath(str(path or '').strip())
        if not path or not os.path.exists(path):
            return {}
        try:
            stat = os.stat(path)
            cache_key = '%s|%s|%s' % (path.lower(), int(getattr(stat, 'st_mtime', 0) or 0), int(getattr(stat, 'st_size', 0) or 0))
        except Exception:
            cache_key = path.lower()
        cached = self._external_metadata_cache.get(cache_key)
        if cached is not None:
            return dict(cached)
        info = {
            'path': path,
            'title': os.path.splitext(os.path.basename(path))[0],
            'authors': [],
            'languages': (),
            'cover_bytes': None,
            'format': os.path.splitext(path)[1].lower().lstrip('.').upper(),
            'metadata_source': '',
        }
        try:
            from calibre.ebooks.metadata.meta import metadata_from_formats
            mi = metadata_from_formats([path], force_read_metadata=True)
            if mi is not None:
                title = getattr(mi, 'title', None) or info['title']
                authors = list(getattr(mi, 'authors', None) or [])
                languages = tuple(getattr(mi, 'languages', None) or ())
                cover_data = getattr(mi, 'cover_data', (None, None)) or (None, None)
                cover_bytes = cover_data[1] if len(cover_data) > 1 else None
                info.update({
                    'title': title,
                    'authors': authors,
                    'languages': languages,
                    'cover_bytes': cover_bytes,
                    'metadata_source': _('File metadata'),
                })
        except Exception:
            try:
                from calibre.ebooks.metadata.meta import get_metadata
                ext = os.path.splitext(path or '')[1].lower().lstrip('.')
                with open(path, 'rb') as stream:
                    mi = get_metadata(stream, stream_type=ext, use_libprs_metadata=True, force_read_metadata=True)
                if mi is not None:
                    title = getattr(mi, 'title', None) or info['title']
                    authors = list(getattr(mi, 'authors', None) or [])
                    languages = tuple(getattr(mi, 'languages', None) or ())
                    cover_data = getattr(mi, 'cover_data', (None, None)) or (None, None)
                    cover_bytes = cover_data[1] if len(cover_data) > 1 else None
                    info.update({
                        'title': title,
                        'authors': authors,
                        'languages': languages,
                        'cover_bytes': cover_bytes,
                        'metadata_source': _('File metadata'),
                    })
            except Exception:
                pass
        self._external_metadata_cache = {cache_key: dict(info)}
        return dict(info)

    def _detect_language_from_path(self, path: str) -> str:
        try:
            return self._pick_first_language_code(self._read_external_metadata(path).get('languages') or ())
        except Exception:
            return ''

    def detect_source_language_info(self) -> tuple[str, str, str]:
        external = self.external_path.text().strip()
        if external and os.path.exists(external):
            code = self._detect_language_from_path(external)
            if code:
                return code, display_language_name(code), _('File metadata')
            return '', '', _('Unknown')
        details = self.selected_book_details()
        if details is not None:
            code = self._pick_first_language_code(details.get('languages') or ())
            if code:
                return code, display_language_name(code), _('Library metadata')
        return '', '', _('Unknown')

    def _on_external_path_changed(self, _text: str = ''):
        self.refresh_selected_book()

    def refresh_library_selection(self):
        external = self.external_path.text().strip()
        if external:
            try:
                self.external_path.blockSignals(True)
                self.external_path.clear()
            finally:
                try:
                    self.external_path.blockSignals(False)
                except Exception:
                    pass
        self.refresh_selected_book()

    def _on_save_clicked(self):
        self.save_result()

    def update_source_summary_label(self):
        external = self.external_path.text().strip()
        rich_text = None
        if external and os.path.exists(external):
            summary = _('External file active')
            detail = os.path.basename(external)
        elif external:
            summary = _('External file missing')
            detail = os.path.basename(external)
        else:
            details = self.selected_book_details()
            if details is None:
                summary = _('Using selected calibre book')
                detail = _('No active source')
            elif details.get('path'):
                summary = _('Using calibre library file')
                detail = '%s • %s' % (details.get('format') or '', details.get('title') or '')
            else:
                summary = _('Using calibre library record')
                detail = '%s • %s' % (_('No ebook format'), details.get('title') or '')
                # Multi-file mode: reflect multi-selection in the source header
            if details is not None and pref_bool('multi_file_enabled', False):
                all_books = self.selected_books_details_list()
                if len(all_books) > 1:
                    summary = _('Using calibre library selection')
                    batch_text = _('Batch: %d books selected') % len(all_books)
                    detail = batch_text
                    rich_text = '%s — <span style="text-decoration: underline;">%s</span>' % (
                        _html_escape(summary),
                        _html_escape(batch_text),
                    )
        text = '%s — %s' % (summary, detail)
        if rich_text is None:
            try:
                fm = self.source_summary_label.fontMetrics()
                text = fm.elidedText(text, Qt.TextElideMode.ElideRight, max(10, int(self.source_summary_label.width() or 0)))
            except Exception:
                pass
            self.source_summary_label.setText(text)
        else:
            self.source_summary_label.setText(rich_text)
        # Build tooltip: show resolved path, and multi-file list when enabled
        resolved_path = self.selected_path_label.text().strip()
        tooltip_lines = []
        if external:
            tooltip_lines.append(external)
        elif resolved_path:
            tooltip_lines.append(resolved_path)
        else:
            tooltip_lines.append('%s — %s' % (summary, detail))
        # Multi-file tooltip
        if pref_bool('multi_file_enabled', False):
            all_books = self.selected_books_details_list()
            if len(all_books) > 1:
                tooltip_lines.append('')
                tooltip_lines.append(_('Multi-file batch (%d books):') % len(all_books))
                for idx, bk in enumerate(all_books[:20], 1):
                    fmt = bk.get('format') or _('No ebook format')
                    tooltip_lines.append('  %d. %s (%s)' % (idx, bk.get('title', '?'), fmt))
                if len(all_books) > 20:
                    tooltip_lines.append('  … ' + _('and %d more') % (len(all_books) - 20))
        self.source_summary_label.setToolTip('\n'.join(tooltip_lines))
        code, lang_name, lang_source = self.detect_source_language_info()
        target_code = normalize_target_language(self.target_language.currentData() or 'same-as-source')
        if code:
            source_part = '<span style="font-weight:700; color:%s;">%s</span> <span style="color:%s;">— %s</span>' % (
                html.escape(self.palette().color(QPalette.ColorRole.Link).name()),
                _html_escape(lang_name),
                html.escape(self.palette().color(QPalette.ColorRole.Mid).name()),
                _html_escape(lang_source),
            )
        else:
            source_part = '<span style="font-weight:700; color:%s;">%s</span> <span style="color:%s;">— %s</span>' % (
                html.escape(self.palette().color(QPalette.ColorRole.Link).name()),
                _html_escape(_('Unknown')),
                html.escape(self.palette().color(QPalette.ColorRole.Mid).name()),
                _html_escape(_('no metadata detected')),
            )
        if target_code == 'same-as-source':
            target_text = _('Same as source')
        else:
            target_text = display_language_name(target_code)
        target_foreground = '#f59e0b' if _is_dark_widget_theme(self) else '#c46a00'
        target_value = '<span style="font-weight:700; color:%s;">%s</span>' % (
            target_foreground,
            _html_escape(target_text),
        )
        target_part = '%s <span style="font-weight:600;">%s</span>' % (
            _html_escape(_('Summary output:')),
            target_value,
        )
        text = source_part + ' &nbsp;&nbsp;•&nbsp;&nbsp; ' + target_part
        plain_tooltip = ((lang_name + ' — ' + lang_source) if code else (_('Unknown') + ' — ' + _('no metadata detected'))) + '\n' + (_('Summary output: %s') % target_text)
        self.source_language_label.setText(text)
        self.source_language_label.setToolTip(plain_tooltip)

    def _switch_library_and_queue_action(self, target_library_path: str, action: str, book_id: int = None,
                                         source_book_id_raw: str = '', profile_key: str = '', source_path: str = '', source_title: str = ''):
        """Switch to a different library and queue an action to execute after the switch."""
        if not target_library_path:
            _cs_debug_log(f'_switch_library_and_queue_action: empty target path for action={action!r}')
            return False
        target_library_path = str(target_library_path).strip()
        if not target_library_path:
            _cs_debug_log(f'_switch_library_and_queue_action: blank target path after strip for action={action!r}')
            return False
        target_library_path = _canonical_library_path(target_library_path)
        self._pending_library_action = {
            'target_path': target_library_path,
            'action': action,
            'book_id': book_id,
            'source_book_id_raw': str(source_book_id_raw or '').strip(),
            'profile_key': profile_key,
            'source_path': source_path,
            'source_title': source_title,
        }
        _cs_debug_log(
            '_switch_library_and_queue_action: queued '
            f'action={action!r} target={target_library_path!r} book_id={book_id!r} '
            f'raw_source_book_id={str(source_book_id_raw or "").strip()!r} '
            f'profile_key={str(profile_key or "").strip()!r} source_path={str(source_path or "").strip()!r}'
        )
        try:
            chooser = getattr(self.gui, 'iactions', {}).get('Choose Library', None)
            if chooser is None:
                self._pending_library_action = None
                _cs_debug_log('_switch_library_and_queue_action: Choose Library action unavailable')
                error_dialog(self, _('Library switch failed'), _('Could not access calibre library switching.'), show=True)
                return False
            chooser.switch_requested(target_library_path)
            _cs_debug_log(f'_switch_library_and_queue_action: switch_requested sent target={target_library_path!r}')
            return True
        except Exception as e:
            self._pending_library_action = None
            _cs_debug_log(f'_switch_library_and_queue_action: switch request failed error={str(e)!r}')
            error_dialog(self, _('Library switch failed'), str(e), show=True)
            return False

    def _run_pending_library_action(self):
        """Execute the queued action after library switch completes."""
        action_data = self._pending_library_action
        if not action_data:
            return
        self._pending_library_action = None

        action = action_data.get('action')
        source_path = action_data.get('source_path', '')
        source_title = action_data.get('source_title', '')
        book_id = action_data.get('book_id')
        source_book_id_raw = str(action_data.get('source_book_id_raw', '') or '').strip()
        retry_count = int(action_data.get('retry_count') or 0)

        current_lib = ''
        try:
            current_lib = _canonical_library_path(getattr(self.gui.current_db, 'library_path', '') or '')
        except Exception:
            current_lib = ''
        _cs_debug_log(
            '_run_pending_library_action: start '
            f'action={action!r} current_library={current_lib!r} book_id={book_id!r} '
            f'raw_source_book_id={source_book_id_raw!r} source_path={str(source_path or "").strip()!r} '
            f'source_title={str(source_title or "").strip()!r}'
        )

        db = getattr(getattr(self, 'gui', None), 'current_db', None)
        db = getattr(db, 'new_api', db) if db is not None else None
        if (book_id is None or book_id == '') and source_path:
            try:
                book_id = _resolve_cached_book_id(source_path, source_title, source_book_id_raw, db=db)
            except Exception:
                book_id = None
        _cs_debug_log(f'_run_pending_library_action: resolved book_id={book_id!r}')

        if action == 'locate':
            # Show book in library view
            if book_id is not None:
                try:
                    selected = False
                    if hasattr(self, '_select_library_book') and callable(self._select_library_book):
                        selected = bool(self._select_library_book(book_id, preserve_selection=False, refresh_dialog=False))
                    if not selected:
                        self.gui.library_view.select_rows([book_id], using_ids=True)
                        try:
                            selected_ids = [int(x) for x in list(self.gui.library_view.get_selected_ids())]
                        except Exception:
                            selected_ids = []
                        selected = bool(selected_ids) and book_id in selected_ids
                    _cs_debug_log(f'_run_pending_library_action: locate selection selected={selected!r} book_id={book_id!r}')
                    if not selected and retry_count < 5:
                        action_data['book_id'] = book_id
                        action_data['retry_count'] = retry_count + 1
                        self._pending_library_action = action_data
                        _cs_debug_log(f'_run_pending_library_action: locate retry scheduled retry_count={retry_count + 1!r}')
                        QTimer.singleShot(200, self._run_pending_library_action)
                        return
                    if selected:
                        try:
                            self.refresh_selected_book()
                        except Exception:
                            pass
                    try:
                        self.gui.raise_()
                        self.gui.activateWindow()
                    except Exception:
                        pass
                except Exception:
                    _cs_debug_log(f'_run_pending_library_action: locate selection raised for book_id={book_id!r}')
            else:
                _cs_debug_log('_run_pending_library_action: locate has no resolved book_id after library switch')
        elif action == 'load_summary':
            # Load summary and select the book
            profile_key = action_data.get('profile_key', '')

            # Select the book in the library if we can resolve it in the switched DB.
            if book_id is not None:
                try:
                    selected = False
                    if hasattr(self, '_select_library_book') and callable(self._select_library_book):
                        selected = bool(self._select_library_book(book_id, preserve_selection=False, refresh_dialog=False))
                    if not selected:
                        self.gui.library_view.select_rows([book_id], using_ids=True)
                    _cs_debug_log(f'_run_pending_library_action: load_summary selection selected={selected!r} book_id={book_id!r}')
                    if not selected and retry_count < 5:
                        action_data['book_id'] = book_id
                        action_data['retry_count'] = retry_count + 1
                        self._pending_library_action = action_data
                        _cs_debug_log(f'_run_pending_library_action: load_summary retry scheduled retry_count={retry_count + 1!r}')
                        QTimer.singleShot(200, self._run_pending_library_action)
                        return
                except Exception:
                    pass
            else:
                _cs_debug_log('_run_pending_library_action: load_summary continuing without resolved book_id')

            # Give the UI time to process the selection before refreshing
            try:
                QApplication.processEvents()
            except Exception:
                pass

            # Refresh the dialog to show the correct book metadata
            try:
                self.refresh_selected_book()
            except Exception:
                pass

            # Load the summary
            if profile_key:
                try:
                    _cs_debug_log(f'_run_pending_library_action: loading cache profile profile_key={str(profile_key or "").strip()!r}')
                    self.load_summary_from_cache_profile(profile_key, source_path=source_path, source_title=source_title)
                except Exception as e:
                    _cs_debug_log(f'_run_pending_library_action: load_summary failed error={str(e)!r}')
                    error_dialog(self, _('Load summary'), _('Failed to load the cached summary after library switch.'), det_msg=str(e), show=True)

    def library_changed(self):
        """Called by calibre when the library changes."""
        action = self._pending_library_action
        if action:
            current_lib = ''
            try:
                current_lib = str(getattr(self.gui.current_db, 'library_path', '') or '').strip()
            except Exception:
                pass
            target_lib = action.get('target_path', '')
            current_lib = _canonical_library_path(current_lib)
            target_lib = _canonical_library_path(target_lib)
            _cs_debug_log(
                'library_changed: '
                f'current_library={current_lib!r} target_library={target_lib!r} '
                f'action={str(action.get("action") or "")!r}'
            )
            if current_lib and target_lib and _same_library_path(current_lib, target_lib):
                # Library switched successfully, run the pending action after a brief delay.
                _cs_debug_log('library_changed: target library active, scheduling pending action')
                QTimer.singleShot(500, self._run_pending_library_action)
            else:
                _cs_debug_log('library_changed: pending action retained, target library not active yet')
        # Update db reference
        self._ensure_current_db()

    def refresh_selected_book(self):
        _perf_started = time.monotonic()
        self._source_cache_matches_path = ''
        self._source_cache_matches = None
        self._ensure_current_db()
        # Reset status when switching sources so stale "Summary complete" doesn't linger
        self.status_label.setText(_('Ready'))
        current_source_path = self._current_source_path_for_chapter_selection()
        if self._chapter_selection_source_path and current_source_path != self._chapter_selection_source_path:
            self._clear_chapter_selection()
        self._last_book_id = None
        self.update_effective_model_label()
        _details_started = time.monotonic()
        details = self.selected_book_details()
        _cs_perf_log('refresh_selected_book: selected_book_details in %.1f ms; found=%s' % (_elapsed_ms(_details_started), bool(details)))
        external = self.external_path.text().strip()
        external_active = bool(external)
        _external_started = time.monotonic()
        external_info = self._read_external_metadata(external) if external_active and os.path.exists(external) else {}
        if external_active:
            _cs_perf_log('refresh_selected_book: external metadata in %.1f ms; found=%s' % (_elapsed_ms(_external_started), bool(external_info)))
        if details is None and not external_info:
            try:
                self.auto_add_to_data_files.setEnabled(False)
            except Exception:
                pass
            try:
                self.view_button.setEnabled(False)
                self._set_book_navigation_enabled(False)
            except Exception:
                pass
            self.selected_book_label.setText(_('No calibre book selected'))
            if external:
                self.selected_path_label.setText(external)
            else:
                self.selected_path_label.setText(_('Using the external file override, if provided'))
            self._set_cover_pixmap(None)
            try:
                self.cover_meta_label.setText('')
            except Exception:
                pass
            self.update_source_summary_label()
            self._update_toc_buttons()
            self._update_chapter_selection_badge()
            self._schedule_lazy_cache_population(external if external and os.path.exists(external) else '')
            self._schedule_splitter_adjustment()
            _cs_perf_log('refresh_selected_book total %.1f ms (no active book)' % _elapsed_ms(_perf_started))
            return
        self._last_book_id = details['book_id'] if details is not None else None
        try:
            self.auto_add_to_data_files.setEnabled(bool(details is not None and not external_active))
        except Exception:
            pass
        try:
            self.view_button.setEnabled(bool(details is not None and details.get('path')))
            self._set_book_navigation_enabled(details is not None)
        except Exception:
            pass
        if external_info:
            title_line = '%s (%s)' % (external_info.get('title') or os.path.splitext(os.path.basename(external))[0], external_info.get('format') or os.path.splitext(external)[1].lower().lstrip('.').upper())
            authors = ', '.join(external_info.get('authors') or []) or _('Unknown author')
            meta_line = _('External file • Authors: %s') % authors
        else:
            fmt = str(details.get('format') or '').strip()
            title_line = "%s (%s)" % (details['title'], fmt) if fmt else str(details['title'])
            meta_line = str(details.get('meta', '') or '')
        try:
            fm = self.selected_book_label.fontMetrics()
            title_line = fm.elidedText(title_line, Qt.TextElideMode.ElideRight, max(10, int(self.selected_book_label.width() or 0)))
            meta_line = fm.elidedText(meta_line, Qt.TextElideMode.ElideRight, max(10, int(self.selected_book_label.width() or 0)))
        except Exception:
            pass
        merged = title_line + "\n" + meta_line
        if details is not None and pref_bool('multi_file_enabled', False):
            all_books = self.selected_books_details_list()
            if len(all_books) > 1 and not external_active:
                merged = _('%d books selected') % len(all_books)
        self.selected_book_label.setText(merged)
        if external:
            self.selected_path_label.setText(external)
        elif details.get('path'):
            self.selected_path_label.setText(details['path'])
        elif details.get('book_folder_path'):
            self.selected_path_label.setText(_('No ebook format — book folder: %s') % details.get('book_folder_path'))
        else:
            self.selected_path_label.setText(_('No ebook format in this library record'))
        if external_info:
            _cover_started = time.monotonic()
            self._set_cover_pixmap(external_info.get('cover_bytes'))
            _cs_perf_log('refresh_selected_book: _set_cover_pixmap external in %.1f ms; cover_bytes=%d' % (_elapsed_ms(_cover_started), len(external_info.get('cover_bytes') or b'')))
        elif external_active:
            self._set_cover_pixmap(None)
            try:
                self.cover_meta_label.setText('')
            except Exception:
                pass
        else:
            _cover_started = time.monotonic()
            self._set_cover_pixmap(details.get('cover_bytes'))
            _cs_perf_log('refresh_selected_book: _set_cover_pixmap in %.1f ms; cover_bytes=%d' % (_elapsed_ms(_cover_started), len(details.get('cover_bytes') or b'')))
        self.update_source_summary_label()
        self._update_toc_buttons()
        _cover_meta_started = time.monotonic()
        self._refresh_cover_meta()
        _cs_perf_log('refresh_selected_book: _refresh_cover_meta in %.1f ms' % _elapsed_ms(_cover_meta_started))
        self._update_chapter_selection_badge()
        self._schedule_lazy_cache_population(external if external_active and os.path.exists(external) else details.get('path', '') if details is not None else '')
        self._schedule_splitter_adjustment()
        _cs_perf_log('refresh_selected_book total %.1f ms' % _elapsed_ms(_perf_started))

    def _set_book_navigation_enabled(self, enabled: bool):
        for button in (
            getattr(self, 'first_book_button', None),
            getattr(self, 'prev_book_button', None),
            getattr(self, 'next_book_button', None),
            getattr(self, 'last_book_button', None),
        ):
            if button is not None:
                button.setEnabled(bool(enabled))

    def _library_view_book_ids(self) -> list[int]:
        view = getattr(self.gui, 'library_view', None)
        if view is None:
            return []
        try:
            model = view.model()
        except Exception:
            model = None
        if model is None or not hasattr(model, 'db') or not hasattr(model.db, 'id'):
            return []
        try:
            row_count = int(model.rowCount())
        except Exception:
            row_count = 0
        book_ids: list[int] = []
        for row in range(max(0, row_count)):
            try:
                book_id = model.db.id(row)
            except Exception:
                book_id = None
            if book_id is None:
                continue
            try:
                book_ids.append(int(book_id))
            except Exception:
                continue
        return book_ids

    def _refresh_navigation_book_ids(self):
        """Mirror OPF-Helper: cache visible library view ids + current index."""
        view = getattr(self.gui, 'library_view', None)
        if view is None:
            self._nav_book_ids = []
            self._nav_current_index = 0
            return

        # In multi-file batch mode, restrict navigation to the selected books.
        if pref_bool('multi_file_enabled', False) and not self.external_path.text().strip():
            try:
                selected_ids = [int(x) for x in list(view.get_selected_ids())]
            except Exception:
                selected_ids = []
            if len(selected_ids) > 1:
                # Keep the selected ids in the current view order so wrap-around is predictable.
                try:
                    model = view.model()
                    row_count = model.rowCount()
                    ordered = []
                    selected_set = set(selected_ids)
                    for row in range(int(row_count)):
                        try:
                            book_id = int(model.id(row))
                        except Exception:
                            continue
                        if book_id in selected_set:
                            ordered.append(book_id)
                    self._nav_book_ids = ordered or selected_ids
                except Exception:
                    self._nav_book_ids = selected_ids
                current_book_id = self._current_library_book_id()
                if current_book_id is not None and self._nav_book_ids:
                    try:
                        self._nav_current_index = self._nav_book_ids.index(int(current_book_id))
                    except ValueError:
                        self._nav_current_index = 0
                else:
                    self._nav_current_index = 0
                return
        try:
            model = view.model()
        except Exception:
            model = None
        if model is None or not hasattr(model, 'id'):
            self._nav_book_ids = []
            self._nav_current_index = 0
            return
        try:
            row_count = model.rowCount()
        except TypeError:
            try:
                from qt.core import QModelIndex
                row_count = model.rowCount(QModelIndex())
            except Exception:
                row_count = 0
        try:
            self._nav_book_ids = [model.id(row) for row in range(int(row_count))]
        except Exception:
            self._nav_book_ids = []
        current_book_id = self._current_library_book_id()
        if current_book_id is not None and self._nav_book_ids:
            try:
                self._nav_current_index = self._nav_book_ids.index(current_book_id)
            except ValueError:
                self._nav_current_index = 0
        else:
            self._nav_current_index = 0

    def _book_has_supported_format(self, book_id: int) -> bool:
        supported_formats = ('EPUB', 'KEPUB', 'AZW3', 'MOBI', 'HTMLZ')
        try:
            db = self.gui.current_db.new_api
            return any(db.has_format(book_id, fmt) for fmt in supported_formats)
        except Exception:
            pass
        try:
            model = self.gui.library_view.model()
            db = getattr(model, 'db', None)
            if db is not None and hasattr(db, 'formats'):
                formats = str(db.formats(book_id, index_is_id=True) or '')
                available = {fmt.strip().upper() for fmt in formats.split(',') if fmt.strip()}
                return any(fmt in available for fmt in supported_formats)
        except Exception:
            pass
        path, _fmt = self.resolve_book_path(book_id)
        return bool(path)

    def _current_library_book_id(self) -> int | None:
        details = self.selected_book_details()
        if details is not None:
            try:
                return int(details.get('book_id'))
            except Exception:
                pass
        return None

    def _select_library_book(self, book_id: int, preserve_selection: bool | None = None, refresh_dialog: bool = True) -> bool:
        view = getattr(self.gui, 'library_view', None)
        if view is None:
            return False
        preserve_existing_selection = False
        preserved_ids: list[int] = []
        if preserve_selection is None and pref_bool('multi_file_enabled', False) and not self.external_path.text().strip():
            try:
                preserved_ids = [int(x) for x in list(view.get_selected_ids())]
            except Exception:
                preserved_ids = []
            preserve_existing_selection = len(preserved_ids) > 1
        elif preserve_selection is not None:
            preserve_existing_selection = bool(preserve_selection)
        try:
            self.gui.show_library_view()
        except Exception:
            pass
        if not preserve_existing_selection:
            try:
                view.clearSelection()
            except Exception:
                pass
            try:
                selection_model = view.selectionModel()
                if selection_model is not None:
                    selection_model.clearSelection()
            except Exception:
                pass
        try:
            view.select_rows([book_id], using_ids=True)
        except Exception:
            return False
        if preserve_existing_selection:
            try:
                # Restore the multi-selection set after moving the current row.
                view.select_rows(preserved_ids, using_ids=True)
            except Exception:
                pass
        try:
            model = view.model()
            if model is not None and hasattr(model, 'db') and hasattr(model.db, 'row'):
                row = model.db.row(book_id)
                if row is not None and row >= 0:
                    idx = model.index(row, 0)
                    if idx.isValid():
                        try:
                            view.setCurrentIndex(idx)
                        except Exception:
                            pass
        except Exception:
            pass
        try:
            view.setFocus(Qt.FocusReason.OtherFocusReason)
        except Exception:
            try:
                view.setFocus()
            except Exception:
                pass
        try:
            QApplication.processEvents()
        except Exception:
            pass
        current_book_id = None
        try:
            current_idx = view.currentIndex()
            model = view.model()
            if current_idx.isValid() and model is not None and hasattr(model, 'db') and hasattr(model.db, 'id'):
                current_book_id = model.db.id(current_idx.row())
        except Exception:
            current_book_id = None
        try:
            selected_ids = [int(x) for x in list(view.get_selected_ids())]
        except Exception:
            selected_ids = []
        selected_ok = bool(selected_ids) and int(book_id) in selected_ids
        current_ok = current_book_id is not None and int(current_book_id) == int(book_id)
        verified = bool(current_ok or (current_book_id is None and selected_ok))
        _cs_debug_log(
            f'_select_library_book: target={int(book_id)!r} current={current_book_id!r} '
            f'selected_ids={selected_ids!r} preserve_selection={preserve_existing_selection!r} verified={verified!r}'
        )
        if verified and refresh_dialog:
            self.refresh_selected_book()
        return verified

    def _navigate_library_books(self, direction: str) -> bool:
        """Mirror OPF-Helper navigation semantics."""
        self._refresh_navigation_book_ids()
        book_ids = list(getattr(self, '_nav_book_ids', []) or [])
        if not book_ids:
            return False

        supported_formats = ('KEPUB', 'EPUB', 'HTMLZ', 'AZW3', 'MOBI')

        def has_supported(book_id: int) -> bool:
            try:
                return any(self.db.has_format(book_id, fmt) for fmt in supported_formats)
            except Exception:
                try:
                    na = self.gui.current_db.new_api
                    return any(na.has_format(book_id, fmt) for fmt in supported_formats)
                except Exception:
                    return False

        if direction == 'first':
            for i, book_id in enumerate(book_ids):
                if has_supported(book_id):
                    self._nav_current_index = i
                    return self._select_library_book(book_id)
            return False

        if direction == 'last':
            for i in range(len(book_ids) - 1, -1, -1):
                book_id = book_ids[i]
                if has_supported(book_id):
                    self._nav_current_index = i
                    return self._select_library_book(book_id)
            return False

        books_checked = 0
        while books_checked < len(book_ids):
            if direction == 'next':
                self._nav_current_index = (int(self._nav_current_index) + 1) % len(book_ids)
            else:
                self._nav_current_index = (int(self._nav_current_index) - 1) % len(book_ids)
            books_checked += 1
            book_id = book_ids[int(self._nav_current_index)]
            if has_supported(book_id):
                return self._select_library_book(book_id)
        return False

    def show_first_book(self):
        if not self._navigate_library_books('first'):
            self.gui.status_bar.showMessage(_('No compatible books found in the current library view.'), 5000)

    def show_previous_book(self):
        if not self._navigate_library_books('previous'):
            self.gui.status_bar.showMessage(_('No compatible books found in the current library view.'), 5000)

    def show_next_book(self):
        if not self._navigate_library_books('next'):
            self.gui.status_bar.showMessage(_('No compatible books found in the current library view.'), 5000)

    def show_last_book(self):
        if not self._navigate_library_books('last'):
            self.gui.status_bar.showMessage(_('No compatible books found in the current library view.'), 5000)

    def open_plugin_config_dialog(self):
        d = ChapterSummarizerConfigDialog(self, icon=self.icon)
        d.exec()
        # Return focus to this window after the config dialog closes
        try:
            self.show()
            self.raise_()
            self.activateWindow()
        except Exception:
            pass

    def _current_source_path_for_chapter_selection(self) -> str:
        ext_path = self.external_path.text().strip()
        if ext_path and os.path.exists(ext_path):
            return ext_path
        details = self.selected_book_details()
        if details is not None:
            path = str(details.get('path', '') or '')
            if path and os.path.exists(path):
                return path
            return path
        return ''

    def _clear_chapter_selection(self):
        self._chapter_selection_ids = None
        self._chapter_selection_total = 0
        self._chapter_selection_source_path = ''

    def _update_chapter_selection_badge(self):
        badge = ''
        if self._chapter_selection_ids is not None:
            total = int(self._chapter_selection_total or 0)
            selected = len(self._chapter_selection_ids)
            if total > 0:
                badge = _('Chapters: %d/%d selected') % (selected, total)
            else:
                badge = _('Chapters: %d selected') % selected
        try:
            self.chapter_selection_badge.setText(badge)
        except Exception:
            pass

    def _cache_callout_stylesheet(self, has_cache: bool) -> str:
        try:
            app = QApplication.instance()
            palette = app.palette() if app else None
            if palette is not None:
                wc = palette.color(QPalette.ColorRole.Window)
                tc = palette.color(QPalette.ColorRole.WindowText)
                dark = tc.lightness() > wc.lightness()
            else:
                dark = False
        except Exception:
            dark = False
        if dark:
            bg = '#2b2b2b'
            border = '#555555'
            fg = '#90EE90' if has_cache else '#c8ccd2'
        else:
            bg = '#f0f0f0'
            border = '#cccccc'
            fg = '#006400' if has_cache else '#5a5a5a'
        weight = 'bold' if has_cache else 'normal'
        return (
            'background-color: {0}; border: 1px solid {1}; border-radius: 4px; '
            'padding: 4px 8px; color: {2}; font-weight: {3}; font-size: 8pt;'
        ).format(bg, border, fg, weight)

    def _refresh_book_details_panel(self, book_id):
        """Trigger the calibre book details panel to refresh for the given book."""
        try:
            model = self.gui.library_view.model()
            if model is not None and hasattr(model, 'refresh_ids'):
                model.refresh_ids([book_id])
        except Exception:
            pass
        try:
            db = getattr(self.gui, 'current_db', None)
            if db is not None and hasattr(db, 'new_api'):
                db.new_api.refresh_book_details(book_id)
        except Exception:
            pass

    def _update_cache_callout(self, source_path: str = ''):
        """Refresh the cache callout label in the footer.

        Searches ALL cache DB files by source_path (not just the current
        profile key), so changing a setting no longer hides existing cache.
        """
        _perf_started = time.monotonic()
        try:
            if not pref_bool('chapter_cache_enabled', True):
                self.cache_callout_label.setVisible(False)
                _cs_perf_log('_update_cache_callout disabled in %.1f ms' % _elapsed_ms(_perf_started))
                return
            path = str(source_path or '').strip()
            if not path:
                path, _title = self._toc_source_path()
            if not path:
                self.cache_callout_label.setVisible(False)
                _cs_perf_log('_update_cache_callout no source in %.1f ms' % _elapsed_ms(_perf_started))
                return
            _find_started = time.monotonic()
            matches = self._cache_matches_for_source(path)
            _cs_perf_log('_update_cache_callout: cache lookup source=%r matches=%d in %.1f ms' % (os.path.basename(path), len(matches), _elapsed_ms(_find_started)))
            if matches:
                best = matches[0]
                count = int(best.get('chapter_count', 0))
                nodes_text = str(count) + (' ' + _('node') if count == 1 else ' ' + _('nodes'))
                text = _('Cache:') + ' ' + _('Yes') + ' — ' + nodes_text
                self.cache_callout_label.setText(text)
                self.cache_callout_label.setStyleSheet(self._cache_callout_stylesheet(True))
                tip_lines = [_('%d cached chapter summary(ies) found for this source') % count]
                if best.get('provider'):
                    tip_lines.append(_('Provider: %s') % best['provider'])
                if best.get('model'):
                    tip_lines.append(_('Model: %s') % best['model'])
                if best.get('updated_at'):
                    tip_lines.append(_('Last updated: %s') % best['updated_at'])
                if len(matches) > 1:
                    tip_lines.append(_('%d cached summary(ies) total for this source') % len(matches))
                self.cache_callout_label.setToolTip('\n'.join(tip_lines))
            else:
                self.cache_callout_label.setText(_('Cache:') + ' ' + _('None'))
                self.cache_callout_label.setStyleSheet(self._cache_callout_stylesheet(False))
                self.cache_callout_label.setToolTip(_('No cached summaries found for this source'))
            self.cache_callout_label.setVisible(True)
            _cs_perf_log('_update_cache_callout total %.1f ms' % _elapsed_ms(_perf_started))
        except Exception:
            self.cache_callout_label.setVisible(False)
            _cs_perf_log('_update_cache_callout failed in %.1f ms' % _elapsed_ms(_perf_started))

    def _set_cache_callout_checking(self):
        try:
            self.cache_callout_label.setText(_('Cache:') + ' ' + _('Checking...'))
            self.cache_callout_label.setStyleSheet(self._cache_callout_stylesheet(False))
            self.cache_callout_label.setToolTip(_('Checking cached summaries for this source'))
            self.cache_callout_label.setVisible(True)
        except Exception:
            pass

    def _schedule_lazy_cache_population(self, source_path: str, restore_tabs: bool = True):
        self._lazy_cache_population_token += 1
        token = int(self._lazy_cache_population_token)
        source_path = str(source_path or '').strip()
        if not source_path:
            self._update_cache_callout('')
            if restore_tabs:
                self._restore_cached_summary_tabs('', preferred_profile_key='')
            return
        if pref_bool('chapter_cache_enabled', True):
            self._set_cache_callout_checking()
        QTimer.singleShot(0, lambda p=source_path, t=token, r=restore_tabs: self._run_lazy_cache_population(p, t, r))

    def _run_lazy_cache_population(self, source_path: str, token: int, restore_tabs: bool):
        if int(token or 0) != int(getattr(self, '_lazy_cache_population_token', 0) or 0):
            return
        _perf_started = time.monotonic()
        try:
            self._update_cache_callout(source_path)
            if restore_tabs:
                _restore_started = time.monotonic()
                self._restore_cached_summary_tabs(source_path)
                _cs_perf_log('lazy cache population: _restore_cached_summary_tabs in %.1f ms' % _elapsed_ms(_restore_started))
            _cs_perf_log('lazy cache population total %.1f ms' % _elapsed_ms(_perf_started))
        except Exception:
            _cs_perf_log('lazy cache population failed in %.1f ms' % _elapsed_ms(_perf_started))

    def _cache_matches_for_source(self, source_path: str) -> list[dict]:
        source_path = str(source_path or '').strip()
        if not source_path:
            return []
        try:
            norm = os.path.normcase(os.path.abspath(source_path))
        except Exception:
            norm = source_path
        if norm and norm == self._source_cache_matches_path and self._source_cache_matches is not None:
            return list(self._source_cache_matches)
        matches = _safe_cache_find_by_source(source_path)
        self._source_cache_matches_path = norm
        self._source_cache_matches = list(matches)
        return matches

    def _clear_source_cache_matches(self):
        self._source_cache_matches_path = ''
        self._source_cache_matches = None

    def _refresh_cover_meta(self):
        _perf_started = time.monotonic()
        external = self.external_path.text().strip()
        if external:
            ext_info = self._read_external_metadata(external) if os.path.exists(external) else {}
            if not ext_info:
                self.cover_meta_label.setText('')
                return
            fmt = str(ext_info.get('format') or os.path.splitext(external)[1].lower().lstrip('.').upper() or '?')
            authors = ', '.join(ext_info.get('authors') or []) or _('Unknown author')
            bits = [
                _('External file'),
                _('Format: %s') % fmt,
                _('Authors: %s') % authors,
            ]
            try:
                size = int(os.path.getsize(external))
                from calibre import human_readable
                bits.append(_('Size: %s') % (human_readable(size) if callable(human_readable) else str(size)))
            except Exception:
                pass
            try:
                sz = getattr(self, '_cover_native_size', None)
                if sz is not None and hasattr(sz, 'width') and hasattr(sz, 'height') and sz.width() > 0 and sz.height() > 0:
                    cover_txt = _('Cover: ') + f'{int(sz.width())}×{int(sz.height())}px'
                    cb = int(getattr(self, '_cover_bytes_len', 0) or 0)
                    if cb > 0:
                        from calibre import human_readable
                        cover_txt += ' (%s)' % (human_readable(cb) if callable(human_readable) else str(cb))
                    bits.append(cover_txt)
            except Exception:
                pass
            self.cover_meta_label.setText('\n'.join(bits))
            try:
                self.cover_meta_label.setToolTip(external)
            except Exception:
                pass
            return
        details = self.selected_book_details()
        if details is None:
            self.cover_meta_label.setText('')
            return

        if pref_bool('multi_file_enabled', False):
            all_books = self.selected_books_details_list()
            if len(all_books) > 1:
                self.cover_meta_label.setText(_('%d books selected') % len(all_books))
                tip_lines = [_('Multi-file batch (%d books):') % len(all_books)]
                for idx, bk in enumerate(all_books[:50], 1):
                    tip_lines.append('  %d. %s (%s)  ID: %s' % (
                        idx,
                        bk.get('title', '?'),
                        bk.get('format', '?'),
                        str(bk.get('book_id', '')),
                    ))
                if len(all_books) > 50:
                    tip_lines.append('   ' + _('and %d more') % (len(all_books) - 50))
                self.cover_meta_label.setToolTip('\n'.join(tip_lines))
                return

        book_id = details.get('book_id', None)
        if book_id is None:
            self.cover_meta_label.setText('')
            return

        zwsp = '\u200b'

        def try_field(field_name: str):
            try:
                return self.db.field_for(field_name, book_id, default_value=None)
            except TypeError:
                try:
                    return self.db.field_for(field_name, book_id)
                except Exception:
                    return None
            except Exception:
                return None

        pages = try_field('pages')
        words = try_field('#words')
        gfi = try_field('#gfog')
        if gfi in (None, ''):
            gfi = try_field('#gfi')

        bits: list[str] = []

        if pages not in (None, '', 0, '0'):
            bits.append(f'{zwsp}📈 ' + _('Pages: ') + str(pages))

        try:
            from calibre import human_readable
        except Exception:
            human_readable = None

        cover_line = ''
        try:
            sz = getattr(self, '_cover_native_size', None)
            if sz is not None and hasattr(sz, 'width') and hasattr(sz, 'height') and sz.width() > 0 and sz.height() > 0:
                w, h = int(sz.width()), int(sz.height())
                cover_txt = _('Cover: ') + f'{w}×{h}px'
                cb = int(getattr(self, '_cover_bytes_len', 0) or 0)
                if cb > 0:
                    cb_txt = human_readable(cb) if callable(human_readable) else str(cb)
                    cover_txt += f' ({cb_txt})'
                cover_line = f'{zwsp}🖼 ' + cover_txt
        except Exception:
            pass

        chosen_path = ''
        chosen_fmt = ''
        try:
            book_id = details.get('book_id', None)
            if book_id is not None:
                chosen_path, chosen_fmt = self.resolve_book_path(book_id)
        except Exception:
            chosen_path = ''
            chosen_fmt = ''
        try:
            if chosen_path and os.path.exists(chosen_path):
                norm_chosen = os.path.normcase(os.path.abspath(chosen_path))
                if norm_chosen == str(getattr(self, '_toc_nodes_path', '') or '') and self._toc_nodes_count not in (None, '', 0, '0'):
                    bits.append(f'{zwsp}🧭 ' + _('ToC nodes: ') + str(self._toc_nodes_count))
                else:
                    self._schedule_lazy_toc_node_count(chosen_path)
        except Exception:
            pass
        if words not in (None, '', 0, '0'):
            try:
                words_txt = f'{int(words):,}'
            except Exception:
                words_txt = str(words)
            bits.append(f'{zwsp}📈 ' + _('Words: ') + words_txt)
        if gfi not in (None, '', 0, '0'):
            try:
                gfi_num = float(gfi)
                gfi_txt = ('%.1f' % gfi_num).rstrip('0').rstrip('.') if abs(gfi_num - round(gfi_num)) > 1e-9 else str(int(round(gfi_num)))
            except Exception:
                gfi_txt = str(gfi)
            bits.append(f'{zwsp}📈 ' + _('GFI: ') + gfi_txt)

        # Show only the chosen input format + size (single line) to keep the callout height stable.
        try:
            if chosen_path and os.path.exists(chosen_path):
                size = int(os.path.getsize(chosen_path))
                if size > 0:
                    size_txt = human_readable(size) if callable(human_readable) else str(size)
                    bits.append(f'{zwsp}💾 {str(chosen_fmt or "").upper()}: {size_txt}')
        except Exception:
            pass
        if cover_line:
            bits.append(cover_line)

        label = ''
        if bits:
            label = '\n'.join(bits)
        self.cover_meta_label.setText(label)
        _cs_perf_log('_refresh_cover_meta total %.1f ms' % _elapsed_ms(_perf_started))

    def _schedule_lazy_toc_node_count(self, path: str):
        path = str(path or '').strip()
        if not path:
            return
        try:
            ext = os.path.splitext(path)[1].lower()
            if ext not in ('.epub', '.kepub', '.azw3'):
                return
            norm = os.path.normcase(os.path.abspath(path))
        except Exception:
            return
        if norm == str(getattr(self, '_toc_nodes_pending_path', '') or ''):
            return
        self._toc_nodes_token += 1
        token = int(self._toc_nodes_token)
        self._toc_nodes_pending_path = norm

        def worker():
            _started = time.monotonic()
            try:
                count = count_toc_nodes_for_path(path)
                _cs_perf_log('lazy ToC node count source=%r count=%r in %.1f ms' % (os.path.basename(path), count, _elapsed_ms(_started)))
                self._toc_count_signals.finished.emit({'token': token, 'path': norm, 'count': count})
            except Exception as e:
                self._toc_count_signals.error.emit(str(e), traceback.format_exc())

        Thread(target=worker, name='ChapterSummarizerTocCountWorker', daemon=True).start()

    def _on_lazy_toc_count_finished(self, payload):
        try:
            token = int((payload or {}).get('token') or 0)
            path = str((payload or {}).get('path') or '')
            count = (payload or {}).get('count')
        except Exception:
            return
        if token != int(getattr(self, '_toc_nodes_token', 0) or 0):
            return
        self._toc_nodes_pending_path = ''
        self._toc_nodes_path = path
        self._toc_nodes_count = count
        try:
            self._refresh_cover_meta()
        except Exception:
            pass

    def _on_lazy_toc_count_error(self, message: str, details: str):
        self._toc_nodes_pending_path = ''
        _cs_perf_log('lazy ToC node count failed: %s' % str(message or '').strip())

    def _ensure_current_db(self):
        try:
            self.db = getattr(self.gui.current_db, 'new_api', self.gui.current_db)
        except Exception:
            pass
        return self.db

    def _book_folder_path(self, book_id) -> str:
        current_db = getattr(self.gui, 'current_db', None)
        current_new_api = getattr(current_db, 'new_api', current_db)
        self._ensure_current_db()
        library_paths = []
        for db in (current_new_api, current_db, self.db):
            lib_path = str(getattr(db, 'library_path', '') or '').strip()
            if lib_path and lib_path not in library_paths:
                library_paths.append(lib_path)
        for db in (current_new_api, current_db, self.db):
            if db is None:
                continue
            try:
                folder = db.abspath(book_id, index_is_id=True)
                if folder and os.path.isdir(folder):
                    return folder
            except Exception:
                pass
            rel_path = ''
            try:
                rel_path = db.field_for('path', book_id, default_value='') or ''
            except TypeError:
                try:
                    rel_path = db.field_for('path', book_id) or ''
                except Exception:
                    rel_path = ''
            except Exception:
                rel_path = ''
            if not rel_path:
                try:
                    rel_path = db.path(book_id, index_is_id=True) or ''
                except Exception:
                    try:
                        rel_path = db.path(book_id) or ''
                    except Exception:
                        rel_path = ''
            rel_path = str(rel_path or '').strip()
            if not rel_path:
                continue
            if os.path.isabs(rel_path) and os.path.isdir(rel_path):
                return rel_path
            for lib_path in library_paths:
                folder = os.path.join(lib_path, rel_path)
                if os.path.isdir(folder):
                    return folder
        return ''

    def _cover_bytes_for_book(self, book_id, mi=None, book_folder_path: str = '') -> bytes | None:
        cover_data = getattr(mi, 'cover_data', (None, None)) if mi is not None else (None, None)
        try:
            cover_bytes = cover_data[1] if len(cover_data) > 1 else None
            if cover_bytes:
                return bytes(cover_bytes)
        except Exception:
            pass
        current_db = getattr(self.gui, 'current_db', None)
        current_new_api = getattr(current_db, 'new_api', current_db)
        self._ensure_current_db()
        for db in (current_new_api, current_db, self.db):
            if db is None or not hasattr(db, 'cover'):
                continue
            for kwargs in ({'index_is_id': True}, {}):
                try:
                    data = db.cover(book_id, **kwargs)
                except Exception:
                    continue
                if isinstance(data, (bytes, bytearray)) and data:
                    return bytes(data)
                if isinstance(data, (list, tuple)) and len(data) > 1 and data[1]:
                    try:
                        return bytes(data[1])
                    except Exception:
                        pass
                if isinstance(data, str) and os.path.exists(data):
                    try:
                        with open(data, 'rb') as f:
                            raw = f.read()
                        if raw:
                            return raw
                    except Exception:
                        pass
        cover_path = os.path.join(str(book_folder_path or ''), 'cover.jpg')
        if cover_path and os.path.exists(cover_path):
            try:
                with open(cover_path, 'rb') as f:
                    raw = f.read()
                if raw:
                    return raw
            except Exception:
                pass
        return None

    def selected_book_details(self):
        _perf_started = time.monotonic()
        self._ensure_current_db()
        view = getattr(self.gui, 'library_view', None)
        if view is None:
            return None
        book_id = None
        try:
            selection_model = view.selectionModel()
        except Exception:
            selection_model = None
        model = None
        try:
            model = view.model()
        except Exception:
            model = None
        try:
            current_idx = view.currentIndex()
            if current_idx.isValid() and model is not None and hasattr(model, 'db') and hasattr(model.db, 'id'):
                book_id = model.db.id(current_idx.row())
        except Exception:
            book_id = None
        if book_id is None and selection_model is not None:
            try:
                rows = list(selection_model.selectedRows())
            except Exception:
                rows = []
            if rows:
                row = rows[0].row()
                try:
                    if model is not None and hasattr(model, 'db') and hasattr(model.db, 'id'):
                        book_id = model.db.id(row)
                except Exception:
                    book_id = None
        if book_id is None:
            try:
                ids = list(view.get_selected_ids())
                if ids:
                    book_id = ids[0]
            except Exception:
                book_id = None
        if book_id is None:
            _cs_perf_log('selected_book_details: no book id in %.1f ms' % _elapsed_ms(_perf_started))
            return None
        _metadata_started = time.monotonic()
        mi = self._get_metadata(book_id, get_cover=True)
        _cs_perf_log('selected_book_details: _get_metadata book_id=%r get_cover=True in %.1f ms' % (book_id, _elapsed_ms(_metadata_started)))
        if mi is None:
            _cs_perf_log('selected_book_details: no metadata in %.1f ms' % _elapsed_ms(_perf_started))
            return None
        _path_started = time.monotonic()
        path, fmt = self.resolve_book_path(book_id)
        _cs_perf_log('selected_book_details: resolve_book_path in %.1f ms' % _elapsed_ms(_path_started))
        book_folder_path = self._book_folder_path(book_id)
        title = getattr(mi, 'title', None) or _('Untitled')
        authors = ', '.join(getattr(mi, 'authors', []) or []) or _('Unknown author')
        languages = tuple(getattr(mi, 'languages', None) or ())
        cover_bytes = self._cover_bytes_for_book(book_id, mi=mi, book_folder_path=book_folder_path)
        meta = _('ID: %d • Authors: %s') % (int(book_id), authors)
        if not path:
            _cs_perf_log('selected_book_details: no format path; using library metadata in %.1f ms' % _elapsed_ms(_perf_started))
        _cs_perf_log('selected_book_details total %.1f ms; cover_bytes=%d' % (_elapsed_ms(_perf_started), len(cover_bytes or b'')))
        return {'book_id': book_id, 'title': title, 'path': path, 'format': fmt, 'meta': meta, 'cover_bytes': cover_bytes, 'languages': languages, 'book_folder_path': book_folder_path}

    def selected_books_details_list(self) -> list[dict]:
        """Return details for ALL selected books in calibre (for multi-file mode)."""
        if not pref_bool('multi_file_enabled', False):
            d = self.selected_book_details()
            return [d] if d else []
        view = getattr(self.gui, 'library_view', None)
        if view is None:
            return []
        try:
            ids = list(view.get_selected_ids())
        except Exception:
            ids = []
        if not ids:
            d = self.selected_book_details()
            return [d] if d else []
        result = []
        for book_id in ids:
            try:
                mi = self._get_metadata(book_id, get_cover=True)
                path, fmt = self.resolve_book_path(book_id)
                if not path:
                    continue
                title = getattr(mi, 'title', None) or os.path.splitext(os.path.basename(path))[0]
                authors_list = getattr(mi, 'authors', []) or []
                authors = ', '.join(authors_list) or _('Unknown author')
                languages = tuple(getattr(mi, 'languages', None) or ())
                cover_data = getattr(mi, 'cover_data', (None, None)) or (None, None)
                cover_bytes = cover_data[1] if len(cover_data) > 1 else None
                meta = _('ID: %d • Authors: %s') % (int(book_id), authors)
                result.append({'book_id': book_id, 'title': title, 'path': path, 'format': fmt, 'meta': meta, 'cover_bytes': cover_bytes, 'languages': languages})
            except Exception:
                continue
        return result

    def _get_metadata(self, book_id, get_cover: bool = False):
        current_db = getattr(self.gui, 'current_db', None)
        current_new_api = getattr(current_db, 'new_api', current_db)
        self._ensure_current_db()
        for db in (current_new_api, current_db, self.db):
            if db is None:
                continue
            try:
                return db.get_metadata(book_id, get_cover=get_cover, cover_as_data=get_cover)
            except TypeError:
                try:
                    return db.get_metadata(book_id, index_is_id=True, get_cover=get_cover, cover_as_data=get_cover)
                except Exception:
                    pass
            except Exception:
                pass
        return None

    def _set_cover_pixmap(self, cover_bytes):
        self._cover_native_size = None
        self._cover_bytes_len = 0
        if not cover_bytes:
            self.cover_label.setPixmap(QPixmap())
            self.cover_label.setText(_('No cover'))
            return
        pixmap = QPixmap()
        if not pixmap.loadFromData(cover_bytes):
            self.cover_label.setPixmap(QPixmap())
            self.cover_label.setText(_('No cover'))
            return
        try:
            self._cover_native_size = pixmap.size()
            self._cover_bytes_len = len(cover_bytes)
        except Exception:
            self._cover_native_size = None
            self._cover_bytes_len = 0
        # Fixed-size thumbnail to keep the selected-book panel height stable.
        scaled = pixmap.scaled(80, 116, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
        self.cover_label.setPixmap(scaled)
        self.cover_label.setText('')

    def resolve_book_path(self, book_id):
        self._ensure_current_db()
        preferred: list[str] = []
        try:
            from calibre.gui2 import gprefs
            raw = gprefs.get('input_format_order', None)
            if isinstance(raw, (list, tuple)):
                preferred = [str(x or '').strip().upper() for x in raw if str(x or '').strip()]
            elif isinstance(raw, str):
                preferred = [x.strip().upper() for x in raw.split(',') if x.strip()]
        except Exception:
            preferred = []
        if not preferred:
            preferred = ['EPUB', 'AZW3', 'MOBI', 'HTMLZ', 'PDF', 'DOCX', 'TXT', 'HTML', 'HTM']
        fmt_string = ''
        current_db = getattr(self.gui, 'current_db', None)
        current_new_api = getattr(current_db, 'new_api', current_db)
        for db in (current_new_api, current_db, self.db):
            if db is None:
                continue
            try:
                fmt_string = db.formats(book_id, index_is_id=True) or ''
                if fmt_string:
                    break
            except Exception:
                continue
        formats = [x.strip().upper() for x in fmt_string.split(',') if x.strip()]
        if not formats:
            return '', ''
        ordered = preferred + [fmt for fmt in formats if fmt not in preferred]
        for fmt in ordered:
            path = self._format_path(book_id, fmt)
            if path and os.path.exists(path):
                return path, fmt
        return '', formats[0]

    def _format_path(self, book_id, fmt):
        current_db = getattr(self.gui, 'current_db', None)
        current_new_api = getattr(current_db, 'new_api', current_db)
        self._ensure_current_db()
        for db in (current_new_api, current_db, self.db):
            if db is None:
                continue
            try:
                return db.format_abspath(book_id, fmt, index_is_id=True)
            except Exception:
                pass
            try:
                return db.format(book_id, fmt, index_is_id=True, as_path=True, preserve_filename=True)
            except Exception:
                pass
        return ''

    def choose_external_file(self):
        path, selected_filter = QFileDialog.getOpenFileName(self, _('Choose text or ebook file'))
        if path:
            self.external_path.setText(path)
            try:
                self.external_path.setCursorPosition(0)
            except Exception:
                pass

    def clear_external_file(self):
        if not self.external_path.text():
            return
        self.external_path.clear()

    def _update_toc_buttons(self):
        path = ''
        ext_path = self.external_path.text().strip()
        if ext_path:
            path = ext_path if os.path.exists(ext_path) else ''
        else:
            details = self.selected_book_details()
            if details is not None:
                path = details.get('path', '') or ''
        enabled = bool(path and os.path.exists(path))
        ext = os.path.splitext(path)[1].lower()
        # Enable ToC viewer for EPUB, KEPUB, and AZW3 (all supported by calibre's get_container)
        if enabled and ext not in ('.epub', '.kepub', '.azw3'):
            enabled = False
        self.toc_fast_btn.setEnabled(enabled)

    def _toc_source_path(self) -> tuple[str, str]:
        path, title, _ = self.resolve_source()
        if not path:
            return '', ''
        if not os.path.exists(path):
            return '', ''
        return path, title

    def open_free_api_guide(self):
        d = FreeApiGuideDialog(parent=self)
        try:
            d.setWindowIcon(self.windowIcon())
        except Exception:
            pass
        d.exec()

    def open_cache_manager(self):
        d = ChapterSummarizerCacheManagerDialog(parent=self)
        d.show()
        d.raise_()
        d.activateWindow()

    def open_history(self):
        source_path, source_title, book_id = self.resolve_source()
        if not source_path:
            return
        d = ChapterSummarizerHistoryDialog(
            source_path,
            source_title=source_title or os.path.splitext(os.path.basename(source_path))[0],
            source_book_id=str(book_id or ''),
            parent=self,
        )
        d.exec()

    def view_selected_book(self):
        details = self.selected_book_details()
        if details is None:
            return
        book_id = details.get('book_id')
        if book_id is None:
            return
        fmt = str(details.get('format') or '').upper().strip()
        if not fmt:
            _path, fmt = self.resolve_book_path(book_id)
            fmt = str(fmt or '').upper().strip()
        if not fmt:
            error_dialog(self, _('View failed'), _('Could not determine a viewable format for the selected book.'), show=True)
            return
        try:
            self.gui.show_library_view()
        except Exception:
            pass
        try:
            self.gui.library_view.select_rows([book_id], using_ids=True)
        except Exception:
            pass
        viewer_action = getattr(self.gui, 'iactions', {}).get('View') if getattr(self.gui, 'iactions', None) else None
        if viewer_action is None:
            error_dialog(self, _('View failed'), _('E-book viewer action is not available in this calibre installation.'), show=True)
            return
        try:
            viewer_action.view_format_by_id(book_id, fmt)
        except Exception as e:
            error_dialog(self, _('View failed'), _('Could not open the selected book in the calibre viewer.'), det_msg=str(e), show=True)

    def _reset_footer_progress_state(self):
        if self._worker is not None and self._worker.is_running():
            return
        try:
            self.progress.setValue(0)
        except Exception:
            pass
        try:
            self.progress.setFormat('%p%')
        except Exception:
            pass

    def _chapter_cache_profile(self, source_path: str) -> tuple[str, str, str, str]:
        provider_name = self.provider_combo.currentData() or self.provider_combo.currentText().strip()
        request_model_id, request_model_label, _ = self._current_requested_model_info(provider_name)
        _effective_model_id, effective_model_label, _effective_model_source = self.current_effective_model_info()
        requested_model = request_model_id or request_model_label or effective_model_label or '(provider default)'
        flash_mode = bool(self.flash_summary_check.isChecked())
        max_chunk_words_spin = getattr(self, 'max_chunk_words_spin', None)
        max_chunk_words = int(max_chunk_words_spin.value()) if max_chunk_words_spin is not None else int(pref('max_chunk_words', 1400))
        profile_key = build_cache_profile_key(
            source_path=source_path,
            provider_name=provider_name,
            requested_model=requested_model,
            target_language=normalize_target_language(self.target_language.currentData() or 'same-as-source'),
            base_paragraphs=self.base_paragraphs.value(),
            include_highlight=self.include_highlight.isChecked(),
            max_chunk_words=max_chunk_words,
            flash_mode=flash_mode,
        )
        return profile_key, source_fingerprint(source_path), requested_model, provider_name

    def _load_cached_chapter_entries(self, source_path: str, source_matches: list[dict] | None = None) -> dict[str, dict[str, object]]:
        """Load cached chapter entries for the given source path.

        Merges entries from EVERY cache DB that matches source_path, plus the
        current profile key (in case it hasn’t had source_path written yet).
        Older DBs are loaded first so that a newer run’s entries win on conflict.
        This prevents the ‘cache erasure’ that occurred when calibre modified the
        epub (changing file size → old profile key became unreachable).
        """
        if not source_path or not pref_bool('chapter_cache_enabled', True):
            return {}
        profile_key, _src_hash, _requested_model, _provider = self._chapter_cache_profile(source_path)
        # Collect all profile keys that have entries for this source, oldest first.
        matches = list(source_matches) if source_matches is not None else self._cache_matches_for_source(source_path)
        all_keys: list[str] = []
        seen: set[str] = set()
        # matches are sorted newest first — reverse so we load oldest first (newer wins)
        for m in reversed(matches):
            k = str(m.get('profile_key') or '')
            if k and k not in seen:
                all_keys.append(k)
                seen.add(k)
        # Always include the current profile key (may be empty / brand-new DB)
        if profile_key not in seen:
            all_keys.append(profile_key)
            seen.add(profile_key)
        ans: dict[str, dict[str, object]] = {}
        for key in all_keys:
            cache = ChapterSummaryCache(key)
            try:
                entries = _safe_cache_all_entries(cache)
            finally:
                _safe_cache_close(cache)
            # Merge unconditionally — source_hash differences are diagnostic only.
            for chapter_id, entry in entries.items():
                ans[chapter_id] = entry
        return ans

    def _load_cached_summary_payload(self, source_path: str, preferred_profile_key: str = '', source_matches: list[dict] | None = None) -> tuple[dict[str, str], dict[str, dict[str, object]]]:
        preferred_profile_key = str(preferred_profile_key or '').strip()
        if (not source_path and not preferred_profile_key) or not pref_bool('chapter_cache_enabled', True):
            return {}, {}
        profile_key = ''
        if source_path:
            profile_key, _src_hash, _requested_model, _provider = self._chapter_cache_profile(source_path)
        candidate_keys = []
        if preferred_profile_key:
            candidate_keys.append(preferred_profile_key)
        if profile_key and profile_key not in candidate_keys:
            candidate_keys.append(profile_key)
        if source_path:
            matches = list(source_matches) if source_matches is not None else self._cache_matches_for_source(source_path)
            for match in matches:
                key = str(match.get('profile_key') or '')
                if key and key not in candidate_keys:
                    candidate_keys.append(key)
        for key in candidate_keys:
            cache = ChapterSummaryCache(key)
            try:
                info = {
                    'source_path': _safe_cache_get_info(cache, 'source_path'),
                    'source_title': _safe_cache_get_info(cache, 'source_title'),
                    'provider_name': _safe_cache_get_info(cache, 'provider_name'),
                    'requested_model': _safe_cache_get_info(cache, 'requested_model'),
                    'updated_at': _safe_cache_get_info(cache, 'updated_at'),
                    'source_book_id': _safe_cache_get_info(cache, 'source_book_id'),
                    'source_format': _safe_cache_get_info(cache, 'source_format'),
                    'rendered_text': _safe_cache_get_info(cache, 'rendered_text'),
                    'document_type': _safe_cache_get_info(cache, 'document_type'),
                    'highlight_label': _safe_cache_get_info(cache, 'highlight_label'),
                    'model_used': _safe_cache_get_info(cache, 'model_used'),
                    # New fields for language handling
                    'target_language': _safe_cache_get_info(cache, 'target_language'),
                    'target_language_name': _safe_cache_get_info(cache, 'target_language_name'),
                    'source_language_code': _safe_cache_get_info(cache, 'source_language_code'),
                    'source_language_name': _safe_cache_get_info(cache, 'source_language_name'),
                    'request_count': _safe_cache_get_info(cache, 'request_count'),
                    'request_attempts': _safe_cache_get_info(cache, 'request_attempts'),
                    'duration_seconds': _safe_cache_get_info(cache, 'duration_seconds'),
                    'chapter_count': _safe_cache_get_info(cache, 'chapter_count'),
                }
                entries = _safe_cache_all_entries(cache)
            finally:
                _safe_cache_close(cache)
            # Include all entries regardless of source_hash — hash differences
            # only mean the epub was touched between runs (e.g. calibre metadata
            # write), not that the summaries are invalid.
            if entries or str(info.get('rendered_text') or '').strip():
                return info, entries
        return {}, {}

    def _rebuild_cached_summary_text(self, info: dict[str, str], entries: dict[str, dict[str, object]]) -> str:
        rendered = str(info.get('rendered_text') or '').strip()
        if rendered:
            try:
                has_entry_stats = any(
                    int((entry or {}).get('request_count') or 0)
                    or int((entry or {}).get('request_attempts') or 0)
                    or float((entry or {}).get('duration_seconds') or 0.0)
                    for entry in (entries or {}).values()
                )
                has_info_stats = (
                    int(info.get('request_count') or 0)
                    or int(info.get('request_attempts') or 0)
                    or float(info.get('duration_seconds') or 0.0)
                    or int(info.get('chapter_count') or 0)
                )
                rendered_has_zero_stats = bool(
                    re.search(r'^- Requests:\s*0\s*\(attempts:\s*0\)', rendered, re.MULTILINE)
                    or re.search(r'^- Duration:\s*0:00\b', rendered, re.MULTILINE)
                    or re.search(r'^- Chapters detected:\s*0\b', rendered, re.MULTILINE)
                )
                if not (rendered_has_zero_stats and (has_entry_stats or has_info_stats)):
                    return rendered
            except Exception:
                return rendered
        def _sort_key(item):
            chapter_id, entry = item
            toc_i = int((entry or {}).get('toc_index') or -1)
            if toc_i >= 0:
                # Use chapter_id as tiebreaker for overlapping toc_index values (e.g. resumed partial runs).
                return (0, toc_i, 0, str(chapter_id or '').casefold())
            # Fall back to index:N in chapter_id (e.g. from flat fallback parser).
            m_id = re.match(r'^index:(\d+)$', str(chapter_id or ''))
            if m_id:
                return (1, int(m_id.group(1)), 0, str(chapter_id or '').casefold())
            # Attempt extraction from title: 'Chapter N', 'Ch. N', '\d+. Title', etc.
            title_str = str((entry or {}).get('title') or '')
            m_title = re.search(r'(?:chapter|ch\.?)\s*(\d+)', title_str, re.I)
            if m_title:
                return (2, int(m_title.group(1)), 0, title_str.casefold())
            m_lead = re.match(r'^\s*(\d+)[.:\s]', title_str)
            if m_lead:
                return (2, int(m_lead.group(1)), 0, title_str.casefold())
            # Known back-matter roles sort last.
            backmatter = ('about the author', 'about author', 'acknowledgment', 'index', 'references',
                          'appendix', 'bibliography', 'glossary', 'conclusion', 'afterword', 'epilogue')
            if any(title_str.casefold().startswith(b) for b in backmatter):
                return (9, 10**9, 0, title_str.casefold())
            # Front-matter sorts near front (high chapter number unlikely).
            frontmatter = ('cover', 'title', 'foreword', 'preface', 'contents', 'introduction')
            if any(title_str.casefold().startswith(f) for f in frontmatter):
                return (0, 10**8, 0, title_str.casefold())
            return (8, 10**9, 0, title_str.casefold())

        chapter_summaries: list[ChapterSummary] = []
        for chapter_id, entry in sorted(entries.items(), key=_sort_key):
            title = str((entry or {}).get('title') or '').strip() or _('Untitled chapter')
            idx = int((entry or {}).get('toc_index') or -1)
            if idx < 0:
                idx_match = re.match(r'^\s*(\d+)\s*\.', title)
                idx = int(idx_match.group(1)) if idx_match else (len(chapter_summaries) + 1)
            chapter_summaries.append(ChapterSummary(
                chapter_id=str(chapter_id or ''),
                index=idx,
                title=title,
                summary=str((entry or {}).get('summary') or '').strip(),
                highlight=str((entry or {}).get('highlight') or '').strip(),
                word_count=int((entry or {}).get('word_count') or 0),
                model_used=str((entry or {}).get('model_used') or info.get('model_used') or ''),
                request_count=int((entry or {}).get('request_count') or 0),
                request_attempts=int((entry or {}).get('request_attempts') or 0),
                duration_seconds=float((entry or {}).get('duration_seconds') or 0.0),
            ))
        if not chapter_summaries:
            return ''
        source_title = str(info.get('source_title') or os.path.splitext(os.path.basename(info.get('source_path') or ''))[0] or _('Summary')).strip()
        source_path = str(info.get('source_path') or '').strip()
        source_format = str(info.get('source_format') or os.path.splitext(source_path)[1].lower().lstrip('.') or 'unknown').strip()
        document = ParsedDocument(
            title=source_title,
            source_path=source_path,
            source_format=source_format,
            raw_text='',
            chapters=[],
        )
        document_type = str(info.get('document_type') or 'nonfiction').strip() or 'nonfiction'
        highlight_label = str(info.get('highlight_label') or default_highlight_label(document_type)).strip() or default_highlight_label(document_type)
        # Reconstruct options from cache to preserve language information
        source_lang_code = str(info.get('source_language_code') or '').strip()
        source_lang_name = str(info.get('source_language_name') or '').strip()
        target_lang = str(info.get('target_language') or 'same-as-source').strip()
        target_lang_name = str(info.get('target_language_name') or '').strip()
        cached_options = SummaryOptions(
            provider_name=str(info.get('provider_name') or ''),
            model_name=str(info.get('model_used') or info.get('requested_model') or ''),
            requested_model_label=str(info.get('requested_model') or ''),
            source_language_code=source_lang_code,
            source_language_name=source_lang_name,
            target_language=target_lang,
            target_language_name=target_lang_name,
        )
        run_stats = None
        try:
            duration_seconds = max(0.0, float(info.get('duration_seconds') or 0.0))
            if duration_seconds <= 0.0:
                duration_seconds = sum(max(0.0, float(getattr(c, 'duration_seconds', 0.0) or 0.0)) for c in chapter_summaries)
            run_stats = RunStats(
                started_at=1.0,
                ended_at=1.0 + duration_seconds,
                chapter_count=int(info.get('chapter_count') or len(chapter_summaries) or 0),
                request_count=int(info.get('request_count') or sum(int(getattr(c, 'request_count', 0) or 0) for c in chapter_summaries)),
                request_attempts=int(info.get('request_attempts') or sum(int(getattr(c, 'request_attempts', 0) or 0) for c in chapter_summaries)),
            )
        except Exception:
            run_stats = None
        return render_summary(
            document,
            {'document_type': document_type, 'highlight_label': highlight_label, 'guidance': ''},
            chapter_summaries,
            str(info.get('provider_name') or ''),
            str(info.get('model_used') or info.get('requested_model') or ''),
            str(info.get('requested_model') or ''),
            options=cached_options,
            stats=run_stats,
        )

    def _restore_cached_summary_tabs(self, source_path: str, preferred_profile_key: str = '') -> bool:
        preferred_profile_key = str(preferred_profile_key or '').strip()
        if self._worker is not None and self._worker.is_running():
            return False
        # If the source has changed, clear stale state from the previous book
        # before we know whether there is a cache for the new one.
        source_changed = bool(source_path) and str(source_path) != str(self._last_source_path or '')
        if source_changed or (not source_path and not preferred_profile_key):
            self._reset_footer_progress_state()
            if not source_path and not preferred_profile_key:
                if not self.result_edit.toPlainText().strip():
                    self.result_pretty.clear()
                return False
            # Source changed — wipe the previous book's result immediately so
            # the wrong summary never lingers while the cache look-up runs.
            self._last_result = None
            self.result_edit.clear()
            self.result_pretty.clear()
        source_matches = self._cache_matches_for_source(source_path) if source_path else None
        info, entries = self._load_cached_summary_payload(source_path, preferred_profile_key=preferred_profile_key, source_matches=source_matches)
        rendered_text = str(info.get('rendered_text') or '').strip()
        if not entries and not rendered_text:
            # No cache for this source — pane is already clear (or was clear).
            self._reset_footer_progress_state()
            return False
        text = rendered_text or self._rebuild_cached_summary_text(info, entries)
        if not text:
            return False
        # Patch the language line in case the cache predates language detection being stored.
        # Re-detect from current book selection to freshen "Source language: unknown".
        try:
            _lang_code, _lang_name, _ = self.detect_source_language_info()
            if _lang_name:
                _opts_for_lang = SummaryOptions(
                    source_language_code=_lang_code,
                    source_language_name=_lang_name,
                    target_language=str(info.get('target_language') or 'same-as-source'),
                    target_language_name=str(info.get('target_language_name') or ''),
                )
                _new_lang_line = render_language_line(_opts_for_lang)
                text = re.sub(r'^- Source language:.*$', _new_lang_line, text, flags=re.MULTILINE)
        except Exception:
            pass
        self._last_source_path = str(info.get('source_path') or source_path)
        self._last_source_title = str(info.get('source_title') or self._last_source_title or '')
        # Sync the Output language combo box to the language stored in the cache (if any).
        try:
            cached_target = str(info.get('target_language') or pref('target_language', 'same-as-source'))
            idx = self.target_language.findData(cached_target)
            if idx >= 0:
                self.target_language.setCurrentIndex(idx)
        except Exception:
            pass
        model_used = str(info.get('model_used') or info.get('requested_model') or '')
        provider_used = str(info.get('provider_name') or '')
        requested_model = str(info.get('requested_model') or model_used)
        chapter_summaries: list[ChapterSummary] = []
        for chapter_id, entry in entries.items():
            title = str((entry or {}).get('title') or '').strip() or _('Untitled chapter')
            idx_match = re.match(r'^\s*(\d+)\s*\.', title)
            idx = int(idx_match.group(1)) if idx_match else (len(chapter_summaries) + 1)
            chapter_summaries.append(ChapterSummary(
                chapter_id=str(chapter_id or ''),
                index=idx,
                title=title,
                summary=str((entry or {}).get('summary') or '').strip(),
                highlight=str((entry or {}).get('highlight') or '').strip(),
                word_count=int((entry or {}).get('word_count') or 0),
                model_used=str((entry or {}).get('model_used') or model_used),
                request_count=int((entry or {}).get('request_count') or 0),
                request_attempts=int((entry or {}).get('request_attempts') or 0),
                duration_seconds=float((entry or {}).get('duration_seconds') or 0.0),
            ))
        self._last_result = SummaryResult(
            text=text,
            document_type=str(info.get('document_type') or 'nonfiction'),
            highlight_label=str(info.get('highlight_label') or default_highlight_label(str(info.get('document_type') or 'nonfiction'))),
            chapter_summaries=chapter_summaries,
            model_used=model_used,
            requested_model=requested_model,
            provider_used=provider_used,
            run_stats=None,
        )
        self._last_model_used = model_used
        self._last_provider_used = provider_used
        self._last_result_is_flash = not bool(chapter_summaries)
        self.result_edit.setPlainText(text)
        self._refresh_result_preview(text)
        self.save_button.setEnabled(True)
        self.summary_browser_button.setEnabled(True)
        self.add_to_data_files_button.setEnabled(self._last_book_id is not None)
        self.add_to_library_button.setEnabled(True)
        self.comments_button.setEnabled(self._last_book_id is not None)
        return True

    def load_summary_from_cache_profile(self, profile_key: str, source_path: str = '', source_title: str = '') -> bool:
        """Load summary text/preview from a specific cached summary entry selected in Cache Manager."""
        key = str(profile_key or '').strip()
        if not key:
            return False
        loaded = self._restore_cached_summary_tabs(str(source_path or '').strip(), preferred_profile_key=key)
        if not loaded:
            return False
        try:
            self.output_tabs.setCurrentIndex(0)
        except Exception:
            pass
        title = str(source_title or '').strip() or str(self._last_source_title or '').strip()
        if title:
            self.status_label.setText(_('Summary loaded from Cache Manager: %s') % title)
        else:
            self.status_label.setText(_('Summary loaded from Cache Manager.'))
        return True

    def _store_result_in_chapter_cache(self, result):
        if result is None or not pref_bool('chapter_cache_enabled', True):
            return
        if not self._last_cache_profile_key or not self._last_source_path:
            return
        # Detect flash mode: empty chapter_summaries list means flash summary
        is_flash = bool(getattr(result, 'chapter_summaries', None) == [])
        cache = ChapterSummaryCache(self._last_cache_profile_key)
        try:
            if not (_cache_has_method(cache, 'set_info') and _cache_has_method(cache, 'upsert_chapter') and _cache_has_method(cache, 'all_entries')):
                return
            try:
                lib_path = ''
                if getattr(self, 'gui', None) is not None and getattr(self.gui, 'current_db', None) is not None:
                    lib_path = str(getattr(self.gui.current_db, 'library_path', '') or '')
                _safe_cache_set_info(cache, 'source_library_path', lib_path)
            except Exception:
                pass
            _safe_cache_set_info(cache, 'source_path', self._last_source_path)
            _safe_cache_set_info(cache, 'source_title', self._last_source_title)
            _safe_cache_set_info(cache, 'provider_name', self._last_cache_provider)
            _safe_cache_set_info(cache, 'requested_model', self._last_cache_requested_model)
            _safe_cache_set_info(cache, 'model_used', str(getattr(result, 'model_used', '') or ''))
            _safe_cache_set_info(cache, 'document_type', str(getattr(result, 'document_type', '') or ''))
            _safe_cache_set_info(cache, 'highlight_label', str(getattr(result, 'highlight_label', '') or ''))
            _safe_cache_set_info(cache, 'rendered_text', str(getattr(result, 'text', '') or ''))
            run_stats = getattr(result, 'run_stats', None)
            duration_seconds = 0.0
            if run_stats is not None:
                try:
                    if getattr(run_stats, 'started_at', 0.0) and getattr(run_stats, 'ended_at', 0.0) and run_stats.ended_at >= run_stats.started_at:
                        duration_seconds = max(0.0, float(run_stats.ended_at - run_stats.started_at))
                except Exception:
                    duration_seconds = 0.0
                _safe_cache_set_info(cache, 'request_count', str(int(getattr(run_stats, 'request_count', 0) or 0)))
                _safe_cache_set_info(cache, 'request_attempts', str(int(getattr(run_stats, 'request_attempts', 0) or 0)))
                _safe_cache_set_info(cache, 'duration_seconds', str(duration_seconds))
                _safe_cache_set_info(cache, 'chapter_count', str(int(getattr(run_stats, 'chapter_count', 0) or len(list(getattr(result, 'chapter_summaries', []) or [])) or 0)))
            _safe_cache_set_info(cache, 'source_book_id', str(self._last_book_id if self._last_book_id is not None else ''))
            _safe_cache_set_info(cache, 'source_format', str(os.path.splitext(self._last_source_path)[1].lower().lstrip('.') or ''))
            _safe_cache_set_info(cache, 'updated_at', _now_stamp())
            # Store language information for proper display in cached summaries
            if hasattr(self, '_last_options') and self._last_options is not None:
                _safe_cache_set_info(cache, 'source_language_code', str(getattr(self._last_options, 'source_language_code', '') or ''))
                _safe_cache_set_info(cache, 'source_language_name', str(getattr(self._last_options, 'source_language_name', '') or ''))
                _safe_cache_set_info(cache, 'target_language', str(getattr(self._last_options, 'target_language', 'same-as-source') or 'same-as-source'))
                _safe_cache_set_info(cache, 'target_language_name', str(getattr(self._last_options, 'target_language_name', '') or ''))
            # For flash summaries, skip chapter entry storage and cross-db merge
            # to avoid overwriting the flash summary with old chapter entries
            if is_flash:
                return
            for toc_pos, chapter in enumerate(list(getattr(result, 'chapter_summaries', []) or [])):
                chapter_id = str(getattr(chapter, 'chapter_id', '') or '')
                if not chapter_id:
                    continue
                summary_text = str(getattr(chapter, 'summary', '') or '').strip()
                highlight_text = str(getattr(chapter, 'highlight', '') or '').strip()
                preview = summary_text.replace('\n', ' ').strip()
                # Use the chapter’s original ToC position for stable ordering.
                toc_index = int(getattr(chapter, 'index', -1) or -1)
                if toc_index < 0:
                    toc_index = toc_pos
                _safe_cache_upsert_chapter(cache, {
                    'chapter_id': chapter_id,
                    'title': str(getattr(chapter, 'title', '') or ''),
                    'summary': summary_text,
                    'highlight': highlight_text,
                    'word_count': int(getattr(chapter, 'word_count', 0) or 0),
                    'provider_name': str(getattr(result, 'provider_used', '') or self._last_cache_provider),
                    'model_used': str(getattr(chapter, 'model_used', '') or getattr(result, 'model_used', '') or ''),
                    'requested_model': str(getattr(result, 'requested_model', '') or self._last_cache_requested_model),
                    'request_count': int(getattr(chapter, 'request_count', 0) or 0),
                    'request_attempts': int(getattr(chapter, 'request_attempts', 0) or 0),
                    'duration_seconds': float(getattr(chapter, 'duration_seconds', 0.0) or 0.0),
                    'created_at': _now_stamp(),
                    'source_hash': self._last_cache_source_hash,
                    'preview': preview[:600],
                    'toc_index': toc_index,
                })
            # Collect entries from EVERY cache DB for this source and consolidate
            # them all into the current-run DB.  This repairs fragmentation caused
            # by old profile-key splitting (file-size hash) so resumed partial
            # runs never silently drop chapters from earlier runs.
            cross_db_entries = self._load_cached_chapter_entries(self._last_source_path)
            existing_ids = set(_safe_cache_all_entries(cache).keys())
            for x_chapter_id, x_entry in cross_db_entries.items():
                if not x_chapter_id or x_chapter_id in existing_ids:
                    continue  # already in current DB
                _safe_cache_upsert_chapter(cache, {
                    'chapter_id': x_chapter_id,
                    'title': str((x_entry or {}).get('title') or ''),
                    'summary': str((x_entry or {}).get('summary') or ''),
                    'highlight': str((x_entry or {}).get('highlight') or ''),
                    'word_count': int((x_entry or {}).get('word_count') or 0),
                    'provider_name': str((x_entry or {}).get('provider_name') or self._last_cache_provider),
                    'model_used': str((x_entry or {}).get('model_used') or ''),
                    'requested_model': str((x_entry or {}).get('requested_model') or self._last_cache_requested_model),
                    'request_count': int((x_entry or {}).get('request_count') or 0),
                    'request_attempts': int((x_entry or {}).get('request_attempts') or 0),
                    'duration_seconds': float((x_entry or {}).get('duration_seconds') or 0.0),
                    'created_at': str((x_entry or {}).get('created_at') or _now_stamp()),
                    'source_hash': str((x_entry or {}).get('source_hash') or self._last_cache_source_hash),
                    'preview': str((x_entry or {}).get('preview') or ''),
                    'toc_index': int((x_entry or {}).get('toc_index') or -1),
                })
            merged_entries = dict(_safe_cache_all_entries(cache))
            merged_info = {
                'source_path': self._last_source_path,
                'source_title': self._last_source_title,
                'provider_name': str(getattr(result, 'provider_used', '') or self._last_cache_provider),
                'requested_model': str(getattr(result, 'requested_model', '') or self._last_cache_requested_model),
                'updated_at': _now_stamp(),
                'source_book_id': str(self._last_book_id if self._last_book_id is not None else ''),
                'source_format': str(os.path.splitext(self._last_source_path)[1].lower().lstrip('.') or ''),
                'rendered_text': '',
                'document_type': str(getattr(result, 'document_type', '') or ''),
                'highlight_label': str(getattr(result, 'highlight_label', '') or ''),
                'model_used': str(getattr(result, 'model_used', '') or ''),
                'request_count': str(int(getattr(run_stats, 'request_count', 0) or 0)) if run_stats is not None else '',
                'request_attempts': str(int(getattr(run_stats, 'request_attempts', 0) or 0)) if run_stats is not None else '',
                'duration_seconds': str(duration_seconds) if run_stats is not None else '',
                'chapter_count': str(int(getattr(run_stats, 'chapter_count', 0) or len(list(getattr(result, 'chapter_summaries', []) or [])) or 0)) if run_stats is not None else '',
            }
            merged_text = self._rebuild_cached_summary_text(merged_info, merged_entries)
            if merged_text:
                _safe_cache_set_info(cache, 'rendered_text', merged_text)
                # Note: we intentionally do NOT overwrite result.text with merged_text here.
                # result.text must keep reflecting exactly what was generated in THIS run;
                # otherwise stale chapters resurrected from older cache DBs (possibly in the
                # wrong language, or from a chapter selection the user no longer wants) would
                # silently reappear in the summary the user is currently viewing/exporting.
                # The consolidated merged_text is still stored above so the Cache Manager and
                # "restore last summary" flow can offer the fuller picture on demand.
        finally:
            _safe_cache_close(cache)
        self._clear_source_cache_matches()

    def select_chapters(self):
        from calibre.gui2 import question_dialog  # type: ignore
        path, title = self._toc_source_path()
        if not path:
            return
        # Warn if the source appears to be a plugin-generated summary rather than an original book.
        _clean_title = str(title or os.path.basename(path) or '').strip()
        _summary_prefixes = ('Summary — ', 'Summary: ', _('Summary') + ' — ', _('Summary') + ': ')
        if any(_clean_title.startswith(p) for p in _summary_prefixes):
            ans = question_dialog(
                self,
                _('Possible misselection'),
                _('The selected source appears to be a generated summary:\n\n   "%s"\n\n'
                  'You are likely about to summarise an already-summarised book.\n'
                  'Continue anyway?') % _clean_title,
                default_yes=False,
            )
            if not ans:
                return
        selected = None
        if self._chapter_selection_source_path == path and self._chapter_selection_ids is not None:
            selected = set(self._chapter_selection_ids)
        cache_entries = self._load_cached_chapter_entries(path)
        last_run = None
        try:
            db = RunHistoryDB()
            last_run = db.last_run_for_source(source_path=path)
            db.close()
        except Exception:
            pass
        d = ChapterSelectorDialog(path, title=title or os.path.basename(path), selected_ids=selected, cache_entries=cache_entries, last_run=last_run, parent=self)
        self._chapter_selector_dialog = d
        if d.exec() == QDialog.DialogCode.Accepted:
            chosen = d.selected_ids()
            total = int(getattr(d, 'total_selectable', 0) or 0)
            if total > 0 and len(chosen) >= total:
                self._chapter_selection_ids = None
                self._chapter_selection_total = total
            else:
                self._chapter_selection_ids = set(chosen)
                self._chapter_selection_total = total
            self._chapter_selection_source_path = path
            self._update_chapter_selection_badge()

    def export_result_html(self):
        if self._last_result is None:
            return
        default_name = os.path.splitext(self.default_summary_filename())[0] + '.html'
        path = choose_save_file(self, _('Export summary as HTML'), _('HTML files (*.html *.htm);;All files (*)'), initial_filename=default_name)
        if not path:
            return
        html_doc = self._current_summary_html(self._last_result.text, layout_mode='html-export')
        with open(path, 'w', encoding='utf-8') as f:
            f.write(html_doc)
        info_dialog(self, _('Saved'), _('HTML exported successfully.'), show=True)

    def export_result_epub(self):
        if self._last_result is None:
            return
        default_name = os.path.splitext(self.default_summary_filename())[0] + '.epub'
        path = choose_save_file(self, _('Export summary as EPUB'), _('EPUB files (*.epub);;All files (*)'), initial_filename=default_name)
        if not path:
            return
        html_doc = self._current_epub_summary_html(self._last_result.text)
        ok, err = export_html_to_epub(html_doc, path)
        if ok:
            info_dialog(self, _('Saved'), _('EPUB exported successfully.'), show=True)
        else:
            error_dialog(self, _('Export failed'), _('Could not export EPUB.'), det_msg=err, show=True)

    def open_result_in_browser(self):
        text = ''
        if self._last_result is not None:
            text = self._last_result.text or ''
        elif self.result_edit.toPlainText().strip():
            text = self.result_edit.toPlainText()
        if not text:
            return
        base = os.path.splitext(self.default_summary_filename())[0]
        safe_title = _safe_filename_component(base, 'summary')
        path = os.path.join(tempfile.gettempdir(), f'chapter_summarizer_preview_{safe_title[:80]}_{_timestamp_suffix()}.html')
        try:
            if self._worker is not None and '[PARTIAL RESULT]' not in text:
                text = '[PARTIAL RESULT] Preview opened while summarization is still running.\n\n' + text
            html_doc = self._current_summary_html(text, layout_mode='browser')
            with open(path, 'w', encoding='utf-8') as f:
                f.write(html_doc)
            open_url(QUrl.fromLocalFile(path).toString())
        except Exception as e:
            error_dialog(self, _('Browser preview failed'), str(e), show=True)

    def configure_provider(self):
        current_provider = self.provider_combo.currentData() or self.provider_combo.currentText().strip()
        name = current_provider
        if not name:
            return
        if is_zero_config_demo_provider(name):
            info_dialog(self, _('Zero-config demo provider'), self._zero_config_provider_notice(), show=True)
            return
        if name == 'NVIDIA Free':
            from calibre_plugins.chapter_summarizer.nvidia_free import config_widget, save_settings, validate
            cw = config_widget()

            class _ProviderConfigDialog(QDialog):
                def accept(self):
                    if not validate(cw):
                        return
                    super().accept()

            d = _ProviderConfigDialog(parent=self)
            d.setWindowTitle(_('Configure provider for Chapter Summarizer') + ' — ' + name)
            try:
                d.setWindowIcon(QIcon.ic('ai.png'))
            except Exception:
                pass
            dl = QVBoxLayout(d)
            dl.addWidget(cw)
            bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
            bb.accepted.connect(d.accept)
            bb.rejected.connect(d.reject)
            dl.addWidget(bb)
            d.resize(d.sizeHint())
            # Set minimum width to 500px to prevent clipping
            d.setMinimumWidth(500)
            if d.exec() == QDialog.DialogCode.Accepted:
                save_settings(cw)
            self.populate_providers(select_provider_name=current_provider)
            self.update_effective_model_label()
            return
        if name == 'NVIDIA Free (API)':
            from calibre_plugins.chapter_summarizer.nvidia_free import config_widget_api, save_settings_api, validate_api
            cw = config_widget_api()

            class _ProviderConfigDialog(QDialog):
                def accept(self):
                    if not validate_api(cw):
                        return
                    super().accept()

            d = _ProviderConfigDialog(parent=self)
            d.setWindowTitle(_('Configure provider for Chapter Summarizer') + ' — ' + name)
            try:
                d.setWindowIcon(QIcon.ic('ai.png'))
            except Exception:
                pass
            dl = QVBoxLayout(d)
            dl.addWidget(cw)
            bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
            bb.accepted.connect(d.accept)
            bb.rejected.connect(d.reject)
            dl.addWidget(bb)
            d.resize(d.sizeHint())
            # Set minimum width to 500px to prevent clipping
            d.setMinimumWidth(500)
            if d.exec() == QDialog.DialogCode.Accepted:
                save_settings_api(cw)
            self.populate_providers(select_provider_name=current_provider)
            self.update_effective_model_label()
            return
        try:
            from calibre.ai.utils import plugin_for_name as _pfn
            plugin = _pfn(name)

            # Get the config widget (LM Studio will now use the patched version from lm_studio_patch.py)
            cw = plugin.config_widget()

            class _ProviderConfigDialog(QDialog):
                def accept(self):
                    if not cw.validate():
                        return
                    super().accept()

            d = _ProviderConfigDialog(parent=self)
            d.setWindowTitle(_('Configure provider for Chapter Summarizer') + ' — ' + name)
            try:
                d.setWindowIcon(QIcon.ic('ai.png'))
            except Exception:
                pass
            dl = QVBoxLayout(d)
            dl.addWidget(cw)
            bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
            bb.accepted.connect(d.accept)
            bb.rejected.connect(d.reject)
            dl.addWidget(bb)
            d.resize(d.sizeHint())
            # Set minimum width to 500px to prevent model list clipping
            d.setMinimumWidth(500)
            if d.exec() == QDialog.DialogCode.Accepted:
                plugin.save_settings(cw)
        except Exception:
            # Fallback to calibre default
            configure_provider(name, parent=self)
        # Keep the provider the user just configured selected.
        self.populate_providers(select_provider_name=current_provider)
        self.update_effective_model_label()

    def start_summary(self):
        # Multi-file batch mode
        if pref_bool('multi_file_enabled', False) and not self.external_path.text().strip():
            all_books = self.selected_books_details_list()
            if len(all_books) > 1:
                self._start_multi_summary(all_books)
                return
        # Single-file mode: reset batch state so stale "Batch complete" never shows
        self._multi_queue = []
        self._multi_total = 0
        self._multi_current = 0
        self._multi_results = []
        self._multi_batch_cancelled = False
        source_path, source_title, book_id = self.resolve_source()
        if not source_path:
            details = self.selected_book_details()
            if details is not None and not details.get('path'):
                error_dialog(self, _('No ebook format'), _('The selected calibre record has metadata and a cover, but no ebook format to summarize.'), show=True)
            else:
                error_dialog(self, _('No source'), _('Select a calibre book or choose an external text/ebook file.'), show=True)
            return
        self._start_single_summary(source_path, source_title, book_id)

    def _start_single_summary(self, source_path: str, source_title: str, book_id=None):
        self._last_source_path = source_path
        self._last_source_title = source_title
        effective_model_id, effective_model_label, effective_model_source = self.current_effective_model_info()
        provider_name = self.provider_combo.currentData() or self.provider_combo.currentText().strip()
        request_model_id, _request_model_label, _request_model_source = self._current_requested_model_info(provider_name)
        reasoning_strategy = str(pref_for_provider(provider_name, 'reasoning_strategy', 'auto') or 'auto')
        source_language_code, source_language_name, _source_language_source = self.detect_source_language_info()
        target_language = normalize_target_language(self.target_language.currentData() or 'same-as-source')
        max_chunk_words_spin = getattr(self, 'max_chunk_words_spin', None)
        max_chunk_words = int(max_chunk_words_spin.value()) if max_chunk_words_spin is not None else int(pref('max_chunk_words', 1400))
        options = SummaryOptions(
            provider_name=provider_name,
            model_name=request_model_id,
            requested_model_label=effective_model_label,
            reasoning_strategy=reasoning_strategy,
            effective_model_id=effective_model_id,
            base_paragraphs=self.base_paragraphs.value(),
            max_chunk_words=max_chunk_words,
            include_highlight=self.include_highlight.isChecked(),
            target_language=target_language,
            target_language_name=display_language_name(target_language),
            source_language_code=source_language_code,
            source_language_name=source_language_name,
            verbose_ai_log=pref_bool('verbose_ai_log', False),
            request_interval_free=int(pref('request_interval_free', 1)),
            request_interval_default=int(pref('request_interval_default', 0)),
        )
        self._last_options = options
        self.persist_ui_state()
        self._sync_primary_action_button(True)
        self.save_button.setEnabled(False)
        self.add_to_data_files_button.setEnabled(False)
        self.add_to_library_button.setEnabled(False)
        self.comments_button.setEnabled(False)
        self.result_edit.setPlainText(_('Working… Live status appears below and detailed request activity is written to the AI log tab.'))
        self._session_ai_log = []
        self.append_ai_log(_('New summarization run started.'))
        self.append_ai_log(_('Effective model: %s (%s)') % (effective_model_label, effective_model_source))
        if effective_model_id:
            self.append_ai_log(_('Effective model id: %s') % effective_model_id)
        self.status_label.setText(_('Preparing document…'))
        self.progress.setValue(0)
        self._last_result = None
        self._last_book_id = book_id
        self._last_cache_profile_key, self._last_cache_source_hash, self._last_cache_requested_model, self._last_cache_provider = self._chapter_cache_profile(source_path)
        selected_chapter_ids = self._chapter_selection_ids if self._chapter_selection_source_path == source_path else None
        total_nodes = None
        selected_nodes = None
        try:
            if self._chapter_selection_source_path == source_path and int(self._chapter_selection_total or 0) > 0:
                total_nodes = int(self._chapter_selection_total)
            if selected_chapter_ids is None:
                if total_nodes is not None:
                    selected_nodes = int(total_nodes)
            else:
                selected_nodes = len(selected_chapter_ids)
        except Exception:
            total_nodes = None
            selected_nodes = None
        self._history_start_run(
            source_path=source_path,
            source_title=source_title,
            source_book_id=str(book_id or ''),
            provider_name=provider_name,
            requested_model=request_model_id,
            effective_model_label=effective_model_label,
            target_language=target_language,
            selected_nodes=selected_nodes,
            total_nodes=total_nodes,
        )
        self._worker = SummaryWorker(source_path, source_title, options, selected_chapter_ids=selected_chapter_ids, flash_mode=self.flash_summary_check.isChecked())
        self._worker.signals.progress.connect(self.on_progress)
        self._worker.signals.preview.connect(self.on_preview)
        self._worker.signals.finished.connect(self.on_finished)
        self._worker.signals.cancelled.connect(self.on_cancelled)
        self._worker.signals.error.connect(self.on_error)
        self._worker.signals.log.connect(self.append_ai_log)
        self._worker.signals.status.connect(self.status_label.setText)
        self._worker.start()

    def stop_summary(self):
        if self._worker is None or not self._worker.is_running():
            # Also cancel multi-file queue
            if getattr(self, '_multi_queue', None):
                self._multi_queue.clear()
                self._multi_batch_cancelled = True
            return
        self._worker.cancel()
        if getattr(self, '_multi_queue', None):
            self._multi_queue.clear()
            self._multi_batch_cancelled = True
        self.status_label.setText(_('Stop requested — waiting for the in-flight provider request to finish or fail…'))
        self.append_ai_log(_('Stop requested by user. Cancellation will occur before the next request; an in-flight provider request may still need to finish first.'))

    def _start_multi_summary(self, books: list[dict]):
        """Start batch summarization of multiple books sequentially."""
        if not books:
            return
        self._multi_queue = list(books[1:])  # remaining after the first
        self._multi_total = len(books)
        self._multi_current = 0
        self._multi_results: list[object] = []
        self._multi_batch_cancelled = False
        first = books[0]
        self.append_ai_log(_('Multi-file batch: %d book(s) queued') % len(books))
        try:
            self.result_edit.setPlainText(_('Batch started — %d books selected. Live status appears below and detailed request activity is written to the AI log tab.') % len(books))
            self._refresh_result_preview(self.result_edit.toPlainText())
        except Exception:
            pass
        self._start_single_summary(first['path'], first['title'], first.get('book_id'))

    def _on_multi_next(self):
        """Called after each file completes in multi-file mode.  Starts the next or finishes."""
        if self._multi_batch_cancelled or not getattr(self, '_multi_queue', None):
            self._multi_queue = []
            if self._multi_results:
                self.append_ai_log(_('Multi-file batch finished: %d of %d completed') % (self._multi_current, self._multi_total))
                self.status_label.setText(_('Batch complete — %d file(s)') % self._multi_current)
            return
        next_book = self._multi_queue.pop(0)
        self._multi_current += 1
        # CRITICAL FIX: Clear chapter selection between books to avoid cross-contamination
        self._clear_chapter_selection()
        self.append_ai_log(_('Multi-file batch: starting file %d of %d — %s') % (self._multi_current, self._multi_total, next_book.get('title', '?')))
        self._start_single_summary(next_book['path'], next_book['title'], next_book.get('book_id'))

    def resolve_source(self):
        ext_path = self.external_path.text().strip()
        if ext_path:
            if not os.path.exists(ext_path):
                raise_error = error_dialog(self, _('Missing file'), _('The selected external file does not exist anymore.'), show=True)
                return '', '', None
            return ext_path, os.path.splitext(os.path.basename(ext_path))[0], None
        details = self.selected_book_details()
        if details is None:
            return '', '', None
        return details['path'], details['title'], details['book_id']

    def _history_start_run(
        self,
        *,
        source_path: str,
        source_title: str,
        source_book_id: str,
        provider_name: str,
        requested_model: str,
        effective_model_label: str,
        target_language: str,
        selected_nodes: int | None,
        total_nodes: int | None,
    ):
        self._history_run_id = 0
        self._history_run_started_monotonic = None
        try:
            self._history_run_id = _safe_history_record_start(
                source_path=source_path,
                source_title=source_title,
                source_book_id=source_book_id,
                source_format=str(os.path.splitext(source_path or '')[1].lower().lstrip('.') or ''),
                provider_name=provider_name,
                requested_model=requested_model,
                effective_model_label=effective_model_label,
                target_language=target_language,
                selected_nodes=selected_nodes,
                total_nodes=total_nodes,
            )
            self._history_run_started_monotonic = time.monotonic()
        except Exception:
            self._history_run_id = 0
            self._history_run_started_monotonic = None

    def _history_finish_run(self, status: str, result=None, error_message: str = ''):
        run_id = int(getattr(self, '_history_run_id', 0) or 0)
        started = getattr(self, '_history_run_started_monotonic', None)
        self._history_run_id = 0
        self._history_run_started_monotonic = None
        if run_id <= 0:
            return
        summarized_nodes = None
        model_used = ''
        try:
            if result is not None:
                summarized_nodes = len(list(getattr(result, 'chapter_summaries', []) or []))
                model_used = str(getattr(result, 'model_used', '') or '')
        except Exception:
            summarized_nodes = None
        duration_seconds = None
        try:
            if started is not None:
                duration_seconds = float(time.monotonic() - float(started))
        except Exception:
            duration_seconds = None
        try:
            _safe_history_record_finish(
                run_id,
                status=str(status or ''),
                summarized_nodes=summarized_nodes,
                error_message=str(error_message or ''),
                model_used=model_used,
                duration_seconds=duration_seconds,
            )
        except Exception:
            pass

    def on_progress(self, message: str, current: int, total: int):
        total = max(1, total)
        # In multi-file mode, scale progress across total batch
        multi_total = getattr(self, '_multi_total', 0) or 0
        multi_current = getattr(self, '_multi_current', 0) or 0
        if multi_total > 1:
            # Each file contributes 1/multi_total of the overall bar
            file_fraction = current / total
            overall = (multi_current + file_fraction) / multi_total
            self.progress.setValue(min(100, int(overall * 100)))
            prefix = _('[%d/%d] ') % (multi_current + 1, multi_total)
            self.status_label.setText(prefix + message)
        else:
            self.status_label.setText(message)
            self.progress.setValue(min(100, int((current / total) * 100)))
        self.append_ai_log(message)

    def on_preview(self, text: str):
        if not text:
            return
        # In multi-file mode, avoid overwriting the live preview for every file.
        # Keep the preview stable (typically showing the first file's partial
        # result) while progress/logs reflect the batch.
        multi_total = getattr(self, '_multi_total', 0) or 0
        if multi_total > 1 and getattr(self, '_multi_current', 0) > 0:
            return
        self.result_edit.setPlainText(text)
        self._refresh_result_preview(text)
        self.output_tabs.setCurrentIndex(0)

    def on_finished(self, result):
        self._worker = None
        self._last_result = result
        try:
            self._last_result_is_flash = bool(getattr(result, 'chapter_summaries', None) == [])
        except Exception:
            self._last_result_is_flash = False
        self._history_finish_run('success', result=result)
        self._store_result_in_chapter_cache(result)
        self._restore_cached_summary_tabs(self._last_source_path)
        # For flash summaries (and any case where the cache has no chapter
        # entries), _restore_cached_summary_tabs returns without touching the
        # UI.  Always populate the tabs directly from the fresh result.
        text = (result.text or '').strip()
        multi_total = getattr(self, '_multi_total', 0) or 0
        if text and not (multi_total > 1 and getattr(self, '_multi_current', 0) > 0):
            self._last_result = result
            self.result_edit.setPlainText(text)
            self._refresh_result_preview(text)
        elif not text:
            # Model returned empty output — show a clear error instead of
            # leaving the stale "Working…" placeholder in place.
            self.result_edit.setPlainText(
                _('The AI provider returned an empty response. '
                  'Check the AI log tab for details and try again.')
            )
            self.result_pretty.clear()
        self._update_cache_callout()
        self._last_model_used = getattr(result, 'model_used', '') or ''
        self._last_provider_used = getattr(result, 'provider_used', '') or ''
        self.append_ai_log(_('Summary complete.'))
        self.update_effective_model_label()
        self.save_button.setEnabled(True)
        self.add_to_data_files_button.setEnabled(self._last_book_id is not None)
        self.add_to_library_button.setEnabled(True)
        self.comments_button.setEnabled(self._last_book_id is not None)
        self.output_tabs.setCurrentIndex(0)
        if self.auto_add_to_data_files.isChecked() and self._last_book_id is not None:
            self.add_result_to_book_data_files(silent=True)
        if pref_bool('append_to_comments', False) and self._last_book_id is not None:
            # In multi-file batch mode this runs once per finished book.
            # Always target the just-finished book explicitly so each book
            # only gets its own summary appended (matches the per-book
            # semantics of add_result_to_library above).
            self.append_result_to_comments(silent=True, book_id=self._last_book_id)
        if self.add_to_library_checkbox.isChecked():
            # In multi-file batch mode, ensure we add the summary for the
            # just-finished book (not a stale/overwritten _last_* state).
            self.add_result_to_library(
                silent=True,
                result=result,
                source_path=getattr(self, '_last_source_path', '') or '',
                source_title=getattr(self, '_last_source_title', '') or '',
                source_book_id=getattr(self, '_last_book_id', None),
            )
        # Multi-file batch: chain to next file or finish
        multi_total = getattr(self, '_multi_total', 0) or 0
        if multi_total > 1:
            self._multi_current = getattr(self, '_multi_current', 0) + 1
            self._multi_results.append(result)
            if getattr(self, '_multi_queue', None):
                QTimer.singleShot(500, self._on_multi_next)
                return
            else:
                # All done
                self.status_label.setText(_('Batch complete — %d file(s)') % self._multi_current)
                self.progress.setValue(100)
                try:
                    self.result_edit.setPlainText(_('Batch complete — %d file(s).\n\nThe Summary tab shows the last processed book. Use the AI log for per-book progress details.') % self._multi_current)
                    self._refresh_result_preview(self.result_edit.toPlainText())
                except Exception:
                    pass
                self._sync_primary_action_button(False)
                return
        self.status_label.setText(_('Summary complete'))
        self.progress.setValue(100)
        self._sync_primary_action_button(False)

    def on_cancelled(self, partial_result):
        self._worker = None
        self._history_finish_run('cancelled', result=partial_result)
        self._multi_queue = []
        self._multi_batch_cancelled = True
        self._sync_primary_action_button(False)
        self.status_label.setText(_('Summarization stopped'))
        self.append_ai_log(_('Summarization stopped by user.'))
        if partial_result is not None:
            self._store_result_in_chapter_cache(partial_result)
            self._last_result = partial_result
            self._last_model_used = getattr(partial_result, 'model_used', '') or ''
            self._last_provider_used = getattr(partial_result, 'provider_used', '') or ''
            self.result_edit.setPlainText(partial_result.text)
            self._refresh_result_preview(partial_result.text)
            self.save_button.setEnabled(True)
            self.add_to_data_files_button.setEnabled(self._last_book_id is not None)
            self.add_to_library_button.setEnabled(True)
            self.comments_button.setEnabled(self._last_book_id is not None)
            self.output_tabs.setCurrentIndex(0)
            self.update_effective_model_label()
        else:
            self.save_button.setEnabled(False)
            self.add_to_data_files_button.setEnabled(False)
            self.add_to_library_button.setEnabled(False)
            self.comments_button.setEnabled(False)
            self.output_tabs.setCurrentIndex(2)

    def on_error(self, message: str, details: str, partial_result):
        self._worker = None
        self._history_finish_run('error', result=partial_result, error_message=message)
        self._multi_queue = []
        self._multi_batch_cancelled = True
        self._sync_primary_action_button(False)
        self.status_label.setText(_('Summarization failed'))
        self.progress.setValue(0)
        self.append_ai_log(_('Summarization failed: %s') % message)
        self.record_ai_error('summarize', message, details)
        if partial_result is not None:
            self._store_result_in_chapter_cache(partial_result)
            self._last_result = partial_result
            self._last_model_used = getattr(partial_result, 'model_used', '') or ''
            self._last_provider_used = getattr(partial_result, 'provider_used', '') or ''
            self.result_edit.setPlainText(partial_result.text)
            self._refresh_result_preview(partial_result.text)
            self.save_button.setEnabled(True)
            self.add_to_data_files_button.setEnabled(self._last_book_id is not None)
            self.add_to_library_button.setEnabled(True)
            self.output_tabs.setCurrentIndex(0)
            self.append_ai_log(_('Partial summary preserved for inspection and saving.'))
            self.update_effective_model_label()
        else:
            self.output_tabs.setCurrentIndex(2)
            self.add_to_data_files_button.setEnabled(False)
            self.add_to_library_button.setEnabled(False)
        error_dialog(self, _('Summarization failed'), message, show=True, det_msg=details)

    def _build_summary_temp_output(self, desired: str | None = None, result=None) -> tuple[str, str]:
        if result is None:
            result = self._last_result
        if result is None:
            return '', ''
        text = (getattr(result, 'text', '') or '').strip()
        if not text:
            return '', ''
        desired = str(desired or pref('add_to_library_format', 'epub') or 'epub').strip().lower()
        if desired in {'epub'}:
            ext = 'epub'
        elif desired in {'html', 'htm'}:
            ext = 'html'
        else:
            ext = 'txt'
        base = _safe_filename_component(os.path.splitext(self.default_summary_filename())[0], 'summary')
        path = os.path.join(tempfile.gettempdir(), f'chapter_summarizer_library_{base}_{_timestamp_suffix()}.{ext}')
        if ext == 'txt':
            with open(path, 'w', encoding='utf-8') as f:
                f.write(text + '\n')
            return path, 'txt'
        html_doc = self._current_epub_summary_html(text)
        if ext == 'html':
            with open(path, 'w', encoding='utf-8') as f:
                f.write(html_doc)
            return path, 'html'
        ok, err = export_html_to_epub(html_doc, path)
        if not ok:
            raise ValueError(str(err))
        return path, 'epub'

    def add_result_to_library(
        self,
        silent: bool = False,
        *,
        result=None,
        source_path: str | None = None,
        source_title: str | None = None,
        source_book_id=None,
    ):
        if result is None:
            result = self._last_result
        if result is None:
            return
        temp_path = ''
        try:
            temp_path, fmt = self._build_summary_temp_output(result=result)
            if not temp_path:
                return
            # Use the stored source info from the run that produced _last_result,
            # NOT resolve_source() which reads the current calibre selection
            # (critical for multi-file batch where the selection doesn't change).
            if source_path is None:
                source_path = self._last_source_path or ''
            if source_title is None:
                source_title = self._last_source_title or ''
            if source_book_id is None:
                source_book_id = self._last_book_id
            title = format_summary_book_title(source_title or _('Summary'))
            metadata = MetaInformation(title, [_('Chapter Summarizer')])
            base_tags = [_('Chapter Summarizer'), _('Summary')]
            custom_raw = str(pref('summary_custom_tags', '') or '').strip()
            custom_tags = [t.strip() for t in re.split(r'[,;\n]+', custom_raw) if t.strip()] if custom_raw else []
            merged_tags: list[str] = []
            seen: set[str] = set()
            for t in base_tags + custom_tags:
                key = t.casefold()
                if key in seen:
                    continue
                seen.add(key)
                merged_tags.append(t)
            metadata.tags = merged_tags

            source_cover_bytes = None
            if source_book_id is not None and pref_bool('summary_copy_source_cover', True):
                try:
                    src_mi = self._get_metadata(source_book_id, get_cover=True)
                    cover_data = getattr(src_mi, 'cover_data', (None, None)) or (None, None)
                    if len(cover_data) > 1:
                        source_cover_bytes = cover_data[1]
                except Exception:
                    source_cover_bytes = None
            if source_book_id is not None:
                try:
                    src_mi = self._get_metadata(source_book_id, get_cover=False)
                    authors = list(getattr(src_mi, 'authors', []) or [])
                    if authors:
                        metadata.authors = authors
                    langs = list(getattr(src_mi, 'languages', []) or [])
                    target_lang = normalize_target_language(self.target_language.currentData() or 'same-as-source')
                    if target_lang not in {'', 'same-as-source'}:
                        metadata.languages = [target_lang]
                    elif langs:
                        metadata.languages = langs
                except Exception:
                    pass
            else:
                target_lang = normalize_target_language(self.target_language.currentData() or 'same-as-source')
                if target_lang not in {'', 'same-as-source'}:
                    metadata.languages = [target_lang]

            summary_cover_bytes = generate_summary_cover_bytes(title, list(getattr(metadata, 'authors', []) or []), source_cover_bytes=source_cover_bytes)
            if summary_cover_bytes:
                try:
                    metadata.cover_data = ('png', summary_cover_bytes)
                except Exception:
                    pass

            db_model = self.gui.library_view.model().db
            _duplicates, added_ids = db_model.add_books([temp_path], [fmt], [metadata], add_duplicates=True, return_ids=True)
            if isinstance(added_ids, (list, tuple)):
                book_id = added_ids[0] if added_ids else None
            else:
                book_id = added_ids
            if book_id is None:
                raise ValueError('Could not determine the newly added summary book ID.')

            if summary_cover_bytes:
                try:
                    self.db.set_cover({book_id: summary_cover_bytes})
                except Exception:
                    try:
                        self.db.set_cover(book_id, summary_cover_bytes)
                    except Exception:
                        try:
                            db_model.set_cover(book_id, summary_cover_bytes)
                        except Exception:
                            pass
            try:
                self.gui.library_view.model().books_added(1)
                self.gui.library_view.model().refresh_ids([book_id])
            except Exception:
                pass
            try:
                self.gui.status_bar.show_message(_('Summary added to library: %s') % title, 5000)
            except Exception:
                pass
            if not silent:
                info_dialog(self, _('Added'), _('Summary added to library as a new book:\n%s') % title, show=True)
        except Exception as e:
            if not silent:
                error_dialog(self, _('Add to library failed'), _('Could not add the summary as a new book.'), det_msg=str(e), show=True)
        finally:
            if temp_path:
                try:
                    os.remove(temp_path)
                except Exception:
                    pass

    def save_result(self):
        if self._last_result is None:
            return
        default_name = self.default_summary_filename()
        path = choose_save_file(self, _('Save summary'), _('Markdown files (*.md);;Text files (*.txt);;All files (*)'), initial_filename=default_name)
        if not path:
            return
        with open(path, 'w', encoding='utf-8') as f:
            f.write(self._last_result.text)
        info_dialog(self, _('Saved'), _('Summary saved successfully.'), show=True)

    def add_result_to_book_data_files(self, silent: bool = False):
        if self._last_result is None or self._last_book_id is None:
            return
        text = (self._last_result.text or '').strip()
        if not text:
            return

        base_name = os.path.splitext(self.default_summary_filename())[0]
        desired = str(pref('data_files_format', 'md') or 'md').strip().lower()
        ext = 'md'
        if desired in {'html', 'htm'}:
            ext = 'html'
        elif desired in {'epub'}:
            ext = 'epub'

        filename = f'{base_name}.{ext}'
        relpath = '/'.join([DATA_DIR_NAME, 'chapter_summarizer', filename])

        tmp_path = ''
        stream_or_path = None
        if ext == 'md':
            stream_or_path = BytesIO((text + '\n').encode('utf-8'))
        elif ext == 'html':
            html_doc = self._current_epub_summary_html(text)
            stream_or_path = BytesIO(html_doc.encode('utf-8'))
        else:
            # epub
            html_doc = self._current_epub_summary_html(text)
            tmp_path = os.path.join(tempfile.gettempdir(), f'chapter_summarizer_datafile_{_timestamp_suffix()}.epub')
            ok, err = export_html_to_epub(html_doc, tmp_path)
            if not ok:
                if not silent:
                    error_dialog(self, _('Failed'), _('Could not export EPUB for data files.'), det_msg=str(err), show=True)
                return
            stream_or_path = tmp_path

        try:
            added = self.db.add_extra_files(self._last_book_id, {relpath: stream_or_path}, replace=False, auto_rename=True)
        except TypeError:
            if hasattr(stream_or_path, 'seek'):
                try:
                    stream_or_path.seek(0)
                except Exception:
                    pass
            added = self.db.add_extra_files(self._last_book_id, {relpath: stream_or_path})
        except Exception as e:
            if not silent:
                error_dialog(self, _('Failed'), _('Could not add the summary to the book data files.'), det_msg=str(e), show=True)
            return
        finally:
            if tmp_path:
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass

        ok = False
        try:
            ok = bool((added or {}).get(relpath))
        except Exception:
            ok = True

        fmt_path, fmt = self.resolve_book_path(self._last_book_id)
        abs_path = ''
        if fmt_path:
            abs_path = os.path.join(os.path.dirname(fmt_path), relpath.replace('/', os.sep))
        file_url = ''
        if abs_path and os.path.exists(abs_path):
            file_url = 'file:///' + abs_path.replace('\\', '/').lstrip('/')

        if ok:
            try:
                model = self.gui.library_view.model()
                if model is not None and hasattr(model, 'refresh_ids'):
                    model.refresh_ids([self._last_book_id])
            except Exception:
                pass
            try:
                self.refresh_selected_book()
            except Exception:
                pass
            # Deferred refresh so the calibre book details panel picks up
            # the new data files entry.
            try:
                QTimer.singleShot(300, lambda bid=self._last_book_id: self._refresh_book_details_panel(bid))
            except Exception:
                pass
            try:
                QApplication.processEvents()
            except Exception:
                pass
            if not silent:
                msg = _('Summary added to book data files.')
                if abs_path:
                    msg += '\n\n' + abs_path
                info_dialog(self, _('Added'), msg, show=True)
        else:
            if not silent:
                error_dialog(self, _('Failed'), _('The summary was not added.'), show=True)

    def default_summary_filename(self) -> str:
        title = ''
        details = self.selected_book_details()
        if details is not None:
            title = details.get('title', '')
        if not title and self._last_result is not None:
            first_line = (self._last_result.text or '').splitlines()[0:1]
            if first_line:
                title = first_line[0].lstrip('#').strip()
        title = _safe_filename_component(title, 'summary')
        is_partial = bool(self._last_result and (self._last_result.text or '').startswith('[PARTIAL RESULT]'))
        is_flash = bool(getattr(self, '_last_result_is_flash', False))
        if is_flash:
            prefix = 'flash-summary-partial' if is_partial else 'flash-summary'
        else:
            prefix = 'chapter-summary-partial' if is_partial else 'chapter-summary'
        return f'{prefix}-{title}-{_timestamp_suffix()}.md'

    def append_result_to_comments(self, silent: bool = False, book_id=None):
        if self._last_result is None:
            return

        # Determine the target field: custom column or built-in comments
        custom_column = str(pref('custom_append_column', '') or '').strip()
        target_field = custom_column if custom_column else 'comments'

        # Get field name for display in prompts
        field_metadata = self.db.field_metadata
        if custom_column and custom_column in field_metadata:
            field_name = field_metadata[custom_column]['name']
        else:
            field_name = _('comments')

        # Always target the book whose summary is currently displayed.
        # In multi-file batch mode on_finished() invokes this once per book
        # and passes book_id=self._last_book_id explicitly; the manual button
        # leaves it None and we fall back to _last_book_id. We intentionally
        # do NOT iterate the calibre multi-selection here: every book in the
        # batch must receive only its own summary, never the others'.
        if book_id is None:
            book_id = self._last_book_id
        if book_id is None:
            return
        target_ids: list[int] = [int(book_id)]

        if not silent:
            prompt = _('Append the generated summary to the %s field of the current book?') % field_name
            if not question_dialog(self, _('Append to %s') % field_name, prompt):
                return

        summary_html = comments_to_html(self._summary_text_for_comments(self._last_result.text))
        updates: dict[int, str] = {}
        for book_id in target_ids:
            try:
                mi = self.db.get_metadata(book_id, get_cover=False)
                if custom_column:
                    # Get existing value from custom column
                    existing = mi.get(custom_column, '') or ''
                else:
                    # Get existing value from built-in comments
                    existing = getattr(mi, 'comments', '') or ''
                combined = existing + ('<hr />' if existing.strip() else '') + summary_html
                updates[int(book_id)] = combined
            except Exception:
                continue
        if not updates:
            return
        self.db.set_field(target_field, updates)
        if hasattr(self.gui, 'library_view'):
            try:
                self.gui.library_view.model().refresh_ids(list(updates.keys()))
            except Exception:
                pass
            # Deferred refresh so the calibre book details panel picks up
            # the updated field.
            try:
                for bid in updates:
                    QTimer.singleShot(300, lambda b=bid: self._refresh_book_details_panel(b))
            except Exception:
                pass
        if not silent:
            if len(updates) > 1:
                info_dialog(self, _('%s updated') % field_name, _('The generated summary was appended to the %s field for %d books.') % (field_name, len(updates)), show=True)
            else:
                info_dialog(self, _('%s updated') % field_name, _('The generated summary was appended to the %s field.') % field_name, show=True)

    def _summary_text_for_comments(self, text: str) -> str:
        text = str(text or '').strip()
        if not text:
            return ''
        partial_prefix = ''
        if text.startswith('[PARTIAL RESULT]'):
            parts = text.split('\n\n', 1)
            partial_prefix = parts[0].strip()
            text = parts[1].strip() if len(parts) > 1 else ''
        match = re.search(r'(?m)^##\s+', text)
        if match:
            text = text[match.start():].strip()
        else:
            text = re.sub(r'(?s)^# .*?\n\s*(?:- .*?\n)+\s*', '', text).strip()
        if partial_prefix and text:
            return partial_prefix + '\n\n' + text
        return text or partial_prefix

    def persist_ui_state(self):
        set_pref('provider_name', self.provider_combo.currentData() or self.provider_combo.currentText().strip())
        set_pref('base_paragraphs', self.base_paragraphs.value())
        max_chunk_words_spin = getattr(self, 'max_chunk_words_spin', None)
        if max_chunk_words_spin is not None:
            set_pref('max_chunk_words', int(max_chunk_words_spin.value()))
        set_pref('include_highlight', self.include_highlight.isChecked())
        set_pref('auto_add_to_data_files', self.auto_add_to_data_files.isChecked())
        set_pref('add_to_library', self.add_to_library_checkbox.isChecked())
        set_pref('target_language', self.target_language.currentData() or 'same-as-source')
        set_pref('flash_summary_mode', self.flash_summary_check.isChecked())

    def reapply_runtime_prefs(self):
        """Re-read runtime prefs into the live widgets.

        Called once after the dialog is first constructed so that prefs saved
        by a prior run (or changed externally) are reflected. The main dialog
        is cached for the calibre session (see ChapterSummarizerAction.show_dialog),
        so without this re-sync the spinbox/checkbox keep whatever value they
        had at construction time — even if the stored pref changed underneath.
        """
        try:
            self.base_paragraphs.setValue(int(pref('base_paragraphs', 2)))
        except Exception:
            pass
        try:
            self.include_highlight.setChecked(pref_bool('include_highlight', True))
        except Exception:
            pass
        max_chunk_words_spin = getattr(self, 'max_chunk_words_spin', None)
        if max_chunk_words_spin is not None:
            try:
                max_chunk_words_spin.setValue(int(pref('max_chunk_words', 1400)))
            except Exception:
                pass

    def append_ai_log(self, message: str):
        if not message:
            return
        text = str(message)
        if not text.startswith('['):
            text = f'[{_now_stamp()}] {text}'
        self._session_ai_log.append(text)
        if len(self._session_ai_log) > 400:
            self._session_ai_log = self._session_ai_log[-400:]
        self.refresh_ai_log_view()

    def _stored_ai_errors(self):
        try:
            return list(pref('ai_error_log', []) or [])
        except Exception:
            return []

    def record_ai_error(self, where: str, err: str, details: str = ''):
        entries = self._stored_ai_errors()
        entries.append({
            'ts': _now_stamp(),
            'where': where,
            'err': err,
            'details': details,
        })
        set_pref('ai_error_log', entries[-100:])
        self.refresh_ai_log_view()

    def _format_stored_ai_errors(self) -> str:
        entries = self._stored_ai_errors()
        if not entries:
            return _('No AI errors logged.')
        lines = []
        for entry in reversed(entries):
            block = []
            ts = str((entry or {}).get('ts') or '')
            where = str((entry or {}).get('where') or '')
            err = str((entry or {}).get('err') or '')
            details = str((entry or {}).get('details') or '')
            if ts or where:
                block.append(f'[{ts}] {where}'.strip())
            if err:
                block.append(err)
            if details:
                block.append(details)
            if block:
                lines.append('\n'.join(block))
        return '\n\n' + ('\n\n' + ('-' * 60) + '\n\n').join(lines)

    def refresh_ai_log_view(self):
        session_count = len(self._session_ai_log)
        stored_count = len(self._stored_ai_errors())
        self.ai_log_count_lbl.setText(_('Session: %d • Stored errors: %d') % (session_count, stored_count))
        session_text = '\n'.join(reversed(self._session_ai_log)) if self._session_ai_log else _('No AI activity yet.')
        stored_text = self._format_stored_ai_errors()
        text = _('Session log') + '\n' + ('=' * 60) + '\n' + session_text + '\n\n' + _('Stored AI errors (historical)') + '\n' + ('=' * 60) + '\n' + stored_text
        self.ai_log_view.setPlainText(text)
        cursor = self.ai_log_view.textCursor()
        cursor.movePosition(cursor.MoveOperation.Start)
        self.ai_log_view.setTextCursor(cursor)
        self.ai_log_view.verticalScrollBar().setValue(0)

    def copy_ai_log(self):
        QApplication.clipboard().setText(self.ai_log_view.toPlainText())
        self.append_ai_log(_('AI log copied to clipboard.'))

    def save_ai_log(self):
        default_name = f'chapter-summarizer-ai-log-{_timestamp_suffix()}.txt'
        path = choose_save_file(self, _('Save AI log'), _('Text files (*.txt);;All files (*)'), initial_filename=default_name)
        if not path:
            return
        with open(path, 'w', encoding='utf-8') as f:
            f.write(self.ai_log_view.toPlainText())
        self.append_ai_log(_('AI log saved to: %s') % path)

    def clear_ai_log(self):
        self._session_ai_log = []
        set_pref('ai_error_log', [])
        self.refresh_ai_log_view()

    def reject(self):
        if self._worker is not None and self._worker.is_running():
            self.stop_summary()
            self.hide()
            return
        super().reject()
