#!/usr/bin/env python

from __future__ import annotations

__license__ = 'GPL v3'
__copyright__ = '2026, Comfy.n'
__docformat__ = 'restructuredtext en'

import datetime
import textwrap
from threading import Thread
from typing import TYPE_CHECKING, Any

from qt.core import (
    QAbstractItemView,
    QAbstractListModel,
    QApplication,
    QComboBox,
    QCursor,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QIcon,
    QLabel,
    QLineEdit,
    QListView,
    QLocale,
    QModelIndex,
    QPalette,
    QSize,
    QSortFilterProxyModel,
    QSplitter,
    Qt,
    QTextBrowser,
    QTimer,
    QUrl,
    QVBoxLayout,
    QWidget,
    pyqtSignal,
)

import time

from calibre.ebooks.txt.processor import create_markdown_object
from calibre.gui2 import gprefs, safe_open_url
from calibre.gui2.widgets2 import Dialog

from calibre_plugins.chapter_summarizer.nvidia_free import debug_log, get_available_models_rich

try:
    load_translations()
except NameError:
    pass

if TYPE_CHECKING:
    NvidiaModel = dict[str, Any]


class Spinner(QLabel):
    frames = ('⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏')

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setTextFormat(Qt.TextFormat.RichText)
        self.timer = t = QTimer(self)
        t.setInterval(120)
        t.timeout.connect(self.update_spinner)
        self.frame = -1
        self.started_at = 0

    def start(self):
        self.frame = -1
        self.started_at = time.monotonic()
        self.timer.start()

    def stop(self):
        self.timer.stop()
        self.setText('')

    def update_spinner(self):
        self.frame = (self.frame + 1) % len(self.frames)
        self.setText(self.frames[self.frame])


def _dt_from_created(created: Any) -> datetime.datetime | None:
    if created is None:
        return None
    if isinstance(created, datetime.datetime):
        return created
    if isinstance(created, datetime.date):
        try:
            tz = getattr(datetime, 'UTC', None) or datetime.timezone.utc
            return datetime.datetime.combine(created, datetime.time(0, 0), tzinfo=tz)
        except Exception:
            return None
    if isinstance(created, (int, float)):
        try:
            ts = float(created)
            if ts > 1e12:
                ts = ts / 1000.0
            tz = getattr(datetime, 'UTC', None) or datetime.timezone.utc
            return datetime.datetime.fromtimestamp(ts, tz=tz)
        except Exception:
            return None
    try:
        if isinstance(created, str) and created.strip():
            s = created.strip().replace('Z', '+00:00')
            try:
                return datetime.datetime.fromisoformat(s)
            except ValueError:
                pass
            tz = getattr(datetime, 'UTC', None) or datetime.timezone.utc
            for fmt in ('%m/%d/%Y', '%m/%d/%Y %H:%M:%S'):
                try:
                    return datetime.datetime.strptime(s.split('+')[0].strip(), fmt).replace(tzinfo=tz)
                except ValueError:
                    pass
    except Exception:
        return None
    return None


def _dt_from_updated(updated: Any) -> datetime.datetime | None:
    if not updated:
        return None
    try:
        if isinstance(updated, datetime.datetime):
            return updated
        if isinstance(updated, datetime.date):
            tz = getattr(datetime, 'UTC', None) or datetime.timezone.utc
            return datetime.datetime.combine(updated, datetime.time(0, 0), tzinfo=tz)
        if isinstance(updated, (int, float)):
            ts = float(updated)
            if ts > 1e12:
                ts = ts / 1000.0
            tz = getattr(datetime, 'UTC', None) or datetime.timezone.utc
            return datetime.datetime.fromtimestamp(ts, tz=tz)
        if isinstance(updated, str):
            return datetime.datetime.fromisoformat(updated.strip().replace('Z', '+00:00'))
    except Exception:
        return None
    return None


def _model_name_key(model: dict[str, Any]) -> tuple[str, str]:
    return (
        str(model.get('name') or model.get('id') or '').lower(),
        str(model.get('id') or '').lower(),
    )


