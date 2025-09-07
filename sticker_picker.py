# -*- coding: utf-8 -*-
# sticker_picker.py — плагин выбора/отправки стикеров Telegram для PySide6+Telethon
#
# Подключение из основного файла (после определения класса MainWindow):
#   from sticker_picker import install_sticker_plugin
#   install_sticker_plugin(MainWindow)
#
# Хранение наборов (sticker_sets.json) — в профиле пользователя:
#   Windows: %APPDATA%\TelegramMulti\sticker_sets.json
#   Linux/macOS: ~/.telegram_multi/sticker_sets.json

from __future__ import annotations

import asyncio
import importlib
import io
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from PySide6.QtCore import Qt, Signal, QUrl, QSize
from PySide6.QtGui import QPixmap, QAction, QImage, QIcon
from PySide6.QtWidgets import (
    QWidget, QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QListWidget, QListWidgetItem, QScrollArea, QGridLayout, QMessageBox,
    QToolButton, QMenu
)

# ---- Медиа-превью (опционально) ----
try:
    from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
    from PySide6.QtMultimediaWidgets import QVideoWidget
    _HAS_QTMULTI = True
except Exception:
    _HAS_QTMULTI = False

# ---- rlottie для .tgs превью (опционально) ----
try:
    import rlottie
    _HAS_RLOTTIE = True
except Exception:
    _HAS_RLOTTIE = False

from telethon import types, functions

# ==============================
#     ХРАНЕНИЕ НАБОРОВ
# ==============================
# <--- ВАЖНО: изменён путь хранения под профиль пользователя --->
APP_DIR = Path(os.getenv("APPDATA", str(Path.home() / ".telegram_multi"))) / "TelegramMulti"
APP_DIR.mkdir(parents=True, exist_ok=True)
PACKS_FILE = APP_DIR / "sticker_sets.json"

def _payload() -> dict:
    if PACKS_FILE.exists():
        try:
            return json.load(open(PACKS_FILE, "r", encoding="utf-8"))
        except Exception:
            pass
    return {"packs": [], "last_short": None}

def _save_payload(d: dict):
    with open(PACKS_FILE, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)

def _load_packs() -> List[dict]:
    d = _payload()
    packs = d.get("packs", [])
    # миграция старого формата ["short", ...] -> [{"short_name":..., "title":...}]
    if isinstance(packs, list) and packs and isinstance(packs[0], str):
        packs = [{"short_name": s, "title": s} for s in packs]
    return packs

def _save_packs(packs: List[dict], *, last_short: Optional[str] = None):
    d = _payload()
    d["packs"] = packs
    if last_short is not None:
        d["last_short"] = last_short
    _save_payload(d)

def _load_last() -> Optional[str]:
    return _payload().get("last_short")

# ==============================
#     УТИЛИТЫ
# ==============================
_THUMB_CACHE: Dict[int, QPixmap] = {}  # document.id -> QPixmap
_WEBM_CACHE: Dict[int, Path] = {}      # document.id -> Path

_PACK_RX = re.compile(r"(?i)(?:https?://t\.me/addstickers/|tg://addstickers\?set=)?([A-Za-z0-9_]{3,64})")

def extract_shortname(s: str) -> Optional[str]:
    if not s:
        return None
    m = _PACK_RX.search(s.strip())
    return m.group(1) if m else None

def _emoji_for_doc(d: types.Document) -> str:
    try:
        for a in d.attributes or []:
            if isinstance(a, types.DocumentAttributeSticker):
                return a.alt or ""
    except Exception:
        pass
    return ""

