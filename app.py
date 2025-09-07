# -*- coding: utf-8 -*-
import os
import sys
import json
import asyncio
import uuid
import traceback
import random
import tempfile
import re
from sticker_picker import install_sticker_plugin
from sticker_picker import pick_sticker_dialog
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from PySide6.QtCore import Qt, Signal, QTimer, QEvent, QUrl
from PySide6.QtGui import (QPixmap, QAction, QPalette, QColor, QGuiApplication, QImage, QKeySequence, QDesktopServices)
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QListWidget, QListWidgetItem, QPushButton, QLineEdit, QFileDialog,
    QMessageBox, QSplitter, QScrollArea, QFrame, QComboBox, QCheckBox,
    QToolButton, QDialog, QFormLayout, QSizePolicy, QInputDialog, QMenu
)

from qasync import QEventLoop
import qrcode
import requests  # загрузка картинок по URL

# -----------------------------
# Конфигурация/пути
# -----------------------------
API_ID = 24664116
API_HASH = "25899cc6b4e1afff1a7e726256b1651c"

ROOT = Path(__file__).parent.resolve()
SESS_DIR = ROOT / "sessions"
SESS_DIR.mkdir(parents=True, exist_ok=True)
PINS_FILE = ROOT / "pins.json"                    # ГЛОБАЛЬНЫЕ закрепы (список)
PROXIES_FILE = ROOT / "proxies.json"
ACCOUNTS_CACHE_FILE = ROOT / "accounts_cache.json"
REACTIONS_CACHE_FILE = ROOT / "reactions_cache.json"   # ref|msg|uid -> emoji

# -----------------------------
# Telethon
# -----------------------------
from telethon import TelegramClient, utils
from telethon.errors import (
    SessionPasswordNeededError, PhoneCodeInvalidError, PhoneNumberBannedError,
    UserDeactivatedBanError, UserDeactivatedError, SessionRevokedError,
    AuthKeyUnregisteredError, PeerFloodError, FloodWaitError, RPCError
)
from telethon.tl import types, functions
from telethon.errors import PasswordHashInvalidError, EmailUnconfirmedError
from telethon.tl.functions.channels import GetFullChannelRequest, JoinChannelRequest
from telethon.tl.functions.messages import (SendReactionRequest, GetDiscussionMessageRequest, GetRepliesRequest, GetBotCallbackAnswerRequest)

# ---- proxy (PySocks) ----
try:
    import socks  # PySocks
except Exception:
    socks = None

# ================== helpers ==================

def _normalize_phone(raw: str) -> str:
    """
    Нормализуем телефон в формат +<код><номер>.
    РФ кейс: 8XXXXXXXXXX -> +7XXXXXXXXXX
    """
    s = re.sub(r"\D+", "", raw or "")
    if len(s) == 11 and s.startswith("8"):
        s = "7" + s[1:]
    if not s.startswith("+"):
        s = "+" + s
    return s

def friendly_display(user_or_entity) -> str:
    if isinstance(user_or_entity, types.User):
        name = " ".join(filter(None, [user_or_entity.first_name, user_or_entity.last_name])).strip()
        if name:
            return name
        if user_or_entity.username:
            return f"@{user_or_entity.username}"
        return f"id{user_or_entity.id}"
    try:
        return utils.get_display_name(user_or_entity) or f"id{getattr(user_or_entity,'id','')}"
    except Exception:
        return str(getattr(user_or_entity, "id", "—"))

def human_dialog_title(dlg):
    try:
        title = dlg.name or dlg.title
    except Exception:
        title = None
    if not title:
        entity = dlg.entity
        if isinstance(entity, (types.User, types.UserEmpty)):
            title = utils.get_display_name(entity) or f"User {entity.id}"
        else:
            title = f"Chat {entity.id}"
    return title

def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, type(default)) else default
    except Exception:
        return default

def save_json(path: Path, data):
    tmp = path.with_suffix(".tmp.json")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(path)

# ---- ГЛОБАЛЬНЫЕ закрепы ----
def load_pins() -> List[str]:
    """Миграция: если старый формат был словарь {uid:[...]}, превращаем в единый список уникальных ссылок."""
    data = load_json(PINS_FILE, [])
    if isinstance(data, dict):
        out, seen = [], set()
        for arr in data.values():
            if isinstance(arr, list):
                for r in arr:
                    if r not in seen:
                        seen.add(r); out.append(r)
        return out
    return data

def save_pins(pins: List[str]):
    save_json(PINS_FILE, pins)

def entity_ref(entity) -> str:
    uname = getattr(entity, "username", None) or None
    if uname: return f"username:{uname}"
    if isinstance(entity, types.User): return f"user:{entity.id}"
    if isinstance(entity, (types.Chat, types.ChatForbidden)): return f"chat:{entity.id}"
    if isinstance(entity, (types.Channel, types.ChannelForbidden)): return f"channel:{entity.id}"
    return f"peer:{utils.get_peer_id(entity)}"

async def resolve_ref(client: TelegramClient, ref: str):
    try:
        kind, value = ref.split(":", 1)
    except ValueError:
        return await client.get_entity(ref)
    try:
        if kind == "username": return await client.get_entity(value)
        if kind == "user":    return await client.get_entity(types.PeerUser(int(value)))
        if kind == "chat":    return await client.get_entity(types.PeerChat(int(value)))
        if kind == "channel": return await client.get_entity(types.PeerChannel(int(value)))
        if kind == "peer":
            pid = int(value)
            for t in (types.PeerChannel, types.PeerUser, types.PeerChat):
                try:
                    return await client.get_entity(t(pid))
                except Exception:
                    continue
    except Exception:
        pass
    return None

async def ensure_join(client: TelegramClient, entity):
    try:
        if isinstance(entity, types.Channel):
            await client(JoinChannelRequest(entity))
    except Exception:
        pass

async def get_allowed_reaction_emojis(client: TelegramClient, entity) -> List[str]:
    default = ["❤️", "👍", "😂", "🔥", "👏", "😮", "😢", "👎"]
    try:
        if isinstance(entity, types.Channel):
            full = await client(GetFullChannelRequest(entity))
            ar = getattr(full.full_chat, "available_reactions", None)
            if isinstance(ar, types.ChatReactionsSome):
                out = [r.emoticon for r in ar.reactions if isinstance(r, types.ReactionEmoji)]
                return out or default
        return default
    except Exception:
        return default

def force_dark_palette(app: QApplication):
    p = QPalette()
    bg = QColor(11,18,32)
    base = QColor(18,26,43)
    text = QColor(234,242,255)
    hl   = QColor(60,124,240)
    p.setColor(QPalette.Window, bg)
    p.setColor(QPalette.WindowText, text)
    p.setColor(QPalette.Base, base)
    p.setColor(QPalette.AlternateBase, bg.darker(110))
    p.setColor(QPalette.Text, text)
    p.setColor(QPalette.Button, base)
    p.setColor(QPalette.ButtonText, text)
    p.setColor(QPalette.Highlight, hl)
    p.setColor(QPalette.HighlightedText, QColor(255,255,255))
    app.setPalette(p)

# ------------- proxies helpers -------------
def _default_proxies_config():
    return {"pool": [], "assignments_by_session": {}, "assignments_by_user": {}}

def _load_proxies_config() -> dict:
    return load_json(PROXIES_FILE, _default_proxies_config())

def _save_proxies_config(cfg: dict):
    save_json(PROXIES_FILE, cfg)

def _parse_proxy_line(line: str) -> Optional[dict]:
    line = (line or "").strip()
    if not line or line.startswith("#"):
        return None
    if "://" in line:
        try:
            scheme, rest = line.split("://", 1)
            scheme = (scheme or "").lower().strip()
            if "@" in rest:
                cred, addr = rest.split("@", 1)
                if ":" in cred:
                    user, pwd = cred.split(":", 1)
                else:
                    user, pwd = cred, ""
            else:
                user = pwd = ""
                addr = rest
            if ":" not in addr:
                return None
            host, port = addr.rsplit(":", 1)
            return {"scheme": scheme or "http", "host": host.strip(), "port": int(port),
                    "username": (user or "").strip(), "password": (pwd or "").strip()}
        except Exception:
            return None
    parts = line.split(":")
    if len(parts) == 4:
        host, port, user, pwd = parts
        try:
            return {"scheme": "http", "host": host.strip(), "port": int(port),
                    "username": (user or "").strip(), "password": (pwd or "").strip()}
        except Exception:
            return None
    if len(parts) == 2:
        host, port = parts
        try:
            return {"scheme": "http", "host": host.strip(), "port": int(port),
                    "username": "", "password": ""}
        except Exception:
            return None
    return None

def _telethon_proxy_tuple_from_cfg(p: dict):
    if socks is None:
        return None
    scheme = (p.get("scheme") or "http").lower().strip()
    host = (p.get("host") or "").strip()
    port = int(p.get("port") or 0)
    user = (p.get("username") or None) or None
    pwd  = (p.get("password") or None) or None
    rdns = True
    if scheme in ("socks5", "socks5h"):
        return (socks.SOCKS5, host, port, rdns, user, pwd)
    elif scheme in ("http", "https"):
        return (socks.HTTP, host, port, rdns, user, pwd)
    else:
        return (socks.HTTP, host, port, rdns, user, pwd)

# =============== Диалоги авторизации ===============
class PhoneLoginDialog(QDialog):
    requestCode = Signal()
    submitLogin = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Добавить аккаунт — телефон")
        self.setModal(True)
        self.resize(480, 260)
        layout = QFormLayout(self)

        self.phone = QLineEdit(self); self.phone.setPlaceholderText("+79991234567")
        self.code  = QLineEdit(self); self.code.setPlaceholderText("Код из SMS/Telegram")
        self.pwd   = QLineEdit(self); self.pwd.setPlaceholderText("Пароль 2FA (если включён)")
        self.pwd.setEchoMode(QLineEdit.Password)

        layout.addRow("Телефон:", self.phone)
        layout.addRow("Код:", self.code)
        layout.addRow("Пароль 2FA:", self.pwd)

        row = QHBoxLayout()
        self.btn_request = QPushButton("Запросить код", self)
        self.btn_ok = QPushButton("Войти", self)
        self.btn_cancel = QPushButton("Отмена", self)
        row.addWidget(self.btn_request); row.addStretch(1); row.addWidget(self.btn_ok); row.addWidget(self.btn_cancel)
        layout.addRow(row)

        self.btn_cancel.clicked.connect(self.reject)
        self.btn_request.clicked.connect(lambda: self.requestCode.emit())
        self.btn_ok.clicked.connect(lambda: self.submitLogin.emit())