def _model_recency_timestamp(model: dict[str, Any]) -> float | None:
    """Return the newest known release/update timestamp for a model."""
    created_dt = _dt_from_created(model.get('created'))
    updated_dt = _dt_from_updated(model.get('updated_date'))
    dates = tuple(x for x in (created_dt, updated_dt) if x is not None)
    return max(dates).timestamp() if dates else None


class ModelsModel(QAbstractListModel):

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.all_models = ()
        self.refresh(use_cache=True)

    def refresh(self, use_cache: bool = True) -> None:
        # GUI-thread callers use cache/disk only; network work runs in warmup.
        raw_models = get_available_models_rich(use_cache=use_cache, allow_network=False) or ()
        normalised = []
        seen: set[str] = set()
        for m in raw_models:
            entry = m if isinstance(m, dict) else {'id': str(m), 'name': str(m)}
            mid = str(entry.get('id') or '').strip().lower()
            if mid:
                if mid in seen:
                    continue
                seen.add(mid)
            normalised.append(entry)

        normalised.sort(key=_model_name_key)
        self.beginResetModel()
        self.all_models = tuple(normalised)
        self.endResetModel()

        free_count = sum(1 for m in self.all_models if m.get('is_free') is True)
        described_count = sum(1 for m in self.all_models if str(m.get('description') or '').strip())
        debug_log(f'Dialog model list initialised total={len(self.all_models)} free={free_count} described={described_count}')

    def apply_sort(self, sort_key: str) -> None:
        """Reorder source rows for one of the four UI sort modes."""
        if sort_key in ('name-asc', 'name-desc'):
            ordered = sorted(
                self.all_models,
                key=_model_name_key,
                reverse=sort_key == 'name-desc',
            )
        else:
            dated: list[tuple[float, dict[str, Any]]] = []
            undated: list[dict[str, Any]] = []
            for model in self.all_models:
                timestamp = _model_recency_timestamp(model)
                if timestamp is None:
                    undated.append(model)
                else:
                    dated.append((timestamp, model))
            if sort_key == 'updated-desc':
                dated.sort(key=lambda item: (-item[0],) + _model_name_key(item[1]))
            else:
                dated.sort(key=lambda item: (item[0],) + _model_name_key(item[1]))
            undated.sort(key=_model_name_key)
            ordered = [model for _timestamp, model in dated] + undated

        self.beginResetModel()
        self.all_models = tuple(ordered)
        self.endResetModel()

    def rowCount(self, parent):
        return len(self.all_models)

    def data(self, index, role):
        try:
            m = self.all_models[index.row()]
        except Exception:
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            return str(m.get('name') or m.get('id') or '')
        if role == Qt.ItemDataRole.UserRole:
            return m
        return None


class ProxyModels(QSortFilterProxyModel):

    def __init__(self, parent=None):
        super().__init__(parent)
        self.source_model = ModelsModel(self)
        self.setSourceModel(self.source_model)
        self.filters = []

    def filterAcceptsRow(self, source_row: int, source_parent) -> bool:
        try:
            m = self.source_model.all_models[source_row]
        except Exception:
            return False
        for f in self.filters:
            if not f(m):
                return False
        return True

    def set_filters(self, *filters):
        self.filters = filters
        try:
            self.invalidateFilter()
        except Exception:
            self.invalidate()

    def set_sort(self, sort_key: str):
        self.source_model.apply_sort(sort_key)
        try:
            self.invalidateFilter()
        except Exception:
            self.invalidate()

    def index_for_model_id(self, model_id: str) -> QModelIndex:
        model_id = str(model_id or '').strip()
        if not model_id:
            return QModelIndex()
        for i in range(self.rowCount(QModelIndex())):
            ans = self.index(i, 0)
            m = ans.data(Qt.ItemDataRole.UserRole) or {}
            if str(m.get('id') or '').strip() == model_id:
                return ans
        return QModelIndex()


