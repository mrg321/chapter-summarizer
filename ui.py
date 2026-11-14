#!/usr/bin/env python
# vim:fileencoding=UTF-8

__license__ = 'GPL v3'
__copyright__ = '2026, Comfy.n'
__docformat__ = 'restructuredtext en'

import os

from calibre.gui2.actions import InterfaceAction
from qt.core import (
    QApplication, QDialog, QDialogButtonBox, QIcon, QLabel,
    QPixmap, Qt, QTimer, QVBoxLayout,
)

from calibre.gui2 import open_url

load_translations()

from calibre_plugins.chapter_summarizer.main import ChapterSummarizerDialog
from calibre_plugins.chapter_summarizer.main import ChapterSummarizerConfigDialog

PLUGIN_IMPORT_NAME = 'chapter_summarizer'


def _cs_debug_enabled() -> bool:
    try:
        return bool(os.environ.get('CHAPTER_SUMMARIZER_DEBUG_ENABLED'))
    except Exception:
        return False


def _cs_debug_log(message: str) -> None:
    if not _cs_debug_enabled():
        return
    try:
        print(f'[Chapter Summarizer][Action] {message}')
    except Exception:
        pass


class ChapterSummarizerAction(InterfaceAction):
    name = 'Chapter Summarizer'
    action_spec = ('Chapter Summarizer', None, 'Summarize a selected book or external text file', None)
    action_shortcut_name = 'Chapter Summarizer'
    action_type = 'current'
    action_add_menu = True
    action_menu_clone_qaction = _('Open Chapter Summarizer')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._dialog = None
        self._palette_signal_connected = False

    def _is_dark_theme(self) -> bool:
        try:
            app = QApplication.instance()
            if not app:
                return False
            palette = app.palette()
            window_color = palette.color(palette.ColorRole.Window)
            text_color = palette.color(palette.ColorRole.WindowText)
            return text_color.lightness() > window_color.lightness()
        except Exception:
            return False

    def _get_plugin_icon(self, icon_name: str) -> QIcon | None:
        gi = globals().get('get_icons', None)
        if not callable(gi):
            try:
                from calibre.customize.zipplugin import get_icons as gi
            except Exception:
                gi = None
        if not callable(gi):
            return None
        # Normalize and prefer images/ path first to avoid zipfile KeyError
        icon_name = icon_name.replace('\\', '/')
        icon_name_backslash = icon_name.replace('/', '\\')
        icon_base = icon_name.replace('images/', '') if isinstance(icon_name, str) and icon_name.startswith('images/') else icon_name
        icon_base_backslash = icon_base.replace('/', '\\')
        plugin_name = getattr(self, 'name', None)
        plugin_path = getattr(self, 'plugin_path', None)

        images_name = icon_name if isinstance(icon_name, str) and icon_name.startswith('images/') else ('images/' + icon_name)
        images_name_backslash = images_name.replace('/', '\\')

        candidates = []
        if plugin_name:
            candidates.append((images_name, plugin_name))
            if images_name_backslash != images_name:
                candidates.append((images_name_backslash, plugin_name))
            candidates.append((icon_name, plugin_name))
            if icon_name_backslash != icon_name:
                candidates.append((icon_name_backslash, plugin_name))
            candidates.append((icon_base, plugin_name))
            if icon_base_backslash != icon_base:
                candidates.append((icon_base_backslash, plugin_name))

        candidates.append((images_name,))
        if images_name_backslash != images_name:
            candidates.append((images_name_backslash,))
        candidates.append((icon_name,))
        if icon_name_backslash != icon_name:
            candidates.append((icon_name_backslash,))
        candidates.append((icon_base,))
        if icon_base_backslash != icon_base:
            candidates.append((icon_base_backslash,))

        if plugin_path:
            candidates.append((plugin_path, images_name))
            if images_name_backslash != images_name:
                candidates.append((plugin_path, images_name_backslash))
            candidates.append((plugin_path, icon_name))
            if icon_name_backslash != icon_name:
                candidates.append((plugin_path, icon_name_backslash))
            candidates.append((plugin_path, icon_base))
            if icon_base_backslash != icon_base:
                candidates.append((plugin_path, icon_base_backslash))

        for args in candidates:
            try:
                icon = gi(*args)
                if isinstance(icon, QIcon) and not icon.isNull():
                    return icon
            except TypeError:
                continue
            except Exception:
                continue
        return None

    def rebuild_icon(self):
        icon_path = 'images/iconplugin_dark.png' if self._is_dark_theme() else 'images/iconplugin_light.png'
        icon = self._get_plugin_icon(icon_path) or self._get_plugin_icon('images/iconplugin_light.png') or self._get_plugin_icon('images/iconplugin_dark.png')
        if icon is not None:
            self.qaction.setIcon(icon)
            if self._dialog is not None:
                try:
                    self._dialog.setWindowIcon(icon)
                except Exception:
                    pass
            # Keep context-menu clone action in sync
            try:
                if hasattr(self, 'menuless_qaction') and self.menuless_qaction is not None:
                    self.menuless_qaction.setIcon(icon)
            except Exception:
                pass

    def genesis(self):
        self.rebuild_icon()
        self.qaction.triggered.connect(self.show_dialog)
        self.qaction.setVisible(True)
        try:
            self.gui.keyboard.register_shortcut(
                'plugin:chapter_summarizer:open',
                _('Chapter Summarizer: open'),
                default_keys=(),
                action=self.qaction,
                description=_('Open the Chapter Summarizer window.'),
                group=_('Chapter Summarizer'),
                persist_shortcut=True,
            )
        except Exception:
            pass

        # Set the theme-responsive icon on the context menu clone action
        try:
            if hasattr(self, 'menuless_qaction') and self.menuless_qaction is not None:
                self.menuless_qaction.setIcon(self.qaction.icon())
        except Exception:
            pass

        # Pre-cache QR code data from zip for About dialog
        try:
            from calibre.customize.zipplugin import get_resources
            self._cached_qr_data = get_resources(self.plugin_path, 'images/qrcode.png')
        except Exception:
            self._cached_qr_data = None

        app = QApplication.instance()
        if app is not None and hasattr(app, 'palette_changed') and not self._palette_signal_connected:
            try:
                app.palette_changed.connect(self.rebuild_icon)
                self._palette_signal_connected = True
            except Exception:
                pass

        menu = self.qaction.menu()
        if menu is not None:
            menu.addSeparator()
            self.create_menu_action(
                menu,
                'configure-chapter-summarizer',
                _('Configure Chapter Summarizer'),
                icon=QIcon.ic('config.png'),
                shortcut=False,
                triggered=self.show_configuration,
            )
            self.create_menu_action(
                menu,
                'about-chapter-summarizer',
                _('About'),
                icon=QIcon.ic('help.png'),
                shortcut=False,
                triggered=self.show_about,
            )

        self.qaction.setToolTip(_('Open the Chapter Summarizer window'))

    def show_configuration(self):
        parent = self._dialog if self._dialog is not None else self.gui
        d = ChapterSummarizerConfigDialog(parent, icon=self.qaction.icon())
        d.exec()
        # Return focus to the CS window, not calibre main UI
        if self._dialog is not None:
            try:
                self._dialog.show()
                self._dialog.raise_()
                self._dialog.activateWindow()
            except Exception:
                pass

    def show_about(self):
        if not hasattr(self, '_about_dialog') or self._about_dialog is None:
            self._prebuild_about_dialog()
        try:
            self._about_dialog.show()
            self._about_dialog.raise_()
            self._about_dialog.activateWindow()
        except Exception:
            pass

    def _prebuild_about_dialog(self):
        if hasattr(self, '_about_dialog') and self._about_dialog is not None:
            return
        dialog = QDialog(self.gui)
        dialog.setWindowTitle(_('About Chapter Summarizer'))
        try:
            dialog.setWindowIcon(self.qaction.icon())
        except Exception:
            pass
        layout = QVBoxLayout(dialog)

        try:
            from calibre_plugins.chapter_summarizer import ChapterSummarizerPlugin
            ver = '.'.join(str(x) for x in ChapterSummarizerPlugin.version)
        except Exception:
            ver = '0.0.0'

        PLUGIN_NAME = 'Chapter Summarizer'
        tip_text = _('Show appreciation')

        html = (
            '<style>a {{ text-decoration: none; }}</style>'
            '<div align="center">'
            '<h2>{0}</h2>'
            '<p>' + _('Summarize books, articles, and news by chapter or section using calibre AI providers.') + '</p>'
            '<p style="font-size:9pt; color:gray;">' + _('Version: {1}') + '</p>'
            '<p style="font-size:11pt;"><a href="https://www.mobileread.com/forums/showthread.php?t=374061">' + _('MR help thread') + '</a></p>'
            '<p style="font-size:12pt;">' + '<a href="https://ko-fi.com/comfy_n">' + tip_text + '</a></p>'
            '</div>'
        ).format(PLUGIN_NAME, ver)
        text_label = QLabel(html, dialog)
        text_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        text_label.setWordWrap(True)
        text_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        text_label.setOpenExternalLinks(False)
        try:
            text_label.linkActivated.connect(open_url)
        except Exception:
            pass
        layout.addWidget(text_label)

        qr_label = QLabel(dialog)
        qr_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._qr_label = qr_label
        layout.addWidget(self._qr_label)

        qr_data = getattr(self, '_cached_qr_data', None)
        if qr_data:
            qp = QPixmap()
            if qp.loadFromData(qr_data):
                self._qr_label.setPixmap(qp.scaledToWidth(150, Qt.TransformationMode.SmoothTransformation))
                self._qr_label.setCursor(Qt.CursorShape.PointingHandCursor)
                self._qr_label.mousePressEvent = self._flip_qr_image
        else:
            self._qr_label.setVisible(False)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, dialog)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)

        self._about_dialog = dialog

    def _flip_qr_image(self, event):
        try:
            QTimer.singleShot(0, lambda: open_url('https://ko-fi.com/comfy_n'))
        except Exception:
            pass

    def show_dialog(self):
        self.rebuild_icon()
        if self._dialog is None:
            self._dialog = ChapterSummarizerDialog(self.gui, self.qaction.icon())
            _cs_debug_log('show_dialog: created ChapterSummarizerDialog instance')
            # Re-sync runtime prefs into the freshly built widgets. The dialog
            # is cached for the session, so without this a pref saved by a prior
            # run (e.g. base_paragraphs / include_highlight) would never reach
            # the widgets — they'd keep the value from construction time.
            try:
                self._dialog.reapply_runtime_prefs()
            except Exception as e:
                _cs_debug_log('show_dialog: reapply_runtime_prefs failed: %r' % (e,))
        self._dialog.refresh_selected_book()
        self._dialog.show()
        self._dialog.raise_()
        self._dialog.activateWindow()
        _cs_debug_log('show_dialog: dialog shown and activated')

    def library_changed(self, db):
        """Called by calibre when the library changes."""
        try:
            current_lib = str(getattr(db, 'library_path', '') or '')
        except Exception:
            current_lib = ''
        _cs_debug_log(f'library_changed: forwarding to dialog present={self._dialog is not None!r} library={current_lib!r}')
        if self._dialog is not None:
            try:
                self._dialog.library_changed()
            except Exception:
                _cs_debug_log('library_changed: dialog callback raised')
                pass