class QRLoginDialog(QDialog):
    cancelled = Signal()
    passwordEntered = Signal(str)  # <-- новый сигнал

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Добавить аккаунт — QR")
        self.setModal(True)
        self.resize(420, 560)
        v = QVBoxLayout(self)
        self.info = QLabel(
            "Откройте Telegram → Устройства → Подключить устройство и сканируйте QR.\n"
            "Код обновляется автоматически.\nЕсли включена 2FA — введите пароль ниже.",
            self
        )
        self.info.setWordWrap(True); self.info.setAlignment(Qt.AlignCenter)
        self.qr_label = QLabel(self); self.qr_label.setAlignment(Qt.AlignCenter)

        # Поле для 2FA внутри того же окна (не блокирует asyncio)
        self.pwd_edit = QLineEdit(self)
        self.pwd_edit.setPlaceholderText("Пароль 2FA")
        self.pwd_edit.setEchoMode(QLineEdit.Password)
        self.pwd_edit.hide()

        self.btn_pwd = QPushButton("Войти с 2FA", self)
        self.btn_pwd.hide()
        self.btn_pwd.clicked.connect(lambda: self.passwordEntered.emit(self.pwd_edit.text()))

        self.btn_cancel = QPushButton("Отмена", self)
        self.btn_cancel.clicked.connect(lambda: self.cancelled.emit())

        v.addWidget(self.info); v.addWidget(self.qr_label, 1)
        v.addWidget(self.pwd_edit); v.addWidget(self.btn_pwd)
        v.addWidget(self.btn_cancel)

    def set_qr(self, url: str):
        from io import BytesIO
        img = qrcode.make(url)
        buf = BytesIO(); img.save(buf, format="PNG")
        pm = QPixmap(); pm.loadFromData(buf.getvalue(), "PNG")
        self.qr_label.setPixmap(pm.scaled(320, 320, Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def show_password_prompt(self):
        self.pwd_edit.show()
        self.btn_pwd.show()
        self.pwd_edit.setFocus()

# =============== Модель ===============
@dataclass
class Account:
    session_path: Path
    client: TelegramClient
    user: types.User
    user_id: int
    display: str
    api_lock: asyncio.Lock
    last_used_ts: float = 0.0

# =============== Баблы/Комментарии ===============
class MessageBubble(QFrame):
    reactClicked = Signal(object, str)
    commentsClicked = Signal(object)
    replyClicked = Signal(object)
    inlineButtonClicked = Signal(object, object)

    def __init__(self, msg: types.Message, outgoing: bool, can_react: bool,
                 show_reply_btn: bool, emojis: List[str], parent=None):
        super().__init__(parent)
        self.msg = msg
        self._allowed = emojis or []
        self.setObjectName("bubbleOut" if outgoing else "bubbleIn")
        self.setFrameShape(QFrame.NoFrame)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        v = QVBoxLayout(self); v.setContentsMargins(12, 10, 12, 10); v.setSpacing(8)

        # header
        header = QHBoxLayout()
        who_text = "Сообщение"
        try:
            if msg.sender:
                who_text = utils.get_display_name(msg.sender) or who_text
            elif outgoing:
                who_text = "Вы"
        except Exception:
            pass
        self.lbl_author = QLabel(who_text, self); self.lbl_author.setObjectName("bubbleAuthor")
        ts = msg.date.strftime("%d.%m %H:%M") if msg.date else ""
        lbl_time = QLabel(ts, self); lbl_time.setObjectName("bubbleTime")
        header.addWidget(self.lbl_author); header.addStretch(1); header.addWidget(lbl_time)
        v.addLayout(header)

        # text
        text = msg.message or (msg.media and getattr(msg.media, 'caption', None)) or ""
        lbl = QLabel(text if text else "(медиа)", self)
        lbl.setWordWrap(True); lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        v.addWidget(lbl)

        # media preview button
        self.media_btn = None
        if msg.media:
            self.media_btn = QPushButton("Показать медиа", self)
            self.media_btn.setCursor(Qt.PointingHandCursor)
            v.addWidget(self.media_btn)

        # inline buttons (ReplyInlineMarkup)
        try:
            rm = getattr(msg, "reply_markup", None)
            if isinstance(rm, types.ReplyInlineMarkup) and getattr(rm, "rows", None):
                for row in rm.rows:
                    row_layout = QHBoxLayout()
                    for b in getattr(row, "buttons", []) or []:
                        text = getattr(b, "text", "") or "…"
                        btn = QPushButton(text, self)
                        btn.setCursor(Qt.PointingHandCursor)
                        btn.setStyleSheet("QPushButton { background:#24325a; border:1px solid #2b3a66; border-radius:10px; padding:6px 10px; }")

                        if isinstance(b, types.KeyboardButtonUrl):
                            url = getattr(b, "url", "")
                            btn.clicked.connect(lambda _, u=url: self.inlineButtonClicked.emit(self.msg, {"kind":"url","url":u}))
                        elif isinstance(b, types.KeyboardButtonCallback):
                            data = getattr(b, "data", None)
                            btn.clicked.connect(lambda _, d=data: self.inlineButtonClicked.emit(self.msg, {"kind":"callback","data":d}))
                        elif isinstance(b, types.KeyboardButtonSwitchInline):
                            q = getattr(b, "query", "") or ""
                            same = bool(getattr(b, "same_peer", False))
                            btn.clicked.connect(lambda _, q=q, s=same: self.inlineButtonClicked.emit(self.msg, {"kind":"switch_inline","query":q,"same_peer":s}))
                        else:
                            btn.clicked.connect(lambda: self.inlineButtonClicked.emit(self.msg, {"kind":"unknown"}))

                        row_layout.addWidget(btn)
                    row_layout.addStretch(1)
                    v.addLayout(row_layout)
        except Exception:
            pass

        # reactions strip
        self._rx_row = QHBoxLayout()
        self._rx_pills: Dict[str, Tuple[QToolButton, int]] = {}
        self._rx_add_btn: Optional[QToolButton] = None

        rx = getattr(msg, "reactions", None)
        if can_react:
            if rx and getattr(rx, "results", None):
                for rc in rx.results:
                    emo = None
                    try:
                        if isinstance(rc.reaction, types.ReactionEmoji):
                            emo = rc.reaction.emoticon
                    except Exception:
                        pass
                    if not emo:
                        continue
                    btn = QToolButton(self)
                    btn.setText(f"{emo} {rc.count}")
                    btn.setCursor(Qt.PointingHandCursor)
                    btn.setStyleSheet("QToolButton { background:#1a2440; border:1px solid #2b3a66; border-radius:12px; padding:2px 8px; }")
                    btn.clicked.connect(lambda _, e=emo: self.reactClicked.emit(self.msg, e))
                    self._rx_row.addWidget(btn)
                    self._rx_pills[emo] = (btn, int(rc.count))
            else:
                addb = QToolButton(self); addb.setText("Добавить реакцию  ⊕")
                addb.setCursor(Qt.PointingHandCursor)
                addb.setStyleSheet("QToolButton { background:#1a2440; border:1px solid #2b3a66; border-radius:12px; padding:2px 8px; }")
                addb.clicked.connect(self._open_add_menu)
                self._rx_row.addWidget(addb)
                self._rx_add_btn = addb
        else:
            note = QLabel("реакции отключены", self)
            note.setStyleSheet("QLabel { color:#9fb3d9; }")
            self._rx_row.addWidget(note)

        self._rx_row.addStretch(1)
        v.addLayout(self._rx_row)

        # bottom actions
        actions = QHBoxLayout()
        if msg.replies or isinstance(msg.peer_id, types.PeerChannel):
            btn_comments = QPushButton("Комментарии", self)
            n = 0
            try: n = getattr(msg.replies, "replies", 0) or 0
            except Exception: pass
            if n: btn_comments.setText(f"Комментарии ({n})")
            btn_comments.setCursor(Qt.PointingHandCursor)
            btn_comments.clicked.connect(lambda: self.commentsClicked.emit(self.msg))
            actions.addWidget(btn_comments)

        if show_reply_btn:
            btn_reply = QPushButton("Ответить", self)
            btn_reply.setCursor(Qt.PointingHandCursor)
            btn_reply.clicked.connect(lambda: self.replyClicked.emit(self.msg))
            actions.addWidget(btn_reply)

        actions.addStretch(1)
        v.addLayout(actions)

    def _open_add_menu(self):
        if not self._allowed:
            return
        menu = QMenu(self)
        for emo in self._allowed:
            act = menu.addAction(emo)
            act.triggered.connect(lambda _, e=emo: self.reactClicked.emit(self.msg, e))
        menu.exec(self.mapToGlobal(self.rect().bottomLeft()))

    def apply_reaction(self, emoji: str, delta: int = 1):
        if self._rx_add_btn:
            try:
                self._rx_row.removeWidget(self._rx_add_btn)
                self._rx_add_btn.deleteLater()
            except Exception:
                pass
            self._rx_add_btn = None

        if emoji in self._rx_pills:
            btn, cnt = self._rx_pills[emoji]
            cnt = max(0, cnt + delta)
            btn.setText(f"{emoji} {cnt}")
            self._rx_pills[emoji] = (btn, cnt)
        else:
            if delta <= 0:
                return
            btn = QToolButton(self)
            btn.setText(f"{emoji} {delta}")
            btn.setCursor(Qt.PointingHandCursor)
            btn.setStyleSheet("QToolButton { background:#1a2440; border:1px solid #2b3a66; border-radius:12px; padding:4px 6px; }")
            btn.clicked.connect(lambda _, e=emoji: self.reactClicked.emit(self.msg, e))
            self._rx_row.insertWidget(self._rx_row.count()-1, btn)
            self._rx_pills[emoji] = (btn, delta)

    def set_author(self, text: str):
        try:
            self.lbl_author.setText(text)
        except Exception:
            pass
class CommentsPanel(QWidget):
    sendComment = Signal(str)
    reactInComment = Signal(object, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        v = QVBoxLayout(self); v.setContentsMargins(8,8,8,8); v.setSpacing(8)
        self.title = QLabel("Комментарии", self); self.title.setObjectName("commentsTitle")
        v.addWidget(self.title)

        self.area = QScrollArea(self); self.area.setWidgetResizable(True)
        self.inner = QWidget(self.area)
        the_v = QVBoxLayout(self.inner); the_v.setContentsMargins(0,0,0,0); the_v.setSpacing(8)
        self.inner_v = the_v
        self.inner_v.addStretch(1)
        self.area.setWidget(self.inner)
        v.addWidget(self.area, 1)

        reply_bar = QHBoxLayout()
        self.reply_info = QLabel("", self); self.reply_info.setObjectName("replyInfo")
        self.reply_cancel = QPushButton("Отменить ответ", self); self.reply_cancel.setVisible(False)
        self.reply_cancel.clicked.connect(self._clear_reply_target)
        reply_bar.addWidget(self.reply_info, 1); reply_bar.addWidget(self.reply_cancel)
        v.addLayout(reply_bar)

        send_row = QHBoxLayout()
        self.input = QLineEdit(self); self.input.setPlaceholderText("Напишите комментарий… (Enter — отправить)")
        self.btn_attach = QPushButton("📎", self)
        the_send = QPushButton("Отправить", self)
        the_send.clicked.connect(self._emit_send)
        self.input.returnPressed.connect(self._emit_send)
        send_row.addWidget(self.input, 1); send_row.addWidget(self.btn_attach); send_row.addWidget(the_send)
        v.addLayout(send_row)

        self._msg_map: List[types.Message] = []
        self._reply_target: Optional[types.Message] = None
        self._id2bubble: Dict[int, MessageBubble] = {}

    def clear_comments(self, title: str = "Комментарии"):
        self.title.setText(title)
        for i in reversed(range(self.inner_v.count() - 1)):
            w = self.inner_v.itemAt(i).widget()
            if w: w.setParent(None)
        self._msg_map.clear()
        self._id2bubble.clear()
        self._clear_reply_target()

    def add_comment_bubble(self, msg: types.Message, outgoing: bool, emojis: List[str]) -> MessageBubble:
        bubble = MessageBubble(msg, outgoing, True, show_reply_btn=True, emojis=emojis, parent=self)
        bubble.reactClicked.connect(lambda message, emoji: self.reactInComment.emit(message, emoji))
        bubble.replyClicked.connect(lambda message=msg: self._select_reply_target(message))
        self.inner_v.insertWidget(self.inner_v.count() - 1, bubble)
        self._msg_map.append(msg)
        if msg and msg.id:
            self._id2bubble[msg.id] = bubble
        return bubble

    def add_comment_bubble_top(self, msg: types.Message, outgoing: bool, emojis: List[str]) -> MessageBubble:
        bubble = MessageBubble(msg, outgoing, True, show_reply_btn=True, emojis=emojis, parent=self)
        bubble.reactClicked.connect(lambda message, emoji: self.reactInComment.emit(message, emoji))
        bubble.replyClicked.connect(lambda message=msg: self._select_reply_target(message))
        self.inner_v.insertWidget(0, bubble)
        if msg and msg.id:
            self._id2bubble[msg.id] = bubble
        return bubble

    def update_comment_reaction(self, msg_id: int, emoji: str, delta: int = 1):
        b = self._id2bubble.get(msg_id)
        if b:
            b.apply_reaction(emoji, delta)

    def _select_reply_target(self, message: types.Message):
        self._reply_target = message
        author = "комментарий"
        try:
            if message.sender:
                author = utils.get_display_name(message.sender) or author
        except Exception:
            pass
        preview = (message.message or "").strip().replace("\n", " ")
        if len(preview) > 40: preview = preview[:37] + "…"
        self.reply_info.setText(f"Ответ на: {author} — «{preview}»")
        self.reply_cancel.setVisible(True)

    def _clear_reply_target(self):
        self._reply_target = None
        self.reply_info.setText("")
        self.reply_cancel.setVisible(False)

    def _emit_send(self):
        text = self.input.text().strip()
        if not text: return
        self.sendComment.emit(text)
        self.input.clear()

    def current_reply_target(self) -> Optional[types.Message]:
        return self._reply_target

    def clear_reply_indicator(self):
        self._clear_reply_target()

# =============== Главное окно ===============
class MainWindow(QMainWindow):
    _IMG_EXT = (".png",".jpg",".jpeg",".webp",".gif",".bmp")

    def __init__(self, loop: asyncio.AbstractEventLoop):
        super().__init__()
        self.loop = loop
        self.setWindowTitle("Telegram Multi-Client (Telethon + PySide6)")
        self.resize(1480, 940)
        self.accounts: Dict[int, Account] = {}
        self.pins: List[str] = load_pins()      # ГЛОБАЛЬНЫЙ список
        self.rr_order: List[int] = []
        self._rr_pointer = 0
        self._uid_to_item: Dict[int, QListWidgetItem] = {}

        # память реакций
        self._react_mem: Dict[str, str] = load_json(REACTIONS_CACHE_FILE, {})

        # proxies
        self.proxies_cfg = _load_proxies_config()

        self.current_view_account_id: Optional[int] = None
        self.current_entity_ref: Optional[str] = None
        self.current_entity_title: str = ""
        self._main_reply_target: Optional[types.Message] = None

        # контекст комментариев
        self._comments_ctx_entity_ref: Optional[str] = None
        self._comments_ctx_post_id: Optional[int] = None
        self._comments_ctx_root_discussion_id: Optional[int] = None
        self._comments_ctx_acc: Optional[Account] = None
        self._comments_pending_file_path: Optional[str] = None

        # live обновление комментов
        self._comments_timer = QTimer(self)
        self._comments_timer.setInterval(2500)
        self._comments_timer.timeout.connect(lambda: asyncio.create_task(self._refresh_comments_tick()))
        self._comments_known_ids: set[int] = set()

        # кеш InputPeer
        self._peer_cache: Dict[Tuple[int, str], object] = {}  # (uid, ref) -> InputPeer

        self._init_ui()
        self._apply_style()
        self.loop.set_exception_handler(self._asyncio_exception_handler)

        # мгновенное заполнение из кэша
        self._prepopulate_accounts_from_cache()

        QTimer.singleShot(0, lambda: asyncio.create_task(self._startup_boot()))

        self._last_loaded_messages: List[types.Message] = []
        self._reply_kb = None
        self._reply_kb_sig = None

        # Reply keyboard (основное меню)
        self._reply_kb: Optional[types.ReplyKeyboardMarkup] = None

        # перехват Ctrl+V в полях ввода (прикреплять картинку, а не ссылку)
        self.input.installEventFilter(self)
        self.comments.input.installEventFilter(self)

        # держим ссылки на открытые модальные окна, чтобы GC их не прибил
        self._phone_login_dialog: Optional[PhoneLoginDialog] = None
        self._qr_login_dialog: Optional[QRLoginDialog] = None

    # ----- message boxes -----
    async def _mb_info(self, title, text): await asyncio.sleep(0); QMessageBox.information(self, title, text)
    async def _mb_warn(self, title, text): await asyncio.sleep(0); QMessageBox.warning(self, title, text)
    async def _mb_crit(self, title, text): await asyncio.sleep(0); QMessageBox.critical(self, title, text)

    # ----- утилиты -----
    def _mark_account_item(self, uid: int, color: str, tooltip: str = ""):
        it = self._uid_to_item.get(uid)
        if it:
            it.setForeground(QColor(color))
            it.setToolTip(tooltip or "")

    def _is_frozen_error(self, e: Exception) -> bool:
        if isinstance(e, RPCError):
            msg = (e.__class__.__name__ + " " + (getattr(e, "message", "") or "")).upper()
            if "FROZEN" in msg:
                return True
        return "FROZEN" in str(e).upper()

    async def _probe_account_health(self, client: TelegramClient) -> Tuple[bool, str]:
        try:
            await client(functions.account.UpdateStatusRequest(offline=True))
            return True, ""
        except (UserDeactivatedBanError, UserDeactivatedError,
                SessionRevokedError, AuthKeyUnregisteredError) as e:
            return False, e.__class__.__name__
        except Exception as e:
            if self._is_frozen_error(e):
                return False, "FROZEN"
            return True, ""

    # ----- Единый раннер Telethon -----
    async def _run_acc(self, acc: Account, coro):
        tries = 0
        while True:
            try:
                async with acc.api_lock:
                    return await coro
            except (UserDeactivatedBanError, UserDeactivatedError,
                    SessionRevokedError, AuthKeyUnregisteredError) as e:
                await self._kill_account(acc, f"Недоступен: {e.__class__.__name__}")
                raise
            except (PeerFloodError, FloodWaitError) as e:
                self._mark_account_item(acc.user_id, "#e3b341", "PeerFlood/FloodWait: временный лимит.")
                raise
            except Exception as e:
                if self._is_frozen_error(e):
                    await self._kill_account(acc, "Аккаунт заморожен (read-only)")
                    raise
                msg = str(e)
                if "Cannot enter into task" in msg and tries < 3:
                    tries += 1
                    await asyncio.sleep(0.15 * tries)
                    continue
                raise

    async def _kill_account(self, acc: Account, reason: str):
        try: await acc.client.disconnect()
        except: pass
        try: acc.session_path.unlink(missing_ok=True)
        except: pass
        self.accounts.pop(acc.user_id, None)
        if acc.user_id in self.rr_order:
            i = self.rr_order.index(acc.user_id)
            self.rr_order.remove(acc.user_id)
            if i <= self._rr_pointer and self._rr_pointer > 0:
                self._rr_pointer -= 1
        it = self._uid_to_item.pop(acc.user_id, None)
        if it:
            row = self.acc_list.row(it)
            self.acc_list.takeItem(row)
        self._rebuild_manual_acc_combo()
        if self.current_view_account_id == acc.user_id:
            self.current_view_account_id = None
            self._clear_chat_area(); self.chat_title.setText("Выберите чат")
        await self._mb_warn("Аккаунт удалён", f"{friendly_display(acc.user)} ({acc.user_id}): {reason}")
        self._save_accounts_cache()
        self._update_labels()

    # ----- UI -----
    def _acc_human(self, acc: Optional[Account]) -> str:
        if not acc: return "—"
        name = " ".join(filter(None, [acc.user.first_name, acc.user.last_name])).strip() if acc.user else ""
        if name:
            return name
        if acc.user and acc.user.username:
            return f"@{acc.user.username}"
        return acc.display

    async def _startup_boot(self):
        pool = (self.proxies_cfg or {}).get("pool") or []
        if not pool:
            await self._mb_info("Прокси", "Сначала загрузите прокси — затем подключим аккаунты.")
            await self._on_load_proxies()
        else:
            await self._auto_load_sessions()

    def _init_ui(self):
        menubar = self.menuBar()
        m_acc = menubar.addMenu("Аккаунты")
        act_add_phone = QAction("Добавить (телефон)", self)
        act_add_qr = QAction("Добавить (QR)", self)
        act_del = QAction("Удалить выбранный", self)
        m_acc.addAction(act_add_phone); m_acc.addAction(act_add_qr); m_acc.addSeparator(); m_acc.addAction(act_del)
        act_add_phone.triggered.connect(lambda: asyncio.create_task(self._on_add_phone()))
        act_add_qr.triggered.connect(lambda: asyncio.create_task(self._on_add_qr()))
        act_del.triggered.connect(lambda: asyncio.create_task(self._on_delete_account()))

        m_prof = menubar.addMenu("Профиль")
        act_name = QAction("Сменить имя/фамилию", self)
        act_avatar = QAction("Сменить аватар", self)
        act_username = QAction("Сменить @username", self)
        m_prof.addAction(act_name); m_prof.addAction(act_avatar); m_prof.addAction(act_username)
        act_name.triggered.connect(lambda: asyncio.create_task(self._on_change_name()))
        act_avatar.triggered.connect(lambda: asyncio.create_task(self._on_change_avatar()))
        act_username.triggered.connect(lambda: asyncio.create_task(self._on_change_username()))
        
        act_phone = QAction("Показать номер телефона", self)
        act_2fa = QAction("Сменить пароль 2FA", self)
        m_prof.addAction(act_phone); m_prof.addAction(act_2fa)
        act_phone.triggered.connect(lambda: asyncio.create_task(self._on_show_phone()))
        act_2fa.triggered.connect(lambda: asyncio.create_task(self._on_change_2fa()))


        m_srv = menubar.addMenu("Сервис")
        act_reload = QAction("Переподхват сессий", self)
        m_srv.addAction(act_reload)
        act_reload.triggered.connect(lambda: asyncio.create_task(self._auto_load_sessions(force=True)))

        splitter = QSplitter(Qt.Horizontal, self)
        self.setCentralWidget(splitter)

        # Левая колонка
        left = QWidget(self); lv = QVBoxLayout(left); lv.setContentsMargins(8,8,8,8); lv.setSpacing(8)

        self.btn_load_proxies = QPushButton("ЗАГРУЗИТЬ ПРОКСИ", self)
        self.btn_load_proxies.setStyleSheet(
            "QPushButton {background:#d7263d; color:#fff; font-weight:800; padding:10px 12px; border-radius:12px;}"
            "QPushButton:hover{background:#f03c51;}"
        )
        self.btn_load_proxies.clicked.connect(lambda: asyncio.create_task(self._on_load_proxies()))
        lv.addWidget(self.btn_load_proxies)

        lbl_acc = QLabel("Аккаунты:", self); lbl_acc.setObjectName("sectionLabel")
        self.acc_list = QListWidget(self)
        self.acc_list.currentItemChanged.connect(lambda cur, prev: asyncio.create_task(self._on_account_changed(cur, prev)))
        lv.addWidget(lbl_acc); lv.addWidget(self.acc_list)

        lbl_pins = QLabel("Закреплённые:", self); lbl_pins.setObjectName("sectionLabel")
        lv.addWidget(lbl_pins)
        self.pins_row = QVBoxLayout(); self.pins_row.setSpacing(6)
        pins_wrap = QWidget(self); pins_wrap.setLayout(self.pins_row)
        pins_scroll = QScrollArea(self); pins_scroll.setWidgetResizable(True); pins_scroll.setWidget(pins_wrap); pins_scroll.setFixedHeight(110)
        lv.addWidget(pins_scroll)

        open_row = QHBoxLayout()
        self.open_edit = QLineEdit(self); self.open_edit.setPlaceholderText("Открыть по @username или ID …")
        self.btn_open = QPushButton("Открыть", self)
        open_row.addWidget(self.open_edit, 1); open_row.addWidget(self.btn_open)
        self.btn_open.clicked.connect(lambda: asyncio.create_task(self._on_open_by_ref()))
        lv.addLayout(open_row)

        lbl_dlgs = QLabel("Диалоги:", self); lbl_dlgs.setObjectName("sectionLabel")
        self.dlg_list = QListWidget(self)
        lv.addWidget(lbl_dlgs); lv.addWidget(self.dlg_list, 1)
        self.dlg_list.itemClicked.connect(self._on_dialog_clicked)

        splitter.addWidget(left)

        # Центр
        center = QWidget(self); cv = QVBoxLayout(center); cv.setContentsMargins(8,8,8,8); cv.setSpacing(8)
        self.chat_title = QLabel("Выберите чат", self); self.chat_title.setObjectName("chatTitle")
        cv.addWidget(self.chat_title)

        media_opts = QHBoxLayout()
        self.cb_autoshow_media = QCheckBox("Автопоказ медиа (до 5)", self)
        self.cb_save_unknown = QCheckBox("Сохранять неизвестные медиа в файл", self)
        media_opts.addWidget(self.cb_autoshow_media)
        media_opts.addWidget(self.cb_save_unknown)
        media_opts.addStretch(1)
        cv.addLayout(media_opts)

        self.chat_area = QScrollArea(self); self.chat_area.setWidgetResizable(True)
        self.chat_inner = QWidget(self.chat_area)
        self.chat_v = QVBoxLayout(self.chat_inner); self.chat_v.setContentsMargins(0,0,0,0); self.chat_v.setSpacing(8)
        self.chat_v.addStretch(1)
        self.chat_area.setWidget(self.chat_inner)
        cv.addWidget(self.chat_area, 1)

        reply_bar = QHBoxLayout()
        self.reply_info_main = QLabel("", self)
        self.reply_cancel_main = QPushButton("Отменить ответ", self); self.reply_cancel_main.setVisible(False)
        self.reply_cancel_main.clicked.connect(self._clear_main_reply_target)
        reply_bar.addWidget(self.reply_info_main, 1); reply_bar.addWidget(self.reply_cancel_main)
        cv.addLayout(reply_bar)

        # индикатор прикреплённого файла к сообщению
        pend_row = QHBoxLayout()
        self.pending_main_info = QLabel("", self); self.pending_main_info.setVisible(False)
        self.btn_pending_clear = QPushButton("×", self); self.btn_pending_clear.setVisible(False)
        self.btn_pending_clear.setFixedWidth(28)
        self.btn_pending_clear.clicked.connect(self._clear_pending_main_file)
        pend_row.addWidget(self.pending_main_info, 1); pend_row.addWidget(self.btn_pending_clear)
        cv.addLayout(pend_row)

        pin_row = QHBoxLayout()
        self.btn_pin = QPushButton("Закрепить", self)
        self.btn_unpin = QPushButton("Открепить", self)
        pin_row.addWidget(self.btn_pin); pin_row.addWidget(self.btn_unpin); pin_row.addStretch(1)
        self.btn_pin.clicked.connect(lambda: asyncio.create_task(self._on_pin_current()))
        self.btn_unpin.clicked.connect(lambda: asyncio.create_task(self._on_unpin_current()))
        cv.addLayout(pin_row)

        mode_row = QHBoxLayout()
        self.cb_auto = QCheckBox("Автоотправка с разных аккаунтов", self)
        self.mode = QComboBox(self); self.mode.addItems(["Поочерёдно", "Рандомно", "Ручной"])
        self.manual_acc = QComboBox(self); self._rebuild_manual_acc_combo()
        self.cb_switch_after_reaction = QCheckBox("Переключать аккаунт после реакции", self)
        self.cb_switch_after_reaction.setChecked(True)
        self.next_acc_label = QLabel("Следующий аккаунт: —", self)
        self.active_acc_label = QLabel("Отправляет: —", self)
        mode_row.addWidget(self.cb_auto); mode_row.addWidget(QLabel("Режим:", self)); mode_row.addWidget(self.mode)
        mode_row.addWidget(QLabel("Аккаунт:", self)); mode_row.addWidget(self.manual_acc)
        mode_row.addWidget(self.cb_switch_after_reaction)
        mode_row.addStretch(1); mode_row.addWidget(self.active_acc_label); mode_row.addWidget(self.next_acc_label)
        cv.addLayout(mode_row)

        # смайлы для текста
        emoji_row = QHBoxLayout()
        emoji_row.addWidget(QLabel("Смайлы:", self))
        for emo in ["😀","😂","🔥","👍","👎","❤️","😮","😢","👏"]:
            b = QToolButton(self); b.setText(emo)
            b.clicked.connect(lambda _, e=emo: self._append_emoji(e))
            emoji_row.addWidget(b)
        self.btn_sticker = QPushButton("Стикер", self)
        self.btn_sticker.clicked.connect(lambda: asyncio.create_task(self._on_send_sticker()))
        emoji_row.addWidget(self.btn_sticker)
        emoji_row.addStretch(1)
        cv.addLayout(emoji_row)

        # Панель reply-клавиатуры (основное меню)
        # Панель reply-клавиатуры (основное меню) — один раз
        self.reply_kb_panel = QWidget(self)
        self.reply_kb_panel.setVisible(False)
        self.reply_kb_v = QVBoxLayout(self.reply_kb_panel)
        self.reply_kb_v.setContentsMargins(0,0,0,0)
        self.reply_kb_v.setSpacing(6)
        cv.addWidget(self.reply_kb_panel)

        send_row = QHBoxLayout()
        self.input = QLineEdit(self); self.input.setPlaceholderText("Напишите сообщение… (Enter — отправить)")
        self.btn_attach = QPushButton("📎", self)
        self.btn_send = QPushButton("Отправить", self)
        self.input.returnPressed.connect(lambda: asyncio.create_task(self._on_send()))
        self.btn_send.clicked.connect(lambda: asyncio.create_task(self._on_send()))
        self.btn_attach.clicked.connect(lambda: asyncio.create_task(self._on_attach()))
        send_row.addWidget(self.input, 1); send_row.addWidget(self.btn_attach); send_row.addWidget(self.btn_send)
        cv.addLayout(send_row)

        splitter.addWidget(center)

        # Правая колонка (комменты)
        self.comments = CommentsPanel(self)
        self.comments.sendComment.connect(lambda txt: asyncio.create_task(self._on_send_comment(txt)))
        self.comments.reactInComment.connect(lambda msg, emo: asyncio.create_task(self._on_react_in_comment(msg, emo)))
        self.comments.btn_attach.clicked.connect(lambda: asyncio.create_task(self._on_attach_comment()))
        splitter.addWidget(self.comments)
        splitter.setSizes([340, 880, 360])

        self.cb_auto.stateChanged.connect(lambda *_: self._update_labels())
        self.mode.currentIndexChanged.connect(lambda *_: self._update_labels())
        self.manual_acc.currentIndexChanged.connect(lambda *_: self._update_labels())
        self.cb_switch_after_reaction.stateChanged.connect(lambda *_: self._update_labels())

    def _apply_style(self):
        force_dark_palette(QApplication.instance())
        self.setStyleSheet("""
        * { font-size: 14px; color: #eaf2ff; }
        QMainWindow { background: #0b1220; }
        QLabel#sectionLabel { color: #c9d8ff; font-weight: 600; padding: 2px 0; }
        QLabel#chatTitle { font-size: 20px; font-weight: 700; color: #ffffff; padding: 6px 8px; }
        QLabel#commentsTitle { font-size: 18px; font-weight: 700; color: #ffffff; padding: 6px 8px; }
        QLabel#replyInfo { color: #d1e0ff; }
        QListWidget, QLineEdit, QScrollArea, QComboBox, QDialog, QMessageBox, QMenu {
            background: #121a2b; border: 1px solid #273356; border-radius: 10px;
        }
        QMenu::item { padding:6px 12px; color:#eaf2ff; }
        QMenu::item:selected { background:#1a2440; }
        QLineEdit { padding: 8px 10px; color:#ffffff; background:#121a2b; }
        QScrollArea QWidget { background: transparent; }
        QPushButton {
            background: #2d6cdf; color: #ffffff; border: none; border-radius: 10px; padding: 8px 12px; font-weight: 600;
        }
        QPushButton:hover { background: #3c7cf0; }
        QPushButton:disabled { background: #304a79; color: #d0d6ea; }
        QToolButton {
            background: #1a2440; border: 1px solid #2b3a66; border-radius: 10px; padding: 4px 6px; color: #eaf2ff;
        }
        QToolButton:hover { background: #24325a; }
        QCheckBox { padding: 2px; color: #ffffff; }
        QFrame#bubbleIn {
            background: qlineargradient(x1:0,y1:0,x2:0,y2:1, stop:0 #18243d, stop:1 #132036);
            border: 1px solid #2a3a63; border-radius: 16px; margin: 6px 120px 6px 8px;
        }
        QFrame#bubbleOut {
            background: qlineargradient(x1:0,y1:0,x2:0,y2:1, stop:0 #264a8a, stop:1 #1f3b70);
            border: 1px solid #3c5da1; border-radius: 16px; margin: 6px 8px 6px 120px;
        }
        QLabel#bubbleAuthor { color: #e8eeff; font-weight: 700; }
        QLabel#bubbleTime { color: #cbd5ff; }
        QMenuBar { background: #0b1220; color: #eaf2ff; }
        QMenuBar::item:selected { background: #1a2440; }
        QToolTip { background:#121a2b; color:#eaf2ff; border:1px solid #273356; }
        """)

    # ----- Исключения -----
    def _asyncio_exception_handler(self, loop, context):
        exc = context.get("exception")
        if isinstance(exc, RuntimeError) and "Cannot enter into task" in str(exc):
            return
        msg = exc or context.get("message")
        print("Async exception:", msg, file=sys.stderr)
        traceback.print_exc()
        QTimer.singleShot(0, lambda: QMessageBox.critical(self, "Ошибка", f"{msg}"))

    # ----- Кэш аккаунтов -----
    def _load_accounts_cache(self) -> list:
        return load_json(ACCOUNTS_CACHE_FILE, [])

    def _save_accounts_cache(self):
        data = []
        for uid, acc in self.accounts.items():
            u = acc.user
            data.append({
                "user_id": uid,
                "display": acc.display,
                "username": (u.username if u else None),
                "first_name": (u.first_name if u else None),
                "last_name": (u.last_name if u else None),
                "session": acc.session_path.name,
            })
        save_json(ACCOUNTS_CACHE_FILE, data)

    def _prepopulate_accounts_from_cache(self):
        cache = self._load_accounts_cache()
        for it in cache:
            try:
                uid = int(it.get("user_id"))
            except Exception:
                continue
            title = it.get("display") or f"id{uid}"
            if uid in self._uid_to_item:
                continue
            item = QListWidgetItem(f"{title} ({uid})")
            item.setData(Qt.UserRole, uid)
            item.setForeground(QColor("#9fb3d9"))
            item.setToolTip("Подключение…")
            self.acc_list.addItem(item)
            self._uid_to_item[uid] = item

    # ----- Сессии -----
    async def _auto_load_sessions(self, force: bool=False):
        if force:
            for acc in list(self.accounts.values()):
                try: await acc.client.disconnect()
                except Exception: pass
            self.accounts.clear(); self.acc_list.clear(); self.rr_order.clear()
            self._rr_pointer = 0
            self._uid_to_item.clear()

        files = sorted(SESS_DIR.glob("*.session"))
        seen_user_ids = set()
        for f in files:
            try:
                proxy_tuple = None
                sess_key = f.name
                idx = self.proxies_cfg.get("assignments_by_session", {}).get(sess_key, None)
                if idx is not None:
                    pool = self.proxies_cfg.get("pool", [])
                    if 0 <= idx < len(pool):
                        proxy_tuple = _telethon_proxy_tuple_from_cfg(pool[idx])

                client = TelegramClient(str(f), API_ID, API_HASH, proxy=proxy_tuple)
                await client.connect()
                me = await client.get_me()
                if not me:
                    await client.disconnect(); continue

                ok, reason = await self._probe_account_health(client)
                if not ok:
                    try: await client.disconnect()
                    except: pass
                    try: f.unlink(missing_ok=True)
                    except: pass
                    print(f"[СЕССИЯ] Удалена сессия {f.name}: {reason}")
                    continue

                uid = me.id
                if idx is not None:
                    self.proxies_cfg.setdefault("assignments_by_user", {})[str(uid)] = idx
                    _save_proxies_config(self.proxies_cfg)

                if uid in seen_user_ids:
                    await client.disconnect()
                    try: f.unlink(missing_ok=True)
                    except: pass
                    continue
                seen_user_ids.add(uid)
                acc = Account(
                    session_path=f,
                    client=client,
                    user=me,
                    user_id=uid,
                    display=friendly_display(me),
                    api_lock=asyncio.Lock()
                )
                self.accounts[uid] = acc
                if uid not in self.rr_order:
                    self.rr_order.append(uid)
                self._add_account_to_ui(acc)

                tip = []
                if idx is not None:
                    pr = self.proxies_cfg["pool"][idx]
                    tip.append(f"Прокси: {pr.get('scheme')}://{pr.get('host')}:{pr.get('port')}")
                self._mark_account_item(uid, "#a0e6a0", " | ".join(tip))

            except Exception as e:
                print(f"[СЕССИЯ] Не удалось подхватить {f.name}: {e}")
        self._rebuild_manual_acc_combo()
        self._save_accounts_cache()
        self._update_labels()

    def _add_account_to_ui(self, acc: Account):
        if acc.user_id in self._uid_to_item:
            it = self._uid_to_item[acc.user_id]
            it.setText(f"{acc.display} ({acc.user_id})")
            it.setForeground(QColor("#eaf2ff"))
            it.setToolTip("")
        else:
            it = QListWidgetItem(f"{acc.display} ({acc.user_id})")
            it.setData(Qt.UserRole, acc.user_id)
            self.acc_list.addItem(it)
            self._uid_to_item[acc.user_id] = it

    def _rebuild_manual_acc_combo(self):
        cur_val = self.manual_acc.currentData(Qt.UserRole) if self.manual_acc.count() else None
        self.manual_acc.clear()
        for uid, a in self.accounts.items():
            self.manual_acc.addItem(f"{a.display}", userData=uid)
        if cur_val is not None:
            ix = max(0, self.manual_acc.findData(cur_val))
            self.manual_acc.setCurrentIndex(ix)

    # ----- Профиль -----
    async def _on_change_name(self):
        it = self.acc_list.currentItem()
        if not it: return await self._mb_info("Профиль", "Выберите аккаунт слева.")
        uid = it.data(Qt.UserRole)
        acc = self.accounts.get(uid)
        if not acc: return
        first, ok1 = QInputDialog.getText(self, "Имя", "Новое имя:", QLineEdit.Normal,
                                          acc.user.first_name or "")
        if not ok1: return
        last, ok2 = QInputDialog.getText(self, "Фамилия", "Новая фамилия (можно пусто):",
                                         QLineEdit.Normal, acc.user.last_name or "")
        if not ok2: return
        try:
            await self._run_acc(acc, acc.client(functions.account.UpdateProfileRequest(
                first_name=first.strip() or None,
                last_name=(last.strip() or None)
            )))
            me = await self._run_acc(acc, acc.client.get_me())
            acc.user = me; acc.display = friendly_display(me)
            if it.data(Qt.UserRole) == acc.user_id:
                it.setText(f"{acc.display} ({acc.user_id})")
            self._rebuild_manual_acc_combo()
            self._save_accounts_cache()
            await self._mb_info("Профиль", "Имя/фамилия обновлены.")
        except (PeerFloodError, FloodWaitError) as e:
            await self._mb_warn("Лимит", f"Временный лимит: {e}")
        except Exception as e:
            if self._is_frozen_error(e):
                await self._kill_account(acc, "Аккаунт заморожен (read-only)")
            else:
                await self._mb_warn("Профиль", f"Не удалось обновить имя: {e}")

    async def _on_change_username(self):
        it = self.acc_list.currentItem()
        if not it: return await self._mb_info("Профиль", "Выберите аккаунт слева.")
        uid = it.data(Qt.UserRole)
        acc = self.accounts.get(uid)
        if not acc: return
        new_u, ok = QInputDialog.getText(self, "Username", "Введите новый @username (без @):",
                                         QLineEdit.Normal, acc.user.username or "")
        if not ok: return
        new_u = new_u.strip().lstrip("@")
        if not new_u: return await self._mb_warn("Username", "Username не должен быть пустым.")
        try:
            r = await self._run_acc(acc, acc.client(functions.account.UpdateUsernameRequest(username=new_u)))
            me = await self._run_acc(acc, acc.client.get_me())
            acc.user = me; acc.display = friendly_display(me)
            it = self._uid_to_item.get(acc.user_id)
            if it: it.setText(f"{acc.display} ({acc.user_id})")
            self._rebuild_manual_acc_combo()
            self._save_accounts_cache()
            await self._mb_info("Username", f"Новый @username: @{r.username}")
        except Exception as e:
            if self._is_frozen_error(e):
                await self._kill_account(acc, "Аккаунт заморожен (read-only)")
            else:
                await self._mb_warn("Username", f"Не удалось сменить: {e}")

    async def _on_change_avatar(self):
        it = self.acc_list.currentItem()
        if not it: return await self._mb_info("Аватар", "Выберите аккаунт слева.")
        uid = it.data(Qt.UserRole)
        acc = self.accounts.get(uid)
        if not acc: return
        fn, _ = QFileDialog.getOpenFileName(self, "Выберите картинку", "", "Изображения (*.png *.jpg *.jpeg *.webp)")
        if not fn: return
        try:
            up = await self._run_acc(acc, acc.client.upload_file(fn))
            await self._run_acc(acc, acc.client(functions.photos.UploadProfilePhotoRequest(file=up)))
            await self._mb_info("Аватар", "Аватар обновлён.")
        except Exception as e:
            if self._is_frozen_error(e):
                await self._kill_account(acc, "Аккаунт заморожен (read-only)")
            else:
                await self._mb_warn("Аватар", f"Не удалось сменить аватар: {e}")

    async def _on_show_phone(self):
        it = self.acc_list.currentItem()
        if not it:
            return await self._mb_info('Профиль', 'Выберите аккаунт слева.')
        uid = it.data(Qt.UserRole)
        acc = self.accounts.get(uid)
        if not acc:
            return
        try:
            me = await self._run_acc(acc, acc.client.get_me())
            acc.user = me
        except Exception:
            me = acc.user
        phone = getattr(me, 'phone', None)
        if phone:
            ph = phone if phone.startswith('+') else '+' + phone
            await self._mb_info('Телефон', f'Номер: {ph}')
        else:
            await self._mb_info('Телефон', 'Номер недоступен (бот/не задан).')

    async def _on_change_2fa(self):
        it = self.acc_list.currentItem()
        if not it:
            return await self._mb_info('2FA', 'Выберите аккаунт слева.')
        uid = it.data(Qt.UserRole)
        acc = self.accounts.get(uid)
        if not acc:
            return

        # просим текущий пароль (можно пусто)
        cur, ok1 = QInputDialog.getText(self, '2FA', 'Текущий пароль (если включён):', QLineEdit.Password)
        if not ok1:
            return
        # новый пароль (пусто — отключить 2FA)
        newp, ok2 = QInputDialog.getText(self, '2FA', 'Новый пароль (пусто — отключить 2FA):', QLineEdit.Password)
        if not ok2:
            return
        if newp:
            conf, ok3 = QInputDialog.getText(self, '2FA', 'Повторите новый пароль:', QLineEdit.Password)
            if not ok3:
                return
            if newp != conf:
                return await self._mb_warn('2FA', 'Пароли не совпадают.')
        # подсказка (необязательно)
        hint, ok4 = QInputDialog.getText(self, '2FA', 'Подсказка (необязательно):', QLineEdit.Normal, '')
        if not ok4:
            return

        try:
            await self._run_acc(
                acc,
                acc.client.edit_2fa(
                    current_password=(cur or None),
                    new_password=(newp or None),   # None => отключить 2FA
                    hint=(hint.strip() or None),
                    email=None, email_code=None
                )
            )
            if newp:
                await self._mb_info('2FA', 'Пароль 2FA обновлён.')
            else:
                await self._mb_info('2FA', 'Двухэтапная аутентификация отключена.')
        except PasswordHashInvalidError:
            await self._mb_warn('2FA', 'Неверный текущий пароль.')
        except EmailUnconfirmedError:
            await self._mb_warn('2FA', 'Требуется подтверждение email. Повторите без изменения email или подтвердите почту в Telegram.')
        except RPCError as e:
            await self._mb_warn('2FA', f'Ошибка Telegram: {e}')
        except Exception as e:
            await self._mb_warn('2FA', f'{e}')
    async def _on_show_phone(self):
        it = self.acc_list.currentItem()
        if not it:
            return await self._mb_info('Профиль', 'Выберите аккаунт слева.')
        uid = it.data(Qt.UserRole)
        acc = self.accounts.get(uid)
        if not acc:
            return
        try:
            me = await self._run_acc(acc, acc.client.get_me())
            acc.user = me
        except Exception:
            me = acc.user
        phone = getattr(me, 'phone', None)
        if phone:
            ph = phone if phone.startswith('+') else '+' + phone
            await self._mb_info('Телефон', f'Номер: {ph}')
        else:
            await self._mb_info('Телефон', 'Номер недоступен (бот/не задан).')

    async def _on_change_2fa(self):
        it = self.acc_list.currentItem()
        if not it:
            return await self._mb_info('2FA', 'Выберите аккаунт слева.')
        uid = it.data(Qt.UserRole)
        acc = self.accounts.get(uid)
        if not acc:
            return

        # просим текущий пароль (можно пусто)
        cur, ok1 = QInputDialog.getText(self, '2FA', 'Текущий пароль (если включён):', QLineEdit.Password)
        if not ok1:
            return
        # новый пароль (пусто — отключить 2FA)
        newp, ok2 = QInputDialog.getText(self, '2FA', 'Новый пароль (пусто — отключить 2FA):', QLineEdit.Password)
        if not ok2:
            return
        if newp:
            conf, ok3 = QInputDialog.getText(self, '2FA', 'Повторите новый пароль:', QLineEdit.Password)
            if not ok3:
                return
            if newp != conf:
                return await self._mb_warn('2FA', 'Пароли не совпадают.')
        # подсказка (необязательно)
        hint, ok4 = QInputDialog.getText(self, '2FA', 'Подсказка (необязательно):', QLineEdit.Normal, '')
        if not ok4:
            return

        try:
            await self._run_acc(
                acc,
                acc.client.edit_2fa(
                    current_password=(cur or None),
                    new_password=(newp or None),   # None => отключить 2FA
                    hint=(hint.strip() or None),
                    email=None, email_code=None
                )
            )
            if newp:
                await self._mb_info('2FA', 'Пароль 2FA обновлён.')
            else:
                await self._mb_info('2FA', 'Двухэтапная аутентификация отключена.')
        except PasswordHashInvalidError:
            await self._mb_warn('2FA', 'Неверный текущий пароль.')
        except EmailUnconfirmedError:
            await self._mb_warn('2FA', 'Требуется подтверждение email. Повторите без изменения email или подтвердите почту в Telegram.')
        except RPCError as e:
            await self._mb_warn('2FA', f'Ошибка Telegram: {e}')
        except Exception as e:
            await self._mb_warn('2FA', f'{e}')
    async def _on_show_phone(self):
        it = self.acc_list.currentItem()
        if not it:
            return await self._mb_info('Профиль', 'Выберите аккаунт слева.')
        uid = it.data(Qt.UserRole)
        acc = self.accounts.get(uid)
        if not acc:
            return
        try:
            me = await self._run_acc(acc, acc.client.get_me())
            acc.user = me
        except Exception:
            me = acc.user
        phone = getattr(me, 'phone', None)
        if phone:
            ph = phone if phone.startswith('+') else '+' + phone
            await self._mb_info('Телефон', f'Номер: {ph}')
        else:
            await self._mb_info('Телефон', 'Номер недоступен (бот/не задан).')

    async def _on_change_2fa(self):
        it = self.acc_list.currentItem()
        if not it:
            return await self._mb_info('2FA', 'Выберите аккаунт слева.')
        uid = it.data(Qt.UserRole)
        acc = self.accounts.get(uid)
        if not acc:
            return

        # просим текущий пароль (можно пусто)
        cur, ok1 = QInputDialog.getText(self, '2FA', 'Текущий пароль (если включён):', QLineEdit.Password)
        if not ok1:
            return
        # новый пароль (пусто — отключить 2FA)
        newp, ok2 = QInputDialog.getText(self, '2FA', 'Новый пароль (пусто — отключить 2FA):', QLineEdit.Password)
        if not ok2:
            return
        if newp:
            conf, ok3 = QInputDialog.getText(self, '2FA', 'Повторите новый пароль:', QLineEdit.Password)
            if not ok3:
                return
            if newp != conf:
                return await self._mb_warn('2FA', 'Пароли не совпадают.')
        # подсказка (необязательно)
        hint, ok4 = QInputDialog.getText(self, '2FA', 'Подсказка (необязательно):', QLineEdit.Normal, '')
        if not ok4:
            return

        try:
            await self._run_acc(
                acc,
                acc.client.edit_2fa(
                    current_password=(cur or None),
                    new_password=(newp or None),   # None => отключить 2FA
                    hint=(hint.strip() or None),
                    email=None, email_code=None
                )
            )
            if newp:
                await self._mb_info('2FA', 'Пароль 2FA обновлён.')
            else:
                await self._mb_info('2FA', 'Двухэтапная аутентификация отключена.')
        except PasswordHashInvalidError:
            await self._mb_warn('2FA', 'Неверный текущий пароль.')
        except EmailUnconfirmedError:
            await self._mb_warn('2FA', 'Требуется подтверждение email. Повторите без изменения email или подтвердите почту в Telegram.')
        except RPCError as e:
            await self._mb_warn('2FA', f'Ошибка Telegram: {e}')
        except Exception as e:
            await self._mb_warn('2FA', f'{e}')
    # ----- Добавить аккаунт (телефон) -----
    async def _on_add_phone(self):
        d = PhoneLoginDialog(self)
        self._phone_login_dialog = d  # держим ссылку
        d.show()

        client_holder = {"client": None, "phone": None, "hash": None, "sess": None}

        def _delivery_label(sent_code_obj) -> str:
            try:
                t = type(getattr(sent_code_obj, "type", None)).__name__
            except Exception:
                t = ""
            if "App" in t:   return "через Telegram (в приложении)"
            if "Sms" in t:   return "по SMS"
            if "Call" in t:  return "звонком"
            if "Email" in t: return "по email"
            return "неизвестным способом"

        async def do_request():
            raw_phone = d.phone.text().strip()
            if not raw_phone:
                return await self._mb_warn("Логин", "Укажите телефон.")
            phone = _normalize_phone(raw_phone)

            d.btn_request.setEnabled(False)
            try:
                sess = SESS_DIR / f"{uuid.uuid4().hex}.session"
                proxy_tuple = None
                pool = self.proxies_cfg.get("pool", [])
                if pool:
                    proxy_tuple = _telethon_proxy_tuple_from_cfg(pool[0])
                client = TelegramClient(str(sess), API_ID, API_HASH, proxy=proxy_tuple)
                await client.connect()

                # --- Совместимые настройки CodeSettings ---
                try:
                    settings = types.CodeSettings(
                        allow_flashcall=False,
                        current_number=False,
                        allow_app_hash=True,
                        allow_sms=True,          # будет отброшено, если версия не поддерживает
                    )
                except TypeError:
                    # В вашей версии этого поля нет — создаём без него
                    settings = types.CodeSettings(
                        allow_flashcall=False,
                        current_number=False,
                        allow_app_hash=True,
                    )

                sent = await client(functions.auth.SendCodeRequest(
                    phone_number=phone,
                    api_id=API_ID,
                    api_hash=API_HASH,
                    settings=settings
                ))

                client_holder.update({
                    "client": client,
                    "phone": phone,
                    "hash": sent.phone_code_hash,
                    "sess": sess
                })

                await self._mb_info(
                    "Код",
                    f"Код отправлен ({_delivery_label(sent)}). "
                    f"Введите его и нажмите «Войти». Если кода нет — проверьте Telegram на этом номере."
                )

            except PhoneNumberBannedError:
                await self._mb_crit("Логин", "Этот номер заблокирован Telegram.")
                try:
                    if client_holder.get("client"):
                        await client_holder["client"].disconnect()
                    if client_holder.get("sess"):
                        client_holder["sess"].unlink(missing_ok=True)
                except:
                    pass
                client_holder.update({"client": None, "phone": None, "hash": None, "sess": None})

            except FloodWaitError as e:
                await self._mb_warn("Логин", f"Слишком частые запросы кода. Подождите {getattr(e, 'seconds', 'несколько')} сек.")
                try:
                    if client_holder.get("client"):
                        await client_holder["client"].disconnect()
                    if client_holder.get("sess"):
                        client_holder["sess"].unlink(missing_ok=True)
                except:
                    pass
                client_holder.update({"client": None, "phone": None, "hash": None, "sess": None})

            except Exception as e:
                await self._mb_crit("Логин", f"{e}")
                try:
                    if client_holder.get("client"):
                        await client_holder["client"].disconnect()
                    if client_holder.get("sess"):
                        client_holder["sess"].unlink(missing_ok=True)
                except:
                    pass
                client_holder.update({"client": None, "phone": None, "hash": None, "sess": None})

            finally:
                d.btn_request.setEnabled(True)

        async def do_submit():
            cl = client_holder.get("client")
            if not cl:
                return await self._mb_warn("Логин", "Сначала запросите код.")
            code = d.code.text().strip()
            pwd = d.pwd.text()
            phone = client_holder["phone"]
            phone_hash = client_holder["hash"]
            try:
                try:
                    await cl.sign_in(phone=phone, code=code, phone_code_hash=phone_hash)
                except SessionPasswordNeededError:
                    if not pwd:
                        return await self._mb_warn("Логин", "Укажите пароль 2FA.")
                    await cl.sign_in(password=pwd)
                me = await cl.get_me()
                acc = Account(
                    session_path=client_holder["sess"],
                    client=cl, user=me, user_id=me.id,
                    display=friendly_display(me), api_lock=asyncio.Lock()
                )
                self.accounts[me.id] = acc
                if me.id not in self.rr_order:
                    self.rr_order.append(me.id)
                self._add_account_to_ui(acc)
                self._rebuild_manual_acc_combo()
                await self._mb_info("Успех", f"Добавлен {acc.display}")
                self._save_accounts_cache()
                self._update_labels()
                d.close()
                self._phone_login_dialog = None
            except PhoneCodeInvalidError:
                await self._mb_warn("Логин", "Неверный код.")
            except FloodWaitError as e:
                await self._mb_warn("Логин", f"Слишком частые попытки входа. Подождите {getattr(e, 'seconds', 'несколько')} сек.")
            except Exception as e:
                await self._mb_crit("Логин", f"{e}")

        d.requestCode.connect(lambda: asyncio.create_task(do_request()))
        d.submitLogin.connect(lambda: asyncio.create_task(do_submit()))


    # ----- Добавить аккаунт (QR) -----
    async def _on_add_qr(self):
        proxy_tuple = None
        pool = self.proxies_cfg.get("pool", [])
        if pool:
            proxy_tuple = _telethon_proxy_tuple_from_cfg(pool[0])

        sess = SESS_DIR / f"{uuid.uuid4().hex}.session"
        client = TelegramClient(str(sess), API_ID, API_HASH, proxy=proxy_tuple)
        await client.connect()

        dlg = QRLoginDialog(self)
        self._qr_login_dialog = dlg
        cancelled = {"v": False}
        dlg.cancelled.connect(lambda: (dlg.close(), cancelled.update(v=True)))
        dlg.show()

        # future для ожидания пароля 2FA из окна
        pwd_future: Optional[asyncio.Future] = None
        def arm_pwd_future():
            nonlocal pwd_future
            if pwd_future is None or pwd_future.done():
                pwd_future = asyncio.get_event_loop().create_future()
            return pwd_future

        dlg.passwordEntered.connect(lambda s: (not cancelled["v"]) and not arm_pwd_future().done() and pwd_future.set_result(s))

        async def qr_flow():
            login = await client.qr_login()
            dlg.set_qr(login.url)
            while not cancelled["v"]:
                try:
                    me = await asyncio.wait_for(login.wait(), timeout=25)
                    return me
                except SessionPasswordNeededError:
                    dlg.show_password_prompt()
                    pf = arm_pwd_future()
                    pwd = await pf
                    await client.sign_in(password=pwd)
                    me = await client.get_me()
                    if me:
                        return me
                except asyncio.TimeoutError:
                    login = await client.qr_login()
                    dlg.set_qr(login.url)
            return None

        try:
            me = await qr_flow()
            if cancelled["v"] or not me:
                try: await client.disconnect()
                except: pass
                try: sess.unlink(missing_ok=True)
                except: pass
                return
            acc = Account(session_path=sess, client=client, user=me, user_id=me.id,
                          display=friendly_display(me), api_lock=asyncio.Lock())
            self.accounts[me.id] = acc
            if me.id not in self.rr_order:
                self.rr_order.append(me.id)
            self._add_account_to_ui(acc)
            self._rebuild_manual_acc_combo()
            await self._mb_info("Успех", f"Добавлен {acc.display}")
            self._save_accounts_cache()
            self._update_labels()
        except Exception as e:
            try: await client.disconnect()
            except: pass
            try: sess.unlink(missing_ok=True)
            except: pass
            await self._mb_crit("QR", f"{e}")
        finally:
            dlg.close()
            self._qr_login_dialog = None

    # ----- Обработчики UI/диалоги/сообщения/комменты -----
    async def _on_delete_account(self):
        it = self.acc_list.currentItem()
        if not it: return await self._mb_info("Удаление", "Выберите аккаунт слева.")
        uid = it.data(Qt.UserRole)
        acc = self.accounts.get(uid)
        if not acc: return
        if QMessageBox.question(self, "Удалить", f"Удалить аккаунт {acc.display} и файл сессии?") != QMessageBox.Yes:
            return
        await self._kill_account(acc, "Удалён пользователем")

    async def _on_account_changed(self, cur: QListWidgetItem, prev: QListWidgetItem):
        if not cur: return
        uid = cur.data(Qt.UserRole)
        self.current_view_account_id = uid
        await self._load_dialogs(uid)
        # если уже открыт чат — откроем его этим аккаунтом (без лишних закреплений)
        if self.current_entity_ref:
            acc = self.accounts.get(uid)
            if acc:
                ent = await resolve_ref(acc.client, self.current_entity_ref)
                if ent:
                    await self._open_chat_with_entity(ent)
        self._rebuild_pins_bar()
        await self._reopen_comments_for_current_account()
        self._update_labels()

    async def _load_dialogs(self, uid: int):
        acc = self.accounts.get(uid)
        if not acc: return
        self.dlg_list.clear()
        try:
            async with acc.api_lock:
                async for dlg in acc.client.iter_dialogs():
                    title = human_dialog_title(dlg)
                    it = QListWidgetItem(title)
                    it.setData(Qt.UserRole, (utils.get_peer_id(dlg.entity), dlg.entity))
                    self.dlg_list.addItem(it)
        except Exception as e:
            await self._mb_crit("Диалоги", f"{e}")

    def _on_dialog_clicked(self, item: QListWidgetItem):
        info = item.data(Qt.UserRole)
        if not info: return
        _, entity = info
        asyncio.create_task(self._open_chat_with_entity(entity))

    async def _on_open_by_ref(self):
        ref = self.open_edit.text().strip()
        if not ref: return
        uid = self.current_view_account_id
        if uid is None: return await self._mb_info("Открытие", "Сначала выберите аккаунт.")
        acc = self.accounts[uid]
        try:
            if ref.startswith("@"): ref = ref[1:]
            entity = await self._run_acc(acc, acc.client.get_entity(ref))
            await self._open_chat_with_entity(entity)
        except Exception as e:
            await self._mb_crit("Открытие", f"{e}")

    async def _open_chat_with_entity(self, entity):
        if self._comments_timer.isActive():
            self._comments_timer.stop()
        self._comments_ctx_entity_ref = None
        self._comments_ctx_post_id = None
        self._comments_ctx_root_discussion_id = None
        self._comments_ctx_acc = None
        self._comments_known_ids.clear()
        self._clear_reply_keyboard()
        
        uid = self.current_view_account_id
        if uid is None: return
        acc = self.accounts[uid]
        await self._run_acc(acc, ensure_join(acc.client, entity))
        try:
            if isinstance(entity, types.User): title = utils.get_display_name(entity)
            elif isinstance(entity, (types.Chat, types.Channel)): title = entity.title
            else: title = str(utils.get_peer_id(entity))
        except Exception:
            title = "Чат"
        self.chat_title.setText(title or "Чат")
        self.current_entity_title = title or "Чат"
        self.current_entity_ref = entity_ref(entity)
        
        await self._load_messages(acc, entity)
        self._update_labels()


    def _clear_chat_area(self):
        for i in reversed(range(self.chat_v.count() - 1)):
            w = self.chat_v.itemAt(i).widget()
            if w: w.setParent(None)

    async def _maybe_resolve_and_set_author(self, client: TelegramClient, bubble: MessageBubble, msg: types.Message):
        try:
            if msg.sender:
                return
            sid = getattr(msg, "sender_id", None)
            if not sid:
                from_id = getattr(msg, "from_id", None)
                sid = utils.get_peer_id(from_id) if from_id else None
            if sid:
                ent = await client.get_entity(sid)
                bubble.set_author(friendly_display(ent))
        except Exception:
            pass

    async def _load_messages(self, acc: Account, entity, limit=60):
        self._clear_chat_area()
        self._last_loaded_messages = []
        media_to_autoload: List[types.Message] = []
        try:
            msgs = await self._run_acc(acc, acc.client.get_messages(entity, limit=limit))
            allowed = await self._run_acc(acc, get_allowed_reaction_emojis(acc.client, entity))
            self._last_loaded_messages = list(msgs)
            for m in msgs:
                outgoing = bool(m.out); can_react = True
                try:
                    r = getattr(m, "reactions", None)
                    if r and hasattr(r, "can_set") and r.can_set is False:
                        can_react = False
                except Exception:
                    pass
                bubble = MessageBubble(m, outgoing, can_react, show_reply_btn=True, emojis=allowed, parent=self.chat_inner)
                # реакция — вызываем общий обработчик (сам получит нужный peer)
                bubble.reactClicked.connect(lambda message, emoji: asyncio.create_task(self._on_react_in_chat(message, emoji)))
                bubble.commentsClicked.connect(lambda msg, a=acc, e=entity: asyncio.create_task(self._open_comments_for_post(a, e, msg)))
                
                bubble.inlineButtonClicked.connect(lambda message, info: asyncio.create_task(self._on_inline_button(message, info)))
                if bubble.media_btn:
                    bubble.media_btn.clicked.connect(lambda _, msg=m, widget=bubble: asyncio.create_task(self._load_media_into_bubble(acc, msg, widget)))
                    media_to_autoload.append(m)
                self.chat_v.insertWidget(self.chat_v.count() - 1, bubble)
                asyncio.create_task(self._maybe_resolve_and_set_author(acc.client, bubble, m))

            if self.cb_autoshow_media.isChecked():
                for m in media_to_autoload[:5]:
                    for i in range(self.chat_v.count()):
                        w = self.chat_v.itemAt(i).widget()
                        if isinstance(w, MessageBubble) and w.msg.id == m.id and w.media_btn:
                            await self._load_media_into_bubble(acc, m, w); break            # обновим панель reply-клавиатуры
            try:
                kb = None
                for _m in msgs:
                    _rm = getattr(_m, 'reply_markup', None)
                    if isinstance(_rm, types.ReplyKeyboardMarkup):
                        kb = _rm
                        break
                self._reply_kb = kb
            except Exception:
                self._reply_kb = None
            self._render_reply_keyboard(self._reply_kb)

        except Exception as e:
            await self._mb_crit('Сообщения', f'{e}')
    def _find_chat_bubble(self, msg_id: int) -> Optional[MessageBubble]:
        for i in range(self.chat_v.count()):
            w = self.chat_v.itemAt(i).widget()
            if isinstance(w, MessageBubble) and w.msg and w.msg.id == msg_id:
                return w
        return None

    def _select_main_reply_target(self, message: types.Message):
        self._main_reply_target = message
        author = "сообщение"
        try:
            if message.sender:
                author = utils.get_display_name(message.sender) or author
        except Exception:
            pass
        preview = (message.message or "").strip().replace("\n", " ")
        if len(preview) > 60: preview = preview[:57] + "…"
        self.reply_info_main.setText(f"Ответ: {author} — «{preview}»")
        self.reply_cancel_main.setVisible(True)

    def _clear_main_reply_target(self):
        self._main_reply_target = None
        self.reply_info_main.setText("")
        self.reply_cancel_main.setVisible(False)

    async def _load_media_into_bubble(self, acc: Account, msg: types.Message, bubble: MessageBubble):
        try:
            from io import BytesIO
            bio = BytesIO()
            await self._run_acc(acc, acc.client.download_media(msg, file=bio))
            if bio.getbuffer().nbytes == 0:
                return await self._mb_info("Медиа", "Нечего показать.")
            data = bio.getvalue()
            pm = QPixmap()
            if pm.loadFromData(data):
                lbl = QLabel(self.chat_inner); lbl.setPixmap(pm.scaledToWidth(460, Qt.SmoothTransformation))
                bubble.layout().insertWidget(2, lbl)
                bubble.media_btn.setVisible(False)
            else:
                if self.cb_save_unknown.isChecked():
                    tmp = ROOT / "downloads"; tmp.mkdir(exist_ok=True)
                    fname = tmp / f"{msg.id}.bin"
                    with open(fname, "wb") as f: f.write(data)
                    await self._mb_info("Медиа", f"Файл сохранён: {fname}")
                else:
                    await self._mb_info("Медиа", "Тип медиа не поддерживается для предпросмотра.")
                bubble.media_btn.setVisible(False)
        except Exception as e:
            await self._mb_crit("Медиа", f"{e}")

    # ----- Пины (глобальные) -----
    async def _on_pin_current(self):
        if not self.current_entity_ref:
            return
        if self.current_entity_ref not in self.pins:
            self.pins.append(self.current_entity_ref)
            save_pins(self.pins)
            self._rebuild_pins_bar()

    async def _on_unpin_current(self):
        if not self.current_entity_ref:
            return
        if self.current_entity_ref in self.pins:
            self.pins.remove(self.current_entity_ref)
            save_pins(self.pins)
            self._rebuild_pins_bar()

    async def _open_pinned_by_ref(self, ref: str):
        uid = self.current_view_account_id
        if uid is None or uid not in self.accounts: return
        acc = self.accounts[uid]
        try:
            ent = await self._run_acc(acc, resolve_ref(acc.client, ref))
            if not ent: return
            await self._run_acc(acc, ensure_join(acc.client, ent))
            await self._open_chat_with_entity(ent)
        except Exception as e:
            await self._mb_warn("Закреплённые", f"{e}")

    def _rebuild_pins_bar(self):
        for i in reversed(range(self.pins_row.count())):
            w = self.pins_row.itemAt(i).widget()
            if w: w.setParent(None)
        if not self.pins: return
        uid = self.current_view_account_id
        acc = self.accounts.get(uid) if uid else None

        for ref in self.pins:
            wrap = QWidget(self); h = QHBoxLayout(wrap); h.setContentsMargins(0,0,0,0)
            btn = QPushButton("…", self); btn.setCursor(Qt.PointingHandCursor)
            btn_open = QPushButton("Открыть", self); btn_open.setCursor(Qt.PointingHandCursor)

            async def _setup(b=btn, r=ref):
                title = r
                if acc:
                    try:
                        ent = await resolve_ref(acc.client, r)
                        if ent:
                            if isinstance(ent, types.User): title = utils.get_display_name(ent)
                            elif isinstance(ent, (types.Chat, types.Channel)): title = ent.title
                    except Exception:
                        pass
                b.setText(title)
                b.clicked.connect(lambda _, rr=r: asyncio.create_task(self._open_pinned_by_ref(rr)))
                btn_open.clicked.connect(lambda _, rr=r: asyncio.create_task(self._open_pinned_by_ref(rr)))
            asyncio.create_task(_setup())

            h.addWidget(btn); h.addWidget(btn_open); h.addStretch(1)
            self.pins_row.addWidget(wrap)

    # ----- Подсказка "кто отправит/кто следующий" -----
    def _peek_send_account(self) -> Optional[Account]:
        if not self.accounts: return None
        if not self.cb_auto.isChecked():
            return self.accounts.get(self.current_view_account_id)
        mode = self.mode.currentText()
        if mode == "Ручной":
            uid = self.manual_acc.currentData(Qt.UserRole)
            return self.accounts.get(uid)
        elif mode == "Рандомно":
            return None
        else:
            if not self.rr_order: return None
            uid = self.rr_order[self._rr_pointer % len(self.rr_order)]
            return self.accounts.get(uid)

    def _peek_next_after(self, current: Optional[Account]) -> Optional[Account]:
        if not self.accounts: return None
        if not self.cb_auto.isChecked():
            return current
        mode = self.mode.currentText()
        if mode == "Ручной":
            return current
        elif mode == "Рандомно":
            return None
        else:
            if not self.rr_order: return None
            if current and current.user_id in self.rr_order:
                i = self.rr_order.index(current.user_id)
                uid = self.rr_order[(i+1) % len(self.rr_order)]
                return self.accounts.get(uid)
            uid = self.rr_order[self._rr_pointer % len(self.rr_order)]
            return self.accounts.get(uid)

    def _update_labels(self):
        cur = self._peek_send_account()
        nxt = self._peek_next_after(cur)
        self.active_acc_label.setText(f"Отправляет: {self._acc_human(cur) if cur else ('случайный' if self.cb_auto.isChecked() and self.mode.currentText()=='Рандомно' else '—')}")
        self.next_acc_label.setText(f"Следующий аккаунт: {self._acc_human(nxt) if nxt else ('случайный' if self.cb_auto.isChecked() and self.mode.currentText()=='Рандомно' else '—')}")

    # ----- Выбор аккаунта -----
    async def _choose_account_for_send(self, *, advance: bool = True) -> Optional[Account]:
        if not self.accounts: return None
        if not self.cb_auto.isChecked():
            uid = self.current_view_account_id
            return self.accounts.get(uid) if uid in self.accounts else None
        mode = self.mode.currentText()
        if mode == "Ручной":
            uid = self.manual_acc.currentData(Qt.UserRole)
            return self.accounts.get(uid)
        elif mode == "Рандомно":
            uid = random.choice(list(self.accounts.keys()))
            return self.accounts[uid]
        else:
            if not self.rr_order: return None
            uid = self.rr_order[self._rr_pointer % len(self.rr_order)]
            acc = self.accounts[uid]
            if advance:
                self._rr_pointer = (self._rr_pointer + 1) % len(self.rr_order)
            return acc

    def _choose_account_for_reaction(self) -> Optional[Account]:
        """Для реакций: поочерёдно/рандом/ручной; если авто-режим выключен — тоже используем rr-очередь."""
        if not self.accounts: return None
        if self.cb_auto.isChecked():
            mode = self.mode.currentText()
            if mode == "Ручной":
                uid = self.manual_acc.currentData(Qt.UserRole)
                return self.accounts.get(uid)
            elif mode == "Рандомно":
                uid = random.choice(list(self.accounts.keys()))
                return self.accounts[uid]
            else:
                uid = self.rr_order[self._rr_pointer % len(self.rr_order)]
                return self.accounts.get(uid)
        else:
            if self.rr_order:
                uid = self.rr_order[self._rr_pointer % len(self.rr_order)]
                return self.accounts.get(uid)
            return self.accounts.get(self.current_view_account_id)

    # ----- InputPeer cache -----
    async def _get_input_peer(self, acc: Account, ref: str):
        key = (acc.user_id, ref)
        if key in self._peer_cache:
            return self._peer_cache[key]
        ent = await resolve_ref(acc.client, ref)
        if not ent: return None
        await ensure_join(acc.client, ent)
        ip = await acc.client.get_input_entity(ent)
        self._peer_cache[key] = ip
        return ip

    # ----- Отправка обычных сообщений -----
    _pending_file_path: Optional[str] = None

    def _set_pending_main_file(self, path: Optional[str]):
        self._pending_file_path = path
        if path:
            self.pending_main_info.setText(f"Прикреплено: {Path(path).name}")
            self.pending_main_info.setVisible(True)
            self.btn_pending_clear.setVisible(True)
        else:
            self.pending_main_info.setVisible(False)
            self.btn_pending_clear.setVisible(False)

    def _clear_pending_main_file(self):
        self._set_pending_main_file(None)

    async def _on_send(self):
        text = self.input.text().strip()
        if not text and not self._pending_file_path: return
        if not self.current_entity_ref:
            return await self._mb_info("Отправка", "Откройте чат.")
        chosen_acc = await self._choose_account_for_send(advance=True)
        if not chosen_acc:
            return await self._mb_info("Отправка", "Нет доступного аккаунта.")
        self._update_labels()

        ip = await self._get_input_peer(chosen_acc, self.current_entity_ref)
        if not ip:
            return await self._mb_warn("Отправка", "Выбранный аккаунт не имеет доступа к этому чату.")
        try:
            reply_to_id = self._main_reply_target.id if self._main_reply_target else None
            if self._pending_file_path:
                path = self._pending_file_path
                await self._run_acc(chosen_acc, chosen_acc.client.send_file(ip, path, caption=text or None, reply_to=reply_to_id))
                self._set_pending_main_file(None)
            else:
                await self._run_acc(chosen_acc, chosen_acc.client.send_message(ip, text, reply_to=reply_to_id))
            self.input.clear(); self._clear_main_reply_target()
            view_uid = self.current_view_account_id
            if view_uid is not None:
                view_acc = self.accounts.get(view_uid)
                if view_acc:
                    ent = await resolve_ref(view_acc.client, self.current_entity_ref)
                    if ent:
                        await self._load_messages(view_acc, ent)
            self._update_labels()
        except (PeerFloodError, FloodWaitError) as e:
            await self._mb_warn("Лимит", f"Временный лимит: {e}")
        except Exception as e:
            if self._is_frozen_error(e):
                await self._kill_account(chosen_acc, "Аккаунт заморожен (read-only)")
            else:
                await self._mb_crit("Отправка", f"{e}")

    async def _on_attach(self):
        fn, _ = QFileDialog.getOpenFileName(self, "Выбрать файл (картинка/медиа)", "",
                                            "Изображения/медиа (*.png *.jpg *.jpeg *.gif *.webp *.mp4 *.mov *.webm);;Все файлы (*.*)")
        if fn:
            self._set_pending_main_file(fn)
    # ----- Reply Keyboard (основное меню) -----
    def _clear_reply_keyboard(self):
        try:
            def _clear_layout(layout):
                if layout is None:
                    return
                while layout.count():
                    item = layout.takeAt(0)
                    w = item.widget()
                    lay = item.layout()
                    if w is not None:
                        w.setParent(None)
                    elif lay is not None:
                        _clear_layout(lay)
                        lay.setParent(None)
        except Exception:
            pass
        self.reply_kb_panel.setVisible(False)
        self._reply_kb_sig = None
    
    def _render_reply_keyboard(self, kb):
        # сигнатура для анти-дубликатов
        sig = None
        try:
            if kb:
                rows = []
                for row in getattr(kb, "rows", []) or []:
                    texts = []
                    for b in getattr(row, "buttons", []) or []:
                        texts.append(getattr(b, "text", "") or "")
                    rows.append(tuple(texts))
                sig = tuple(rows)
        except Exception:
            sig = None
    
        if sig is not None and sig == getattr(self, "_reply_kb_sig", None):
            return
    
        self._clear_reply_keyboard()
        if not kb:
            return
        try:
            for row in getattr(kb, "rows", []) or []:
                h = QHBoxLayout()
                for b in getattr(row, "buttons", []) or []:
                    text = getattr(b, "text", "") or "…"
                    btn = QPushButton(text, self)
                    btn.setCursor(Qt.PointingHandCursor)
                    btn.setStyleSheet("QPushButton { background:#1a2440; border:1px solid #2b3a66; border-radius:10px; padding:6px 10px; }")
                    btn.clicked.connect(lambda _, t=text: asyncio.create_task(self._on_reply_kb_click({'text': t})))
                    h.addWidget(btn)
                h.addStretch(1)
                self.reply_kb_v.addLayout(h)
            self.reply_kb_panel.setVisible(True)
            self._reply_kb_sig = sig
        except Exception:
            self.reply_kb_panel.setVisible(False)
            self._reply_kb_sig = None
    
    async def _on_reply_kb_click(self, info: dict):
        t = (info or {}).get("text", "")
        if not t:
            await self._mb_info("Клавиатура", "Кнопка без текста пока не поддерживается.")
            return
        if not self.current_entity_ref:
            await self._mb_info("Клавиатура", "Откройте чат.")
            return
        acc = await self._choose_account_for_send(advance=True)
        if not acc:
            await self._mb_warn("Клавиатура", "Нет доступного аккаунта.")
            return
        try:
            ip = await self._get_input_peer(acc, self.current_entity_ref)
            if not ip:
                return await self._mb_warn("Клавиатура", "Нет доступа к чату.")
            await self._run_acc(acc, acc.client.send_message(ip, t))
            # перезагрузка чата (клавиатура могла измениться)
            view_uid = self.current_view_account_id
            if view_uid is not None:
                view_acc = self.accounts.get(view_uid)
                if view_acc:
                    ent = await resolve_ref(view_acc.client, self.current_entity_ref)
                    if ent:
                        await self._load_messages(view_acc, ent)
        except Exception as e:
            await self._mb_warn("Клавиатура", f"{e}")
    
            # ----- Реакции -----
    def _react_key(self, ref: str, msg_id: int, uid: int) -> str:
        return f"{ref}|{msg_id}|{uid}"

    async def _on_react_in_chat(self, msg: types.Message, emoji: str):
        if not emoji or not self.current_entity_ref:
            return
        chosen = self._choose_account_for_reaction()
        if not chosen:
            return await self._mb_warn("Реакции", "Нет доступного аккаунта.")
        key = self._react_key(self.current_entity_ref, msg.id, chosen.user_id)
        if self._react_mem.get(key) == emoji:
            return await self._mb_info("Реакции", "Этот аккаунт уже ставил такую реакцию на этот пост.")
        try:
            ip = await self._get_input_peer(chosen, self.current_entity_ref)
            if not ip:
                return await self._mb_warn("Реакции", "Нет доступа к чату у выбранного аккаунта.")
            await self._run_acc(chosen, chosen.client(SendReactionRequest(
                peer=ip, msg_id=msg.id,
                reaction=[types.ReactionEmoji(emoticon=emoji)],
                add_to_recent=True
            )))
            b = self._find_chat_bubble(msg.id)
            if b:
                b.apply_reaction(emoji, +1)
            self._react_mem[key] = emoji
            save_json(REACTIONS_CACHE_FILE, self._react_mem)
        except (PeerFloodError, FloodWaitError) as e:
            await self._mb_warn("Реакции", f"Лимит: {e}")
        except Exception as e:
            await self._mb_warn("Реакции", f"{e}")
        finally:
            if self.cb_switch_after_reaction.isChecked() and self.rr_order:
                self._rr_pointer = (self._rr_pointer + 1) % len(self.rr_order)
            self._update_labels()

    async def _on_inline_button(self, msg: types.Message, info: dict):
        """Обработка нажатия inline-кнопки под сообщением."""
        kind = (info or {}).get("kind")

        if kind == "url":
            try:
                QDesktopServices.openUrl(QUrl((info or {}).get("url", "")))
            except Exception:
                pass
            return

        if not self.current_entity_ref:
            await self._mb_info("Кнопки", "Откройте чат.")
            return

        # Берём текущий аккаунт (без продвижения очереди)
        acc = await self._choose_account_for_send(advance=False) or self.accounts.get(self.current_view_account_id)
        if not acc:
            await self._mb_warn("Кнопки", "Нет доступного аккаунта.")
            return

        try:
            ip = await self._get_input_peer(acc, self.current_entity_ref)
            if not ip:
                return await self._mb_warn("Кнопки", "Нет доступа к чату у выбранного аккаунта.")

            if kind == "callback":
                data = (info or {}).get("data", None)
                await self._run_acc(acc, acc.client(GetBotCallbackAnswerRequest(peer=ip, msg_id=msg.id, data=data)))
            elif kind == "switch_inline":
                await self._mb_info("Кнопки", "Кнопка 'Switch Inline' пока не поддерживается. Введите запрос вручную через @бот.")
            else:
                await self._mb_info("Кнопки", "Этот тип кнопки пока не поддержан.")
        except Exception as e:
            await self._mb_warn("Кнопки", f"{e}")

    # ----- Комментарии / обсуждения -----
    async def _get_discussion_chat(self, client: TelegramClient, channel_entity):
        try:
            full = await client(GetFullChannelRequest(channel_entity))
            linked_id = getattr(full.full_chat, 'linked_chat_id', None)
            if not linked_id: return None
            try:
                return await client.get_entity(types.PeerChannel(linked_id))
            except Exception:
                return await client.get_entity(linked_id)
        except Exception:
            return None

    async def _fetch_comments_via_getreplies(self, acc: Account, channel, post_id: int, limit=400):
        try:
            res = await self._run_acc(acc, acc.client(GetRepliesRequest(
                peer=channel, msg_id=post_id,
                offset_id=0, offset_date=None, add_offset=0, limit=limit,
                max_id=0, min_id=0, hash=0
            )))
            msgs = list(getattr(res, "messages", []))
            msgs.sort(key=lambda m: (m.date or 0), reverse=True)
            return msgs
        except Exception:
            return []

    async def _iter_comments_for_post(self, acc: Account, channel_entity, channel_msg_id, limit=400):
        discussion = await self._get_discussion_chat(acc.client, channel_entity)
        if not discussion:
            return None, []
        await self._run_acc(acc, ensure_join(acc.client, discussion))

        comments = await self._fetch_comments_via_getreplies(acc, channel_entity, channel_msg_id, limit=limit)
        if comments:
            return discussion, comments

        fallback: List[types.Message] = []
        async with acc.api_lock:
            async for m in acc.client.iter_messages(discussion, limit=limit):
                rt = getattr(m, "reply_to", None)
                top_id = getattr(rt, "reply_to_top_id", None) if rt else None
                mid = getattr(rt, "reply_to_msg_id", None) if rt else None
                if top_id == channel_msg_id or mid == channel_msg_id:
                    fallback.append(m)
        fallback.sort(key=lambda m: (m.date or 0), reverse=True)
        return discussion, fallback

    async def _get_discussion_root_id(self, acc: Account, channel_entity, post_id: int, discussion) -> Optional[int]:
        try:
            res = await self._run_acc(acc, acc.client(GetDiscussionMessageRequest(
                peer=channel_entity, msg_id=int(post_id)
            )))
            msgs = getattr(res, "messages", []) or []
            for m in msgs:
                pid = getattr(m, "peer_id", None)
                if isinstance(pid, types.PeerChannel):
                    if isinstance(discussion, types.Channel) and pid.channel_id == discussion.id:
                        return m.id
            if msgs and isinstance(getattr(msgs[0], "peer_id", None), types.PeerChannel):
                return msgs[0].id
        except Exception:
            pass
        return None

    async def _open_comments_for_post(self, acc: Account, entity, msg: types.Message):
        real_post_id = msg.id
        self.comments.clear_comments(f"Комментарии к посту {real_post_id} • {self.current_entity_title}")
        try:
            discussion = await self._get_discussion_chat(acc.client, entity)
            if not discussion:
                return await self._mb_info("Комментарии", "Комментарии недоступны для этого поста.")
            await self._run_acc(acc, ensure_join(acc.client, discussion))

            root_id = await self._get_discussion_root_id(acc, entity, real_post_id, discussion)

            _, comments = await self._iter_comments_for_post(acc, entity, real_post_id, limit=500)
            allowed = await self._run_acc(acc, get_allowed_reaction_emojis(acc.client, discussion))
            self._comments_known_ids = set()
            for cm in comments:
                bub = self.comments.add_comment_bubble(cm, bool(cm.out), emojis=allowed)
                self._comments_known_ids.add(cm.id)
                asyncio.create_task(self._maybe_resolve_and_set_author(acc.client, bub, cm))

            self._comments_ctx_entity_ref = entity_ref(entity)
            self._comments_ctx_post_id = real_post_id
            self._comments_ctx_root_discussion_id = root_id
            self._comments_ctx_acc = acc
            if not self._comments_timer.isActive():
                self._comments_timer.start()
        except Exception as e:
            if self._is_frozen_error(e):
                await self._kill_account(acc, "Аккаунт заморожен (read-only)")
            else:
                await self._mb_crit("Комментарии", f"{e}")

    async def _reopen_comments_for_current_account(self):
        if not (self._comments_ctx_entity_ref and self._comments_ctx_post_id):
            return
        uid = self.current_view_account_id
        if uid is None or uid not in self.accounts:
            return
        acc = self.accounts[uid]
        try:
            channel_entity = await self._run_acc(acc, resolve_ref(acc.client, self._comments_ctx_entity_ref))
            if not channel_entity:
                return
            discussion = await self._get_discussion_chat(acc.client, channel_entity)
            if not discussion:
                return
            await self._run_acc(acc, ensure_join(acc.client, discussion))

            self._comments_ctx_root_discussion_id = await self._get_discussion_root_id(
                acc, channel_entity, int(self._comments_ctx_post_id), discussion
            )

            self.comments.clear_comments(f"Комментарии к посту {self._comments_ctx_post_id} • {self.current_entity_title}")

            _, comments = await self._iter_comments_for_post(acc, channel_entity, int(self._comments_ctx_post_id), limit=500)
            allowed = await self._run_acc(acc, get_allowed_reaction_emojis(acc.client, discussion))
            self._comments_known_ids = set()
            for cm in comments:
                bub = self.comments.add_comment_bubble(cm, bool(cm.out), emojis=allowed)
                self._comments_known_ids.add(cm.id)
                asyncio.create_task(self._maybe_resolve_and_set_author(acc.client, bub, cm))
            if not self._comments_timer.isActive():
                self._comments_timer.start()
        except Exception:
            pass

    async def _refresh_comments_tick(self):
        if not (self._comments_ctx_entity_ref and self._comments_ctx_post_id and self._comments_ctx_acc):
            return
        acc = self._comments_ctx_acc
        try:
            channel_entity = await self._run_acc(acc, resolve_ref(acc.client, self._comments_ctx_entity_ref))
            if not channel_entity:
                return
            discussion = await self._get_discussion_chat(acc.client, channel_entity)
            if not discussion:
                return
            latest = await self._fetch_comments_via_getreplies(acc, channel_entity, int(self._comments_ctx_post_id), limit=60)
            if not latest:
                return
            allowed = await self._run_acc(acc, get_allowed_reaction_emojis(acc.client, discussion))
            for cm in latest:
                if cm.id not in self._comments_known_ids:
                    bub = self.comments.add_comment_bubble_top(cm, bool(cm.out), emojis=allowed)
                    self._comments_known_ids.add(cm.id)
                    asyncio.create_task(self._maybe_resolve_and_set_author(acc.client, bub, cm))
        except Exception:
            pass

    async def _on_send_comment(self, text: str):
        if not (self._comments_ctx_entity_ref and self._comments_ctx_post_id):
            return await self._mb_info("Комментарии", "Откройте ветку комментариев (кнопка «Комментарии» под постом).")

        chosen_acc = await self._choose_account_for_send(advance=True)
        if not chosen_acc:
            return await self._mb_warn("Комментарии", "Нет доступного аккаунта.")

        self._update_labels()

        try:
            channel_entity = await self._run_acc(
                chosen_acc, resolve_ref(chosen_acc.client, self._comments_ctx_entity_ref)
            )
            if not channel_entity:
                return await self._mb_warn("Комментарии", "Не удалось получить канал.")

            discussion = await self._get_discussion_chat(chosen_acc.client, channel_entity)
            if not discussion:
                return await self._mb_warn("Комментарии", "У поста нет доступной ветки комментариев.")
            await self._run_acc(chosen_acc, ensure_join(chosen_acc.client, discussion))

            post_id = int(self._comments_ctx_post_id)
            root_id = self._comments_ctx_root_discussion_id
            if not root_id:
                root_id = await self._get_discussion_root_id(chosen_acc, channel_entity, post_id, discussion)
                self._comments_ctx_root_discussion_id = root_id
            if not root_id:
                return await self._mb_warn("Комментарии", "Не удалось определить корень обсуждения. Откройте ветку заново.")

            reply_target = self.comments.current_reply_target()
            reply_to_id = int(reply_target.id) if reply_target else int(root_id)

            sent = None
            if self._comments_pending_file_path:
                path = Path(self._comments_pending_file_path)
                self._comments_pending_file_path = None
                sent = await self._run_acc(
                    chosen_acc,
                    chosen_acc.client.send_file(
                        discussion, str(path),
                        caption=text or "",
                        reply_to=reply_to_id
                    )
                )
                if isinstance(sent, list):
                    sent = sent[0] if sent else None
            else:
                sent = await self._run_acc(
                    chosen_acc,
                    chosen_acc.client.send_message(
                        discussion, text or "",
                        reply_to=reply_to_id
                    )
                )

            if isinstance(sent, types.Message):
                allowed = await self._run_acc(chosen_acc, get_allowed_reaction_emojis(chosen_acc.client, discussion))
                bub = self.comments.add_comment_bubble_top(sent, True, emojis=allowed)
                self._comments_known_ids.add(sent.id)
                asyncio.create_task(self._maybe_resolve_and_set_author(chosen_acc.client, bub, sent))

            self.comments.clear_reply_indicator()
            self._update_labels()

        except Exception as e:
            if self._is_frozen_error(e):
                await self._kill_account(chosen_acc, "Аккаунт заморожен (read-only)")
            else:
                await self._mb_crit("Комментарии", f"{e}")

    async def _on_attach_comment(self):
        if not (self._comments_ctx_entity_ref and self._comments_ctx_post_id):
            return await self._mb_info("Прикрепление", "Откройте ветку комментариев.")
        fn, _ = QFileDialog.getOpenFileName(self, "Файл к комментарию", "", "Изображения/медиа (*.png *.jpg *.jpeg *.gif *.webp *.mp4 *.mov *.webm);;Все файлы (*.*)")
        if not fn: return
        self._comments_pending_file_path = fn
        # не отправляем сразу — дождёмся Enter/кнопки

    async def _on_react_in_comment(self, cm: types.Message, emoji: str):
        if not (self._comments_ctx_entity_ref and self._comments_ctx_post_id) or not emoji:
            return
        chosen = self._choose_account_for_reaction()
        if not chosen:
            return await self._mb_warn("Реакции", "Нет доступного аккаунта.")
        key = self._react_key(self._comments_ctx_entity_ref or "", cm.id, chosen.user_id)
        if self._react_mem.get(key) == emoji:
            return await self._mb_info("Реакции", "Этот аккаунт уже ставил такую реакцию на этот комментарий.")
        try:
            channel_entity = await self._run_acc(chosen, resolve_ref(chosen.client, self._comments_ctx_entity_ref))
            discussion = await self._get_discussion_chat(chosen.client, channel_entity)
            if not discussion:
                return
                await self._run_acc(chosen, ensure_join(chosen.client, discussion))
            ip = await chosen.client.get_input_entity(discussion)
            await self._run_acc(chosen,
                chosen.client(SendReactionRequest(
                    peer=ip, msg_id=cm.id,
                    reaction=[types.ReactionEmoji(emoticon=emoji)], add_to_recent=True
                ))
            )
            self.comments.update_comment_reaction(cm.id, emoji, +1)
            self._react_mem[key] = emoji
            save_json(REACTIONS_CACHE_FILE, self._react_mem)
        except Exception as e:
            await self._mb_warn("Реакции", f"{e}")
        finally:
            if self.cb_switch_after_reaction.isChecked() and self.rr_order:
                self._rr_pointer = (self._rr_pointer + 1) % len(self.rr_order)
            self._update_labels()

    # ===== ПРОКСИ =====
    async def _on_load_proxies(self):
        if socks is None:
            return await self._mb_warn("Прокси", "Не установлен модуль PySocks. Установите: pip install PySocks")
        fn, _ = QFileDialog.getOpenFileName(self, "Выберите файл прокси (по одному на строку)", "", "Текстовые файлы (*.txt);;Все файлы (*.*)")
        if not fn: return
        try:
            with open(fn, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()
            pool: List[dict] = []
            for ln in lines:
                p = _parse_proxy_line(ln)
                if p: pool.append(p)
            if not pool:
                return await self._mb_warn("Прокси", "Не удалось распознать ни один прокси.")
            cfg = _load_proxies_config()
            cfg["pool"] = pool

            abys = {k:v for k,v in cfg.get("assignments_by_session", {}).items()
                    if isinstance(v,int) and 0 <= v < len(pool)}
            abyu = {k:v for k,v in cfg.get("assignments_by_user", {}).items()
                    if isinstance(v,int) and 0 <= v < len(pool)}
            cfg["assignments_by_session"] = abys
            cfg["assignments_by_user"] = abyu

            files = sorted(SESS_DIR.glob("*.session"))
            used = set(abys.keys())
            idx_cycle = 0
            for f in files:
                if f.name in used: continue
                cfg["assignments_by_session"][f.name] = idx_cycle % len(pool)
                idx_cycle += 1

            _save_proxies_config(cfg)
            self.proxies_cfg = cfg

            await self._mb_info("Прокси", f"Загружено прокси: {len(pool)}. Переподключаем сессии через привязанные прокси.")
            await self._auto_load_sessions(force=True)

        except Exception as e:
            await self._mb_crit("Прокси", f"{e}")

    # ----- Clipboard helpers -----
    def _save_qimage_temp(self, img: QImage) -> Optional[str]:
        try:
            tmpdir = Path(tempfile.gettempdir())
            fn = tmpdir / f"tg_clip_{uuid.uuid4().hex}.png"
            img.save(str(fn), "PNG")
            return str(fn)
        except Exception as e:
            print(f"[CLIPBOARD] Ошибка сохранения картинки: {e}")
            return None

    def _extract_image_url_from_clipboard(self) -> Optional[str]:
        md = QGuiApplication.clipboard().mimeData()
        # текст
        if md and md.hasText():
            t = md.text().strip()
            if re.match(r"^https?://", t, flags=re.I):
                low = t.lower()
                if any(low.endswith(ext) for ext in self._IMG_EXT):
                    return t
        # url-объекты
        if md and md.hasUrls():
            for u in md.urls():
                s = u.toString()
                low = s.lower()
                if re.match(r"^https?://", s, flags=re.I) and any(low.endswith(ext) for ext in self._IMG_EXT):
                    return s
        return None

    def _download_image_to_temp(self, url: str) -> Optional[str]:
        try:
            r = requests.get(url, timeout=12)
            r.raise_for_status()
            tmpdir = Path(tempfile.gettempdir())
            ext = ".jpg"
            for e in self._IMG_EXT:
                if url.lower().endswith(e):
                    ext = e; break
            fn = tmpdir / f"tg_url_{uuid.uuid4().hex}{ext}"
            with open(fn, "wb") as f:
                f.write(r.content)
            return str(fn)
        except Exception as e:
            print("IMG URL download error:", e)
            return None

    # перехватываем Ctrl+V, чтобы не вставлять ссылку в поле, а прикреплять файл
    def eventFilter(self, obj, event):
        if event.type() == QEvent.KeyPress and (event.matches(QKeySequence.Paste)):
            if obj is self.input:
                md = QGuiApplication.clipboard().mimeData()
                if md and md.hasImage():
                    img = QGuiApplication.clipboard().image()
                    if not img.isNull():
                        fn = self._save_qimage_temp(img)
                        if fn:
                            self._set_pending_main_file(fn)
                            return True  # не вставлять текст
                url = self._extract_image_url_from_clipboard()
                if url:
                    fn = self._download_image_to_temp(url)
                    if fn:
                        self._set_pending_main_file(fn)
                        return True
            elif obj is self.comments.input:
                md = QGuiApplication.clipboard().mimeData()
                if md and md.hasImage():
                    img = QGuiApplication.clipboard().image()
                    if not img.isNull():
                        fn = self._save_qimage_temp(img)
                        if fn:
                            self._comments_pending_file_path = fn
                            return True
                url = self._extract_image_url_from_clipboard()
                if url:
                    fn = self._download_image_to_temp(url)
                    if fn:
                        self._comments_pending_file_path = fn
                        return True
        return super().eventFilter(obj, event)

# -----------------------------
# Точка входа
# -----------------------------
async def amain():
    app = QApplication(sys.argv)
    force_dark_palette(app)
    loop = QEventLoop(app)
    asyncio.set_event_loop(loop)

    win = MainWindow(loop)
    win.show()
    install_sticker_plugin(MainWindow)

    try:
        await loop.run_forever()
    except asyncio.CancelledError:
        pass

if __name__ == "__main__":
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        pass