class ModelDetails(QTextBrowser):

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setOpenLinks(False)
        self.anchorClicked.connect(self.open_link)
        try:
            # Enable word wrapping to prevent horizontal scrollbars
            self.setLineWrapMode(QTextBrowser.LineWrapMode.WidgetWidth)
        except Exception:
            pass
        try:
            self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        except Exception:
            pass
        self.show_help()

    def show_help(self):
        self.setText(f'''
        <p>{_('Pick an AI model to use. The NVIDIA models list is fetched from the NVIDIA API, then enriched with metadata from the public NVIDIA Build catalogue and cached locally.')}</p>
        <p>{_('The details pane shows best-effort availability badges, publisher, use cases, and recency information gathered from NVIDIA Build pages.')}</p>
        ''')

    def show_model_details(self, m: NvidiaModel):
        """Display rich model details with theme-responsive formatting."""
        md = create_markdown_object(extensions=())
        name = str(m.get('name') or m.get('id') or '')
        model_id = str(m.get('id') or '')
        desc = str(m.get('description') or '')
        ctx = int(m.get('context_length') or 0)
        created_dt = _dt_from_created(m.get('created'))
        created_txt = ''
        if created_dt is not None:
            try:
                from calibre.utils.date import qt_from_dt
                created = qt_from_dt(created_dt).date()
                created_txt = QLocale.system().toString(created, QLocale.FormatType.ShortFormat)
            except Exception:
                created_txt = created_dt.isoformat()
        if not created_txt:
            raw_created = m.get('created')
            if raw_created:
                created_txt = str(raw_created).strip()

        # Detect theme for color scheme
        is_dark_theme = self._is_dark_theme()

        # Present tri-state labels: yes / no / unknown. Treat missing keys as unknown.
        fv = m.get('is_free') if 'is_free' in m else None
        pv = m.get('has_partner_endpoint') if 'has_partner_endpoint' in m else None
        dv = m.get('is_downloadable') if 'is_downloadable' in m else None

        # Build availability badges with rich formatting
        badges = []
        if fv is True:
            badge_bg = '#2d6a3e' if is_dark_theme else '#28a745'
            badges.append(f'<span style="display: inline-block; padding: 4px 10px; border-radius: 4px; '
                         f'background-color: {badge_bg}; color: white; font-size: 9pt; font-weight: 600; '
                         f'margin: 0 12px 8px 0;">✓ {_("Free")}</span>')
        elif fv is False:
            badge_bg = '#6c757d' if is_dark_theme else '#6c757d'
            badges.append(f'<span style="display: inline-block; padding: 4px 10px; border-radius: 4px; '
                         f'background-color: {badge_bg}; color: white; font-size: 9pt; font-weight: 600; '
                         f'margin: 0 12px 8px 0; opacity: 0.7;">✗ {_("Paid")}</span>')

        if pv is True:
            badge_bg = '#0d5aa7' if is_dark_theme else '#007bff'
            badges.append(f'<span style="display: inline-block; padding: 4px 10px; border-radius: 4px; '
                         f'background-color: {badge_bg}; color: white; font-size: 9pt; font-weight: 600; '
                         f'margin: 0 12px 8px 0;">{_("Partner API")}</span>')

        if dv is True:
            badge_bg = '#117a8b' if is_dark_theme else '#17a2b8'
            badges.append(f'<span style="display: inline-block; padding: 4px 10px; border-radius: 4px; '
                         f'background-color: {badge_bg}; color: white; font-size: 9pt; font-weight: 600; '
                         f'margin: 0 12px 8px 0;">↓ {_("Downloadable")}</span>')

        badge_sep = '<span style="display: inline-block; width: 10px;">&nbsp;</span>'
        badges_html = badge_sep.join(badges) if badges else ''
        # Add a non‑breaking space after each badge to guarantee visual separation in Qt HTML rendering.
        badges_html = badges_html.replace('</span>', '</span>&nbsp;')

        publisher = str(m.get('publisher') or '')
        updated_dt = _dt_from_updated(m.get('updated_date'))
        updated_label = ''
        if updated_dt is not None:
            try:
                from calibre.utils.date import qt_from_dt
                updated = qt_from_dt(updated_dt).date()
                updated_label = QLocale.system().toString(updated, QLocale.FormatType.ShortFormat)
            except Exception:
                updated_label = updated_dt.isoformat()
        if not updated_label:
            raw_updated = str(m.get('updated_date') or '')
            if raw_updated:
                if 'T' in raw_updated:
                    raw_updated = raw_updated.split('T', 1)[0].strip()
                updated_label = raw_updated.rstrip('Z').strip()
        use_cases = m.get('use_cases') if isinstance(m.get('use_cases'), (list, tuple)) else ()
        use_cases_txt = ', '.join(str(x) for x in use_cases if str(x).strip())

        raw = m.get('raw') if isinstance(m.get('raw'), dict) else {}
        # Best-effort fields
        tokenizer = str(raw.get('tokenizer') or raw.get('architecture', {}).get('tokenizer') or '')
        input_mod = raw.get('architecture', {}).get('input_modalities') if isinstance(raw.get('architecture'), dict) else None
        output_mod = raw.get('architecture', {}).get('output_modalities') if isinstance(raw.get('architecture'), dict) else None
        if isinstance(input_mod, (list, tuple)):
            input_mod = ', '.join(map(str, input_mod))
        else:
            input_mod = str(input_mod or '')
        if isinstance(output_mod, (list, tuple)):
            output_mod = ', '.join(map(str, output_mod))
        else:
            output_mod = str(output_mod or '')

        # Build detail rows with rich formatting
        detail_rows = []

        def add_detail(label, value, emphasize=False):
            if not value:
                return
            weight = '700' if emphasize else '600'
            detail_rows.append(
                f'<div style="padding: 10px 0; border-bottom: 1px solid palette(mid);">'
                f'<div style="font-weight: {weight}; color: palette(text); margin-bottom: 4px;">{label}:</div>'
                f'<div style="color: palette(text); margin-left: 8px; white-space: pre-wrap; word-wrap: break-word; overflow-wrap: anywhere;">{value}</div>'
                f'</div>'
            )

        if publisher:
            add_detail(_('Publisher'), publisher, emphasize=True)
        if ctx:
            ctx_formatted = QLocale.system().toString(ctx)
            add_detail(_('Context Length'), _('%s tokens') % ctx_formatted, emphasize=True)
        if created_txt:
            add_detail(_('Released'), created_txt)
        if updated_label:
            add_detail(_('Last Updated'), updated_label)
        if use_cases_txt:
            add_detail(_('Use Cases'), f'<i>{use_cases_txt}</i>')
        if tokenizer:
            add_detail(_('Tokenizer'), f'<code>{tokenizer}</code>')
        if input_mod:
            add_detail(_('Input Modalities'), input_mod)
        if output_mod:
            add_detail(_('Output Modalities'), output_mod)

        add_detail(_('Model ID'), f'<code style="font-size: 8pt; color: palette(mid); white-space: pre-wrap; word-wrap: break-word; overflow-wrap: anywhere;">{model_id}</code>')

        details_html = ''.join(detail_rows)

        # Link to NVIDIA docs (best-effort). If the stored URL is missing or points to the generic models list,
        # construct a model‑specific URL using the publisher and build slugs.
        link = str(m.get('build_url') or '').strip()
        if not link or link.endswith('/models'):
            publisher_slug = str(m.get('publisher_slug') or '').strip()
            build_slug = str(m.get('build_slug') or '').strip()
            if publisher_slug and build_slug:
                link = f'https://build.nvidia.com/{publisher_slug}/{build_slug}'
            else:
                link = 'https://build.nvidia.com/models'
        link_color = '#6cb3ff' if is_dark_theme else '#007bff'

        # Description with markdown support
        desc_html = md.convert(desc) if desc else f'<p style="color: palette(mid); font-style: italic;">{_("No description available")}</p>'

        html = f'''
        <div style="padding: 16px;">
            <div style="margin-bottom: 24px;">
                <h2 style="margin: 0 0 14px 0; padding: 0; font-size: 16pt; font-weight: 700; word-wrap: break-word; overflow-wrap: anywhere;">{name}</h2>
                {f'<div style="margin-bottom: 16px; line-height: 1.8;">{badges_html}</div>' if badges_html else ''}
            </div>

            <div style="margin-bottom: 28px; padding: 16px; background-color: palette(base);
                        border-left: 3px solid palette(highlight); border-radius: 4px;
                        word-wrap: break-word; overflow-wrap: anywhere; white-space: normal;">
                {desc_html}
            </div>

            <h3 style="font-size: 11pt; font-weight: 600; margin: 24px 0 14px 0;
                       border-bottom: 2px solid palette(mid); padding-bottom: 8px;">
                {_('Technical Details')}
            </h3>
            <div style="margin-bottom: 28px;">
                {details_html}
            </div>

            <p style="margin-top: 28px; padding-top: 16px; border-top: 1px solid palette(mid);">
                <a href="{link}" style="color: {link_color}; text-decoration: none; font-weight: 600;">
                    {_('View complete documentation on NVIDIA Build')} →
                </a>
            </p>
        </div>
        '''
        self.setText(html)

    def _is_dark_theme(self) -> bool:
        """Detect if the current theme is dark mode."""
        try:
            app = QApplication.instance()
            if app is None:
                return False
            palette = app.palette()
            window_color = palette.color(QPalette.ColorRole.Window)
            text_color = palette.color(QPalette.ColorRole.WindowText)
            return text_color.lightness() > window_color.lightness()
        except Exception:
            return False

    def sizeHint(self):
        return QSize(380, 520)

    def open_link(self, url: QUrl):
        safe_open_url(url)