async def _tgs_to_pixmap(client, doc, lock) -> Optional[QPixmap]:
    if not _HAS_RLOTTIE:
        return None
    try:
        bio = io.BytesIO()
        if lock:
            async with lock:
                await client.download_media(doc, file=bio)
        else:
            await client.download_media(doc, file=bio)
        data = bio.getvalue()
        anim = rlottie.Animation.from_tgs_bytes(data)
        w, h = anim.size()
        scale = min(96 / max(w, h), 1.0)
        W, H = max(32, int(w * scale)), max(32, int(h * scale))
        buf = bytearray(W * H * 4)
        anim.render(0, buf, W, H, W * 4)
        img = QImage(bytes(buf), W, H, W * 4, QImage.Format_RGBA8888)
        return QPixmap.fromImage(img)
    except Exception:
        return None

async def _thumb_pm(client, doc: types.Document, lock: Optional[asyncio.Lock], sem: asyncio.Semaphore) -> Optional[QPixmap]:
    if doc.id in _THUMB_CACHE:
        return _THUMB_CACHE[doc.id]

    mime = (getattr(doc, "mime_type", "") or "").lower()
    if ("tgs" in mime or "application/x-tgsticker" in mime) and _HAS_RLOTTIE:
        pm = await _tgs_to_pixmap(client, doc, lock)
        if pm:
            pm = pm.scaled(72, 72, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            _THUMB_CACHE[doc.id] = pm
            return pm

    async with sem:
        try:
            bio = io.BytesIO()
            thumb = None
            if getattr(doc, "thumbs", None):
                thumb = min(doc.thumbs, key=lambda t: getattr(t, "w", 512) * getattr(t, "h", 512))
            if lock:
                async with lock:
                    await client.download_media(doc, file=bio, thumb=thumb)
            else:
                await client.download_media(doc, file=bio, thumb=thumb)
            pm = QPixmap()
            if pm.loadFromData(bio.getvalue()):
                pm = pm.scaled(72, 72, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                _THUMB_CACHE[doc.id] = pm
                return pm
        except Exception:
            pass
    return None

# ==============================
#     КОНТРОЛЫ
# ==============================
@dataclass
class _DocWrap:
    doc: types.Document
    emoji: str

class StickerButton(QToolButton):
    clickedWithDoc = Signal(object)  # _DocWrap
    def __init__(self, wrap: _DocWrap, pm: Optional[QPixmap] = None, parent=None):
        super().__init__(parent)
        self.wrap = wrap
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(92, 92)
        self.setStyleSheet(
            "QToolButton { background:#121a2b; border:1px solid #273356; border-radius:12px; }"
            "QToolButton:hover { background:#1a2440; }"
        )
        self.setToolTip(wrap.emoji or "")
        if pm and not pm.isNull():
            self.setIcon(QIcon(pm))
            self.setIconSize(QSize(72, 72))
        else:
            self.setText("🌀")
    def mouseReleaseEvent(self, e):
        super().mouseReleaseEvent(e)
        if e.button() == Qt.LeftButton:
            self.clickedWithDoc.emit(self.wrap)

class _VideoTile(QWidget):
    clicked = Signal(object)  # _DocWrap
    def __init__(self, wrap: _DocWrap, path: Path, parent=None):
        super().__init__(parent)
        self.wrap = wrap
        self.setFixedSize(92, 92)
        lay = QVBoxLayout(self); lay.setContentsMargins(0, 0, 0, 0); lay.setSpacing(0)
        if not _HAS_QTMULTI:
            lbl = QLabel("▶", self); lbl.setAlignment(Qt.AlignCenter); lay.addWidget(lbl); return
        vw = QVideoWidget(self); vw.setFixedSize(92, 92)
        self.player = QMediaPlayer(self); self.player.setVideoOutput(vw)
        try:
            ao = QAudioOutput(self); ao.setVolume(0.0); self.player.setAudioOutput(ao)
        except Exception:
            pass
        self.player.setSource(QUrl.fromLocalFile(str(path)))
        self.player.mediaStatusChanged.connect(self._loop)
        lay.addWidget(vw); self.player.play()
    def _loop(self, st):
        try:
            from PySide6.QtMultimedia import QMediaPlayer as _MP
            if hasattr(_MP, "EndOfMedia") and st == _MP.EndOfMedia:
                self.player.setPosition(0); self.player.play()
        except Exception:
            pass
    def mouseReleaseEvent(self, e):
        super().mouseReleaseEvent(e)
        if e.button() == Qt.LeftButton:
            self.clicked.emit(self.wrap)

# ==============================
#     ПОЛКА В ЧАТЕ
# ==============================
class StickerShelf(QWidget):
    picked = Signal(object)  # types.Document
    def __init__(self, client, lock, title, parent=None):
        super().__init__(parent)
        self.client = client
        self.lock = lock
        self._packs = _load_packs()
        self._sema = asyncio.Semaphore(4)
        self.setObjectName("StickerShelf")
        self.setStyleSheet("#StickerShelf {background:#0e1629; border:1px solid #273356; border-radius:10px;}")
        v = QVBoxLayout(self); v.setContentsMargins(8,8,8,8); v.setSpacing(6)

        top = QHBoxLayout(); v.addLayout(top)
        top.addWidget(QLabel(title, self))
        self.pack_box = QListWidget(self); self.pack_box.setFixedHeight(80)
        for p in self._packs:
            it = QListWidgetItem(p.get("title") or p["short_name"]); it.setData(Qt.UserRole, p["short_name"])
            self.pack_box.addItem(it)
        self.pack_box.itemClicked.connect(lambda it: asyncio.create_task(self.load_pack(it.data(Qt.UserRole))))
        top.addWidget(self.pack_box, 1)

        self.add_edit = QLineEdit(self); self.add_edit.setPlaceholderText("https://t.me/addstickers/<shortname>")
        b_add = QPushButton("+", self); b_add.setFixedWidth(32); b_add.clicked.connect(lambda: asyncio.create_task(self._on_add()))
        top.addWidget(self.add_edit); top.addWidget(b_add)

        self.scroll = QScrollArea(self); self.scroll.setWidgetResizable(True); v.addWidget(self.scroll)
        self.host = QWidget(self.scroll); self.h = QHBoxLayout(self.host); self.h.setContentsMargins(0,0,0,0); self.h.setSpacing(6)
        self.scroll.setWidget(self.host)

        last = _load_last()
        if last:
            for i in range(self.pack_box.count()):
                if self.pack_box.item(i).data(Qt.UserRole) == last:
                    self.pack_box.setCurrentRow(i); break
            asyncio.create_task(self.load_pack(last))

    def set_client(self, client, lock):
        self.client = client; self.lock = lock
        it = self.pack_box.currentItem()
        cur = it.data(Qt.UserRole) if it else None
        asyncio.create_task(self.load_pack(cur or _load_last() or (self._packs[0]["short_name"] if self._packs else None)))

    async def _on_add(self):
        sn = extract_shortname(self.add_edit.text())
        if not sn:
            return QMessageBox.information(self, "Набор", "Укажи ссылку или shortname.")
        ok, title = await self._probe(sn)
        if not ok:
            return QMessageBox.warning(self, "Набор", "Не удалось получить набор.")
        if not any(p["short_name"] == sn for p in self._packs):
            self._packs.append({"short_name": sn, "title": title or sn})
            _save_packs(self._packs, last_short=sn)
            it = QListWidgetItem(title or sn); it.setData(Qt.UserRole, sn)
            self.pack_box.addItem(it); self.pack_box.setCurrentItem(it)
        self.add_edit.clear()
        await self.load_pack(sn)

    async def _probe(self, short):
        try:
            res = await self.client(functions.messages.GetStickerSetRequest(
                stickerset=types.InputStickerSetShortName(short_name=short), hash=0
            ))
            title = getattr(getattr(res, "set", None), "title", None)
            return True, title
        except Exception:
            return False, None

    async def load_pack(self, short: Optional[str]):
        while self.h.count():
            w = self.h.takeAt(0).widget()
            if w: w.setParent(None)
        if not short:
            return
        _save_packs(self._packs, last_short=short)
        try:
            res = await self.client(functions.messages.GetStickerSetRequest(
                stickerset=types.InputStickerSetShortName(short_name=short), hash=0
            ))
        except Exception as e:
            return QMessageBox.critical(self, "Набор", f"Ошибка загрузки: {e}")
        for d in list(getattr(res, "documents", []) or []):
            asyncio.create_task(self._add_tile(d))

    async def _add_tile(self, doc: types.Document):
        mime = (doc.mime_type or "").lower()
        if _HAS_QTMULTI and "video" in mime and ".webm" in mime:
            try:
                if doc.id not in _WEBM_CACHE:
                    tmp = Path(tempfile.gettempdir()) / f"st_{doc.id}.webm"
                    if not tmp.exists():
                        await self.client.download_media(doc, file=str(tmp))
                    _WEBM_CACHE[doc.id] = tmp
                tile = _VideoTile(_DocWrap(doc, _emoji_for_doc(doc)), _WEBM_CACHE[doc.id])
                tile.clicked.connect(lambda w: self.picked.emit(w.doc))
                self.h.addWidget(tile); return
            except Exception:
                pass
        pm = await _thumb_pm(self.client, doc, self.lock, asyncio.Semaphore(4))
        btn = StickerButton(_DocWrap(doc, _emoji_for_doc(doc)), pm)
        if "tgs" in mime or "application/x-tgsticker" in mime:
            btn.setText("")
            badge = QLabel("TGS", btn)
            badge.setStyleSheet("QLabel { background:#2d6cdf; color:#fff; border-radius:6px; padding:0 4px; font: bold 10px;}")
            badge.move(4, 4); badge.show()
        btn.clickedWithDoc.connect(lambda w: self.picked.emit(w.doc))
        self.h.addWidget(btn)

# ==============================
#     ДИАЛОГ
# ==============================
class StickerPickerDialog(QDialog):
    stickerPicked = Signal(object)  # types.Document
    def __init__(self, client, lock=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Стикеры Telegram — выбор")
        self.resize(740, 580)
        self.client = client; self.lock = lock

        v = QVBoxLayout(self)

        row = QHBoxLayout()
        self.link = QLineEdit(self); self.link.setPlaceholderText("https://t.me/addstickers/<shortname>")
        b_add = QPushButton("Добавить набор", self); b_add.clicked.connect(lambda: asyncio.create_task(self._on_add()))
        b_del = QPushButton("Удалить набор", self); b_del.clicked.connect(lambda: asyncio.create_task(self._on_del()))
        row.addWidget(self.link, 1); row.addWidget(b_add); row.addWidget(b_del)
        v.addLayout(row)

        self.area = QScrollArea(self); self.area.setWidgetResizable(True)
        self.host = QWidget(self.area)
        self.grid = QGridLayout(self.host); self.grid.setContentsMargins(8,8,8,8); self.grid.setSpacing(8)
        self.area.setWidget(self.host)
        v.addWidget(self.area, 1)

        v.addWidget(QLabel("Наборы:", self))
        self.packs = QListWidget(self)
        self.packs.itemClicked.connect(lambda it: asyncio.create_task(self._load(it.data(Qt.UserRole))))
        self.packs.setContextMenuPolicy(Qt.CustomContextMenu)
        self.packs.customContextMenuRequested.connect(self._ctx)
        v.addWidget(self.packs)

        self._packs = _load_packs()
        self._rebuild()
        last = _load_last()
        if last:
            for i in range(self.packs.count()):
                if self.packs.item(i).data(Qt.UserRole) == last:
                    self.packs.setCurrentRow(i); break
            asyncio.create_task(self._load(last))

    def _rebuild(self):
        self.packs.clear()
        for p in self._packs:
            it = QListWidgetItem(f"{p.get('title') or p['short_name']} (@{p['short_name']})")
            it.setData(Qt.UserRole, p["short_name"])
            self.packs.addItem(it)

    def _ctx(self, pos):
        it = self.packs.itemAt(pos)
        if not it: return
        short = it.data(Qt.UserRole)
        m = QMenu(self); a = m.addAction("Удалить")
        a.triggered.connect(lambda: asyncio.create_task(self._remove(short)))
        m.exec(self.packs.mapToGlobal(pos))

    async def _on_add(self):
        sn = extract_shortname(self.link.text())
        if not sn:
            return QMessageBox.information(self, "Набор", "Укажи ссылку или shortname.")
        ok, title = await self._probe(sn)
        if not ok:
            return QMessageBox.warning(self, "Набор", "Не удалось получить набор.")
        if not any(p["short_name"] == sn for p in self._packs):
            self._packs.append({"short_name": sn, "title": title or sn})
            _save_packs(self._packs, last_short=sn)
            self._rebuild()
        self.link.clear()
        await self._load(sn)

    async def _on_del(self):
        it = self.packs.currentItem()
        if not it: return
        await self._remove(it.data(Qt.UserRole))

    async def _remove(self, short):
        self._packs = [p for p in self._packs if p["short_name"] != short]
        _save_packs(self._packs); self._rebuild(); self._clear()

    async def _probe(self, short):
        try:
            res = await self.client(functions.messages.GetStickerSetRequest(
                stickerset=types.InputStickerSetShortName(short_name=short), hash=0
            ))
            title = getattr(getattr(res, "set", None), "title", None)
            return True, title
        except Exception:
            return False, None

    def _clear(self):
        while self.grid.count():
            w = self.grid.takeAt(0).widget()
            if w: w.setParent(None)

    async def _load(self, short):
        _save_packs(self._packs, last_short=short)
        self._clear()
        try:
            res = await self.client(functions.messages.GetStickerSetRequest(
                stickerset=types.InputStickerSetShortName(short_name=short), hash=0
            ))
        except Exception as e:
            return QMessageBox.critical(self, "Набор", f"Ошибка загрузки: {e}")

        docs: List[types.Document] = list(getattr(res, "documents", []) or [])
        cols = 6; r = c = 0
        for d in docs:
            mime = (d.mime_type or "").lower()
            if _HAS_QTMULTI and "video" in mime and ".webm" in mime:
                try:
                    if d.id not in _WEBM_CACHE:
                        tmp = Path(tempfile.gettempdir()) / f"st_{d.id}.webm"
                        if not tmp.exists():
                            await self.client.download_media(d, file=str(tmp))
                        _WEBM_CACHE[d.id] = tmp
                    tile = _VideoTile(_DocWrap(d, _emoji_for_doc(d)), _WEBM_CACHE[d.id])
                    tile.clicked.connect(lambda w, dd=d: self._picked(dd))
                    self.grid.addWidget(tile, r, c)
                except Exception:
                    pm = await _thumb_pm(self.client, d, None, asyncio.Semaphore(1))
                    btn = StickerButton(_DocWrap(d, _emoji_for_doc(d)), pm)
                    btn.clickedWithDoc.connect(lambda w, dd=d: self._picked(dd))
                    self.grid.addWidget(btn, r, c)
            else:
                pm = await _thumb_pm(self.client, d, None, asyncio.Semaphore(1))
                btn = StickerButton(_DocWrap(d, _emoji_for_doc(d)), pm)
                if "tgs" in mime or "application/x-tgsticker" in mime:
                    btn.setText("")
                    badge = QLabel("TGS", btn)
                    badge.setStyleSheet("QLabel { background:#2d6cdf; color:#fff; border-radius:6px; padding:0 4px; font: bold 10px;}")
                    badge.move(4, 4); badge.show()
                btn.clickedWithDoc.connect(lambda w, dd=d: self._picked(dd))
                self.grid.addWidget(btn, r, c)

            c += 1
            if c >= cols:
                c = 0; r += 1

    def _picked(self, doc: types.Document):
        self.stickerPicked.emit(doc)
        self.accept()

async def pick_sticker_dialog(parent, client, lock=None):
    dlg = StickerPickerDialog(client, lock=lock, parent=parent)
    fut = asyncio.get_event_loop().create_future()
    dlg.stickerPicked.connect(lambda d: (not fut.done()) and fut.setResult(d))
    dlg.rejected.connect(lambda: (not fut.done()) and fut.setResult(None))
    dlg.show()
    return await fut

# ==============================
#     ВСТРОЙКА В MainWindow
# ==============================
def install_sticker_plugin(MainWindow_cls):
    """Интеграция плагина в существующий MainWindow."""

    async def _on_send_sticker(self):
        if not getattr(self, "current_entity_ref", None):
            return await self._mb_info("Стикер", "Откройте чат.")
        acc = await self._choose_account_for_send(advance=True)
        if not acc:
            return await self._mb_info("Стикер", "Нет доступного аккаунта.")
        ip = await self._get_input_peer(acc, self.current_entity_ref)
        if not ip:
            return await self._mb_warn("Стикер", "Нет доступа к чату.")
        doc = await pick_sticker_dialog(self, acc.client, lock=acc.api_lock)
        if not doc:
            return
        reply_to_id = getattr(self, "_main_reply_target", None)
        reply_to_id = reply_to_id.id if reply_to_id else None
        try:
            await self._run_acc(acc, acc.client.send_file(ip, doc, reply_to=reply_to_id))
            if hasattr(self, "_clear_main_reply_target"):
                self._clear_main_reply_target()
            self._update_labels()
        except Exception as e:
            if hasattr(self, "_is_frozen_error") and self._is_frozen_error(e):
                await self._kill_account(acc, "Аккаунт заморожен (read-only)")
            else:
                await self._mb_warn("Стикер", f"Не удалось отправить: {e}")

    async def _send_sticker_to_comments(self, doc: types.Document):
        if not (getattr(self, "_comments_ctx_entity_ref", None) and getattr(self, "_comments_ctx_post_id", None)):
            return await self._mb_info("Стикер", "Откройте ветку комментариев.")
        acc = await self._choose_account_for_send(advance=True)
        if not acc:
            return await self._mb_warn("Комментарии", "Нет доступного аккаунта.")
        try:
            host = importlib.import_module(self.__class__.__module__)
            channel = await self._run_acc(acc, host.resolve_ref(acc.client, self._comments_ctx_entity_ref))
            discussion = await self._get_discussion_chat(acc.client, channel)
            if not discussion:
                return await self._mb_warn("Комментарии", "У поста нет ветки комментариев.")
            await self._run_acc(acc, host.ensure_join(acc.client, discussion))

            post_id = int(self._comments_ctx_post_id)
            root = self._comments_ctx_root_discussion_id
            if not root:
                root = await self._get_discussion_root_id(acc, channel, post_id, discussion)
                self._comments_ctx_root_discussion_id = root
            if not root:
                return await self._mb_warn("Комментарии", "Не удалось определить корень обсуждения.")

            reply_target = self.comments.current_reply_target()
            reply_to_id = int(reply_target.id) if reply_target else int(root)

            sent = await self._run_acc(acc, acc.client.send_file(discussion, doc, reply_to=reply_to_id))
            if isinstance(sent, list):
                sent = sent[0] if sent else None
            if isinstance(sent, types.Message):
                allowed = await self._run_acc(acc, host.get_allowed_reaction_emojis(acc.client, discussion))
                bub = self.comments.add_comment_bubble_top(sent, True, emojis=allowed)
                self._comments_known_ids.add(sent.id)
                asyncio.create_task(self._maybe_resolve_and_set_author(acc.client, bub, sent))
            self.comments.clear_reply_indicator()
            self._update_labels()
        except Exception as e:
            if hasattr(self, "_is_frozen_error") and self._is_frozen_error(e):
                await self._kill_account(acc, "Аккаунт заморожен (read-only)")
            else:
                await self._mb_warn("Стикеры", f"{e}")

    setattr(MainWindow_cls, "_on_send_sticker", _on_send_sticker)
    setattr(MainWindow_cls, "_send_sticker_to_comments", _send_sticker_to_comments)

    _orig_init_ui = getattr(MainWindow_cls, "_init_ui")
    _orig_open_comments = getattr(MainWindow_cls, "_open_comments_for_post", None)
    _orig_reopen_comments = getattr(MainWindow_cls, "_reopen_comments_for_current_account", None)
    _orig_load_messages = getattr(MainWindow_cls, "_load_messages", None)

    async def _send_direct(self, doc):
        if not getattr(self, "current_entity_ref", None):
            return await self._mb_info("Стикер", "Откройте чат.")
        acc = await self._choose_account_for_send(advance=True)
        if not acc:
            return await self._mb_info("Стикер", "Нет доступного аккаунта.")
        ip = await self._get_input_peer(acc, self.current_entity_ref)
        if not ip:
            return await self._mb_warn("Стикер", "Нет доступа к чату.")
        await self._run_acc(acc, acc.client.send_file(ip, doc))
        self._update_labels()

    def _ensure_comments_sticker_button(self):
        if not hasattr(self, "comments"):
            return
        panel = self.comments
        if getattr(self, "_btn_sticker_comments", None):
            return
        v = panel.layout()
        row = None
        if v:
            for i in range(v.count() - 1, -1, -1):
                it = v.itemAt(i)
                lay = it.layout()
                if not lay: continue
                has_input = False; has_send = False
                for j in range(lay.count()):
                    w = lay.itemAt(j).widget()
                    if w is panel.input: has_input = True
                    if isinstance(w, QPushButton) and ("Отправить" in (w.text() or "")): has_send = True
                if has_input and has_send:
                    row = lay; break
        if not row and v and v.count()>0 and v.itemAt(v.count()-1).layout():
            row = v.itemAt(v.count()-1).layout()
        if not row: return

        btn = QPushButton("Стикеры", panel)
        btn.setCursor(Qt.PointingHandCursor)

        async def _open_picker_for_comments():
            acc = await MainWindow_cls._choose_account_for_send(self, advance=True)
            if not acc:
                return await MainWindow_cls._mb_warn(self, "Комментарии", "Нет доступного аккаунта.")
            doc = await pick_sticker_dialog(self, acc.client, lock=acc.api_lock)
            if not doc: return
            await MainWindow_cls._send_sticker_to_comments(self, doc)

        btn.clicked.connect(lambda: asyncio.create_task(_open_picker_for_comments()))
        insert_at = max(1, row.count() - 1)  # перед «Отправить»
        row.insertWidget(insert_at, btn)
        self._btn_sticker_comments = btn

    def _strip_comment_buttons_in_view(self):
        try:
            host = importlib.import_module(self.__class__.__module__)
            MessageBubble = getattr(host, "MessageBubble", None)
            if not MessageBubble or not hasattr(self, "comments"): return
            panel = self.comments
            for b in panel.findChildren(MessageBubble):
                lay = b.layout()
                if not lay or lay.count() == 0: continue
                actions = lay.itemAt(lay.count() - 1).layout()
                if not actions: continue
                for i in range(actions.count() - 1, -1, -1):
                    w = actions.itemAt(i).widget()
                    if isinstance(w, QPushButton) and (w.text() or "").startswith("Коммент"):
                        w.setParent(None)
        except Exception:
            pass

    def _wire_chat_reply_buttons(self):
        try:
            host = importlib.import_module(self.__class__.__module__)
            MessageBubble = getattr(host, "MessageBubble", None)
            chat_inner = getattr(self, "chat_inner", None)
            if not MessageBubble or chat_inner is None:
                return
            for b in chat_inner.findChildren(MessageBubble):
                if getattr(b, "_reply_hooked", False):
                    continue
                try:
                    b.replyClicked.connect(lambda m, self=self: self._select_main_reply_target(m))
                    b._reply_hooked = True
                except Exception:
                    pass
        except Exception:
            pass

    setattr(MainWindow_cls, "_ensure_comments_sticker_button", _ensure_comments_sticker_button)
    setattr(MainWindow_cls, "_strip_comment_buttons_in_view", _strip_comment_buttons_in_view)
    setattr(MainWindow_cls, "_wire_chat_reply_buttons", _wire_chat_reply_buttons)

    def _init_ui_patched(self, *a, **kw):
        _orig_init_ui(self, *a, **kw)

        # Меню «Стикеры»
        try:
            mb = self.menuBar()
            menu = mb.addMenu("Стикеры")
            act = QAction("Выбрать и отправить…", self)
            act.setShortcut("Ctrl+Shift+S")
            act.triggered.connect(lambda: asyncio.create_task(self._on_send_sticker()))
            menu.addAction(act)
        except Exception:
            pass

        # Кнопка «Стикер» в чате
        try:
            if hasattr(self, "btn_sticker"):
                try:
                    self.btn_sticker.clicked.disconnect()
                except Exception:
                    pass
                self.btn_sticker.clicked.connect(lambda: asyncio.create_task(self._on_send_sticker()))
        except Exception:
            pass

        # Полка в чате
        self._sticker_shelf_chat = StickerShelf(None, None, "Стикеры (чат)", parent=self)
        try:
            cw = self.centralWidget()
            center = cw.widget(1) if hasattr(cw, "widget") else None
            lay = center.layout() if center else None
            if lay:
                lay.addWidget(self._sticker_shelf_chat)
        except Exception:
            pass
        self._sticker_shelf_chat.picked.connect(lambda d: asyncio.create_task(_send_direct(self, d)))

        # Комментарии — кнопка + убрать «Комментарии»
        self._ensure_comments_sticker_button()
        self._strip_comment_buttons_in_view()

    setattr(MainWindow_cls, "_init_ui", _init_ui_patched)

    # при смене аккаунта — обновим клиента полки
    if hasattr(MainWindow_cls, "_on_account_changed"):
        _orig_on_acc_changed = getattr(MainWindow_cls, "_on_account_changed")
        async def _on_account_changed_patched(self, cur, prev):
            await _orig_on_acc_changed(self, cur, prev)
            try:
                uid = self.current_view_account_id
                if uid is not None and uid in self.accounts:
                    acc = self.accounts[uid]
                    if getattr(self, "_sticker_shelf_chat", None):
                        self._sticker_shelf_chat.set_client(acc.client, acc.api_lock)
            except Exception:
                pass
        setattr(MainWindow_cls, "_on_account_changed", _on_account_changed_patched)

    # после открытия/переоткрытия ветки комментариев — гарантируем кнопку и чистку
    if _orig_open_comments:
        async def _open_comments_patched(self, *a, **kw):
            await _orig_open_comments(self, *a, **kw)
            self._ensure_comments_sticker_button()
            self._strip_comment_buttons_in_view()
        setattr(MainWindow_cls, "_open_comments_for_post", _open_comments_patched)

    if _orig_reopen_comments:
        async def _reopen_comments_patched(self, *a, **kw):
            await _orig_reopen_comments(self, *a, **kw)
            self._ensure_comments_sticker_button()
            self._strip_comment_buttons_in_view()
        setattr(MainWindow_cls, "_reopen_comments_for_current_account", _reopen_comments_patched)

    # после загрузки сообщений — цепляем «Ответить», чтобы стикер уходил как reply
    if _orig_load_messages:
        async def _load_messages_patched(self, *a, **kw):
            await _orig_load_messages(self, *a, **kw)
            self._wire_chat_reply_buttons()
        setattr(MainWindow_cls, "_load_messages", _load_messages_patched)
