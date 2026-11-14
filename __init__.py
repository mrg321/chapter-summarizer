# vim:fileencoding=UTF-8

__license__ = 'GPL v3'
__copyright__ = '2026, Comfy.n'
__docformat__ = 'restructuredtext en'

from calibre.customize import InterfaceActionBase

class ChapterSummarizerPlugin(InterfaceActionBase):
    name = 'Chapter Summarizer'
    description = _('Summarize books, articles, and news by chapter or section using calibre AI providers')
    supported_platforms = ['windows', 'osx', 'linux']
    author = 'Comfy.n'
    version = (1, 5, 0)
    minimum_calibre_version = (8, 16, 2)
    actual_plugin = 'calibre_plugins.chapter_summarizer.ui:ChapterSummarizerAction'

    def is_customizable(self):
        return True

    def config_widget(self):
        # Import lazily to avoid circular imports during plugin initialization.
        from calibre_plugins.chapter_summarizer.main import ChapterSummarizerConfigDialog
        # self.gui is only on the InterfaceAction, not on this wrapper.
        gui = getattr(self, 'gui', None) or getattr(self.actual_plugin_, 'gui', None)
        # embed=True tells the dialog to skip its own OK/Cancel buttons,
        # since calibre's preferences dialog provides its own.
        return ChapterSummarizerConfigDialog(gui, icon=getattr(self, 'icon', None), embed=True)

    def save_settings(self, config_widget):
        # Dialog.accept() persists prefs.
        try:
            config_widget.accept()
        except Exception:
            pass