class SortLoc(QComboBox):
    """Single sort dropdown offering Name and Updated sort options.

    Each item stores a sort-key string that encodes both the field and the
    direction (e.g. ``'name-asc'``, ``'updated-desc'``).  The
    :attr:`sort_key_func` property returns a callable suitable for
    :meth:`ProxyModels.set_sorts`.
    """

    def __init__(self, initial='', parent=None):
        super().__init__(parent)
        self.addItem(_('Name (A → Z)'), 'name-asc')
        self.addItem(_('Name (Z → A)'), 'name-desc')
        self.addItem(_('Recent (newest first)'), 'updated-desc')
        self.addItem(_('Recent (oldest first)'), 'updated-asc')
        if (idx := self.findData(initial or 'updated-desc')) > -1:
            self.setCurrentIndex(idx)

    @property
    def sort_key(self) -> str:
        return self.currentData() or 'updated-desc'

    @property
    def sort_key_func(self):
        key = self.sort_key
        if key == 'name-asc':
            return lambda x: str(x.get('name') or x.get('id') or '').lower()
        if key == 'name-desc':
            # Return a tuple (0, name) so that _to_sort_tuple keeps it as a
            # tuple.  We negate the string sort by wrapping it: the
            # ProxyModels.lessThan will be told to use DescendingOrder for
            # name-desc, so we just return the plain name and let the order
            # flag handle reversal.
            return lambda x: str(x.get('name') or x.get('id') or '').lower()
        if key == 'updated-desc':
            def _updated_desc(x):
                # For recency sorting, use the most recent of created (release date) or updated_date
                # This ensures recently released models appear alongside recently updated ones
                created_dt = _dt_from_created(x.get('created'))
                updated_dt = _dt_from_updated(x.get('updated_date'))

                # Pick the most recent date between created and updated
                best_dt = None
                if created_dt is not None and updated_dt is not None:
                    best_dt = max(created_dt, updated_dt)
                elif created_dt is not None:
                    best_dt = created_dt
                elif updated_dt is not None:
                    best_dt = updated_dt

                if best_dt is not None:
                    return best_dt.timestamp()
                return float('-inf')
            return _updated_desc
        if key == 'updated-asc':
            def _updated_asc(x):
                # For oldest-first, use the earliest of created or updated_date
                created_dt = _dt_from_created(x.get('created'))
                updated_dt = _dt_from_updated(x.get('updated_date'))

                # Pick the earliest date between created and updated
                best_dt = None
                if created_dt is not None and updated_dt is not None:
                    best_dt = min(created_dt, updated_dt)
                elif created_dt is not None:
                    best_dt = created_dt
                elif updated_dt is not None:
                    best_dt = updated_dt

                if best_dt is not None:
                    return best_dt.timestamp()
                return float('inf')
            return _updated_asc
        return lambda x: str(x.get('name') or x.get('id') or '').lower()


