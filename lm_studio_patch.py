"""
LM Studio Config Widget Patch

This module monkey-patches calibre's LM Studio ConfigWidget to fix the model
status text clipping issue where "Found x models. e.g.: ..." text gets cut off
regardless of dialog width, and the dialog height also needs to be patched.

The fix patches __init__, refresh_models, and adds a showEvent handler to
ensure the dialog gets proper size constraints.
"""

from qt.core import QSizePolicy


def patch_lm_studio_config_widget():
    """
    Monkey-patch calibre's LM Studio ConfigWidget to fix the model status
    text clipping issue and dialog height.
    """
    try:
        from calibre.ai.lm_studio.config import ConfigWidget

        # Store the original methods
        original_init = ConfigWidget.__init__
        original_refresh_models = ConfigWidget.refresh_models

        def patched_init(self, parent=None):
            """Patched __init__ that fixes the model_status label sizing."""
            # Call original init
            original_init(self, parent)

            # FIX 1: Set explicit minimum width on the model status label
            if hasattr(self, 'model_status') and self.model_status:
                self.model_status.setMinimumWidth(550)
                self.model_status.setWordWrap(True)  # Allow text to wrap
                self.model_status.setSizePolicy(
                    QSizePolicy.Policy.Expanding,
                    QSizePolicy.Policy.MinimumExpanding
                )

            # FIX 2: Set minimum size on the widget itself
            self.setMinimumWidth(650)
            self.setMinimumHeight(280)

        def patched_refresh_models(self):
            """Refresh models and fix the status label width."""
            from calibre.ai.lm_studio import LMStudioAI
            from calibre.ai.utils import plugin_for_name
            from qt.core import Qt

            # Set busy cursor
            from qt.core import QApplication
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            try:
                plugin = plugin_for_name(LMStudioAI.name)
                backend = plugin.builtin_live_module

                # Clear the LRU cache to fetch fresh models from LM Studio
                if hasattr(backend, 'get_available_models'):
                    backend.get_available_models.cache_clear()

                models_dict = backend.get_available_models(self.api_url)
                keys = list(models_dict.keys()) if models_dict else []
                self._last_models = keys

                self.model_combo.blockSignals(True)
                self.model_combo.clear()
                for k in keys:
                    self.model_combo.addItem(k)

                # FIX: Ensure the label has proper sizing
                if hasattr(self, 'model_status') and self.model_status:
                    self.model_status.setMinimumWidth(550)
                    self.model_status.setWordWrap(True)
                    self.model_status.setSizePolicy(
                        QSizePolicy.Policy.Expanding,
                        QSizePolicy.Policy.MinimumExpanding
                    )

                # Only update status if we have models
                if keys:
                    # Show all models since we have plenty of space
                    example_text = ', '.join(keys)
                    self.model_status.setText(
                        f'Found {len(keys)} model(s). e.g.: {example_text}'
                    )
                else:
                    self.model_status.setText('No models found')

                self.model_combo.blockSignals(False)

            except Exception as e:
                self.model_status.setText('Error loading models')
            finally:
                QApplication.restoreOverrideCursor()

        def showEvent(self, event):
            """Override showEvent to ensure dialog has proper size and refresh models."""
            from qt.core import QSize
            # Call parent's showEvent if it exists
            super(ConfigWidget, self).showEvent(event)
            # Ensure the widget and its parent dialog are sized properly
            if self.parent() and hasattr(self.parent(), 'resize'):
                # Resize parent dialog if needed
                current_size = self.parent().size()
                new_width = max(current_size.width(), 700)
                new_height = max(current_size.height(), 350)
                self.parent().resize(new_width, new_height)
            # Refresh models when the dialog is shown to pick up newly downloaded models
            if hasattr(self, 'refresh_models'):
                self.refresh_models()

        # Apply the patches
        ConfigWidget.__init__ = patched_init
        ConfigWidget.refresh_models = patched_refresh_models
        ConfigWidget.showEvent = showEvent

        return True

    except Exception as e:
        print(f"Failed to patch LM Studio config widget: {e}")
        import traceback
        traceback.print_exc()
        return False
