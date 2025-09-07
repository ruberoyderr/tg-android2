# mobile_ui.py — Android-friendly theming & UX helpers for PySide6 (Qt Widgets)
from PySide6 import QtCore, QtGui, QtWidgets
import os, sys
def _is_android():
    return sys.platform.startswith('android') or ('ANDROID_ROOT' in os.environ)
def _dp(dp, screen): 
    dpi = screen.logicalDotsPerInch() or 160.0
    return int(dp * (dpi/160.0) + 0.5)
def apply_android_theme(app, root=None):
    QtWidgets.QApplication.setAttribute(QtCore.Qt.AA_UseHighDpiPixmaps, True)
    QtWidgets.QApplication.setAttribute(QtCore.Qt.AA_SynthesizeMouseForUnhandledTouchEvents, True)
    QtWidgets.QApplication.setAttribute(QtCore.Qt.AA_CompressHighFrequencyEvents, True)
    app.setStyle('Fusion')
    s = app.primaryScreen()
    btn_h = _dp(48, s); field_h=_dp(44, s); r=_dp(12, s); pad_h=_dp(16, s); pad_v=_dp(10, s); font_px=_dp(15, s); sb=_dp(10, s)
    pal = QtGui.QPalette()
    bg=QtGui.QColor('#151619'); panel=QtGui.QColor('#1E1F22'); text=QtGui.QColor('#EAECEE'); muted=QtGui.QColor('#A0A6AC')
    acc=QtGui.QColor('#2E7D32'); out=QtGui.QColor('#3A3F44')
    pal.setColor(QtGui.QPalette.Window, panel); pal.setColor(QtGui.QPalette.Base,bg); pal.setColor(QtGui.QPalette.Button,panel)
    pal.setColor(QtGui.QPalette.ButtonText,text); pal.setColor(QtGui.QPalette.Text,text); pal.setColor(QtGui.QPalette.Highlight,acc.darker(90))
    pal.setColor(QtGui.QPalette.HighlightedText, QtGui.QColor('white')); pal.setColor(QtGui.QPalette.Disabled, QtGui.QPalette.Text, muted)
    pal.setColor(QtGui.QPalette.Disabled, QtGui.QPalette.ButtonText, muted); app.setPalette(pal)
    f=app.font(); f.setPointSizeF(max(10.0, font_px*0.75)); app.setFont(f)
    style=f"""*{{font-size:{font_px}px;}} QPushButton{{min-height:{btn_h}px;padding:{pad_v}px {pad_h}px;border-radius:{r}px;background:{acc.name()};color:white;border:none;}}
    QPushButton:disabled{{background:#444950;color:{muted.name()};}} QLineEdit,QComboBox,QSpinBox,QDoubleSpinBox,QTextEdit,QPlainTextEdit{{min-height:{field_h}px;
    padding:{pad_v}px {pad_h}px;border-radius:{r}px;border:1px solid {out.name()};background:{bg.name()};color:{text.name()};selection-background-color:{acc.name()};}}
    QToolTip{{background:{panel.name()};color:{text.name()};border:1px solid {out.name()};padding:{pad_v//2}px {pad_h//2}px;border-radius:{r//2}px;}}
    QScrollBar:vertical{{width:{sb}px;background:transparent;margin:0px;}} QScrollBar::handle:vertical{{min-height:{sb*2}px;border-radius:{sb//2}px;background:#6A6E75;}}
    QScrollBar:horizontal{{height:{sb}px;background:transparent;margin:0px;}} QScrollBar::handle:horizontal{{min-width:{sb*2}px;border-radius:{sb//2}px;background:#6A6E75;}}"""
    app.setStyleSheet(style)
    if root is not None and _is_android():
        root.setAttribute(QtCore.Qt.WA_InputMethodEnabled, True)
def enable_kinetic_scrolling(root):
    for w in root.findChildren(QtWidgets.QWidget):
        v = w.viewport() if hasattr(w,'viewport') else w
        try: QtWidgets.QScroller.grabGesture(v, QtWidgets.QScroller.LeftMouseButtonGesture)
        except Exception: pass
def install_back_button_handler(window, on_back=None):
    class _F(QtCore.QObject):
        def eventFilter(self, obj, ev):
            if ev.type()==QtCore.QEvent.KeyPress and ev.key()==QtCore.Qt.Key_Back:
                (on_back or window.close)(); return True
            return super().eventFilter(obj, ev)
    f=_F(window); window.installEventFilter(f); window._android_back_filter=f