class ChooseNvidiaModel(Dialog):

    metadata_warmup_finished = pyqtSignal()

    def __init__(self, model_id: str = '', parent: QWidget | None = None):
        # Initialise the model id before the base Dialog constructor runs. The
        # Dialog base class invokes ``setup_ui`` during its ``__init__``, and that
        # method accesses ``self._initial_model_id``. Setting the attribute first
        # prevents an AttributeError.
        self._initial_model_id = str(model_id or '').strip()
        self._metadata_warmup_started = False
        self._metadata_warmup_done = False
        super().__init__(title=_('NVIDIA Provider - Experimental'), name='nvidia-choose-model', parent=parent)
        self.metadata_warmup_finished.connect(self._finish_metadata_warmup)

    def sizeHint(self):
        return QSize(860, 560)

    @property
    def model_id(self) -> str:
        ci = self.models.currentIndex()
        if ci.isValid():
            m = ci.data(Qt.ItemDataRole.UserRole) or {}
            return str(m.get('id') or '').strip()
        return ''

    @property
    def model_name(self) -> str:
        idx = self.models.currentIndex()
        if idx.isValid():
            return str(idx.data(Qt.ItemDataRole.DisplayRole) or '')
        return ''

    def setup_ui(self):
        l = QVBoxLayout(self)
        self.search = f = QLineEdit(self)
        f.setPlaceholderText(_('Search for models by name'))
        f.textChanged.connect(self.update_filters)
        f.setClearButtonEnabled(True)

        h = QHBoxLayout()
        h.addWidget(f)
        l.addLayout(h)

        h = QHBoxLayout()
        la = QLabel(_('Sort by:'))
        h.addWidget(la)
        sort_key = str(gprefs.get('nvidia-model-sort') or 'updated-desc')
        self.sort_loc = SortLoc(sort_key, self)
        self.sort_loc.currentIndexChanged.connect(self.update_sorts)
        h.addWidget(self.sort_loc)
        la.setBuddy(self.sort_loc)
        h.addStretch()
        l.addLayout(h)

        self.splitter = s = QSplitter(self)
        l.addWidget(s)
        self.models = m = QListView(self)
        m.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.proxy_model = pm = ProxyModels(m)
        m.setModel(pm)
        s.addWidget(m)
        self.details = d = ModelDetails(self)
        s.addWidget(d)
        try:
            # Give the details pane a bit more room to avoid clipped specs.
            s.setStretchFactor(0, 2)
            s.setStretchFactor(1, 3)
        except Exception:
            pass
        m.selectionModel().currentChanged.connect(self.current_changed)

        b = self.bb.addButton(_('Clear choice'), QDialogButtonBox.ButtonRole.ActionRole)
        b.setIcon(QIcon.ic('trash.png'))
        b.clicked.connect(lambda: self._clear_choice())
        b.setToolTip(_('Let the model be chosen from the dropdown or manual entry'))

        b_refresh = self.bb.addButton(_('Refresh list'), QDialogButtonBox.ButtonRole.ActionRole)
        b_refresh.setIcon(QIcon.ic('view-refresh.png'))
        b_refresh.clicked.connect(lambda: self._refresh_models())
        b_refresh.setToolTip(_('Fetch fresh model list from NVIDIA API (bypasses cache)'))

        h = QHBoxLayout()
        self.status = QLabel('')
        self.status.setWordWrap(True)
        h.addWidget(self.status)
        self.spinner = Spinner(self)
        h.addWidget(self.spinner)
        self.counts = QLabel('')
        h.addWidget(self.counts), h.addStretch(), h.addWidget(self.bb)
        l.addLayout(h)

        self.update_filters()
        self.update_sorts()
        # Restore selection: explicit initial model id wins, otherwise use persisted last selection.
        initial = self._initial_model_id or str(gprefs.get('nvidia-last-model-id') or '').strip()
        if initial:
            self.models.setCurrentIndex(self.proxy_model.index_for_model_id(initial))
        if self.proxy_model.rowCount(QModelIndex()) == 0:
            self.status.setText(_('Loading models…'))
            self.spinner.start()
        QTimer.singleShot(0, self._start_metadata_warmup)

    def _clear_choice(self):
        self.models.setCurrentIndex(QModelIndex())

    def _refresh_models(self):
        """Re-fetch the model list and live metadata, off the GUI thread.

        This previously ran the fetch inline, which froze calibre for as long as
        the network work took. It now goes through the same background worker as
        the initial load.
        """
        try:
            from calibre_plugins.chapter_summarizer.nvidia_free import (
                invalidate_build_metadata_cache,
                invalidate_models_rich_cache,
            )
            invalidate_build_metadata_cache()
            invalidate_models_rich_cache()
        except Exception:
            pass
        self._metadata_warmup_started = False
        self._metadata_warmup_done = False
        debug_log('Manual refresh requested')
        self._start_metadata_warmup()

    def _start_metadata_warmup(self):
        if self._metadata_warmup_started or self._metadata_warmup_done:
            return
        self._metadata_warmup_started = True
        debug_log('Dialog warmup scheduled')
        try:
            self.status.setText(_('Loading models…'))
            self.spinner.start()
        except Exception:
            pass

        def worker():
            try:
                from calibre_plugins.chapter_summarizer.nvidia_free import warmup_build_metadata
                debug_log('Dialog warmup worker started')
                warmup_build_metadata(use_cache=False)
            finally:
                try:
                    debug_log('Dialog warmup worker emitting completion signal')
                    self.metadata_warmup_finished.emit()
                except Exception:
                    debug_log('Dialog warmup worker failed to emit completion signal')
                    pass

        Thread(target=worker, name='ChapterSummarizerNvidiaWarmup', daemon=True).start()

    def _finish_metadata_warmup(self):
        self._metadata_warmup_done = True
        debug_log('Dialog warmup completion received')
        try:
            self.spinner.stop()
        except Exception:
            pass
        try:
            from calibre_plugins.chapter_summarizer.nvidia_free import invalidate_models_rich_cache
            invalidate_models_rich_cache()
        except Exception:
            pass
        current_id = self.model_id
        self.proxy_model = ProxyModels(self.models)
        try:
            self.proxy_model.source_model.refresh(use_cache=True)
        except Exception:
            pass
        self.models.setModel(self.proxy_model)
        self.models.selectionModel().currentChanged.connect(self.current_changed)
        self.update_filters()
        self.update_sorts()
        if not current_id:
            current_id = self._initial_model_id or str(gprefs.get('nvidia-last-model-id') or '').strip()
        if current_id:
            self.models.setCurrentIndex(self.proxy_model.index_for_model_id(current_id))
        try:
            visible = self.proxy_model.rowCount(QModelIndex())
            total = self.proxy_model.sourceModel().rowCount(QModelIndex())
            if visible == 0 and total > 0:
                self.status.setText(_('No models match the current filters.'))
            else:
                self.status.setText(_('Loaded %d models.') % total)
        except Exception:
            pass
        debug_log(f'Dialog warmup applied visible={self.proxy_model.rowCount(QModelIndex())} total={self.proxy_model.sourceModel().rowCount(QModelIndex())}')

    def current_changed(self):
        idx = self.models.selectionModel().currentIndex()
        if idx.isValid():
            model = idx.data(Qt.ItemDataRole.UserRole)
            self.details.show_model_details(model)
            try:
                mid = str((model or {}).get('id') or '').strip()
                if mid:
                    gprefs.set('nvidia-last-model-id', mid)
            except Exception:
                pass
        else:
            self.details.show_help()

    def update_sorts(self):
        sort_key = self.sort_loc.sort_key
        self.proxy_model.set_sort(sort_key)
        gprefs.set('nvidia-model-sort', sort_key)

    def update_filters(self):
        filters = []
        text = self.search.text().strip()
        if text:
            search_tokens = text.lower().split()

            def model_matches(m):
                name_tokens = str(m.get('name') or m.get('id') or '').lower().split()
                for tok in search_tokens:
                    for q in name_tokens:
                        if tok in q:
                            break
                    else:
                        return False
                return True

            filters.append(model_matches)

        self.proxy_model.set_filters(*filters)
        num_showing = self.proxy_model.rowCount(QModelIndex())
        total = self.proxy_model.sourceModel().rowCount(QModelIndex())
        if num_showing == total:
            self.counts.setText(_('{} models').format(num_showing))
        else:
            self.counts.setText(_('{0} of {1} models').format(num_showing, total))
        debug_log(f'Dialog filters updated search={text!r} visible={num_showing} total={total}')
        self.current_changed()
