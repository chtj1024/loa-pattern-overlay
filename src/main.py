"""로아 패턴 오버레이 (배포판).

화면의 보스 이름 / 남은 줄 수(X 205)를 OCR로 읽어 patterns 폴더에 적어둔 패턴을
이전 2개 · 진행 중 패턴 · 그 다음 2개 형태로 게임 위에 띄운다.
보스가 없을 땐 왼쪽 위 콘텐츠 제목 + 관문 체크(V) 표시를 읽어 해당 관문의 정비소 준비 텍스트를 띄운다.
평소엔 클릭이 통과되고, 이동 모드(기본 Ctrl+Shift+M)일 때만 드래그로 옮길 수 있다.
"""
import argparse
import copy
import ctypes
import ctypes.wintypes as wt
import json
import logging
import logging.handlers
import os
import shutil
import sys
import time
import traceback
from pathlib import Path

import mss
from PIL import Image
from PySide6.QtCore import QPoint, QRect, Qt, QThread, QTimer, Signal
from PySide6.QtGui import (QAction, QColor, QFont, QFontMetrics, QIcon, QImage, QPainter,
                           QPainterPath, QPen, QPixmap)
from PySide6.QtWidgets import QApplication, QMenu, QMessageBox, QSystemTrayIcon, QWidget

from ocr import Ocr
from patterns import (GUIDE_TEXT, all_boss_names, bar_patterns, dir_signature, empty_data,
                      find_hidden_hp_patterns, find_patterns, load_dir, pattern_window, prep_lines)

_MSS = getattr(mss, "MSS", None) or mss.mss

APP_NAME = "로아 패턴 오버레이"
VERSION = "1.2.0"
NOTICE_VERSION = 2  # 첫 실행 안내 문구 버전 (문구를 바꾸면 올림)

# 프로그램 폴더: exe로 묶였을 땐 exe가 있는 폴더, 아니면 이 파일이 있는 폴더 (업데이트 시 통째로 교체)
BASE = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) \
    else Path(__file__).resolve().parent
STARTER_DIR = BASE / "처음 패턴"          # 첫 실행 때 한 번만 복사해 주는 패턴


def documents_dir():
    """사용자의 실제 문서 폴더 (OneDrive로 옮겨진 경우 포함)."""
    class GUID(ctypes.Structure):
        _fields_ = [("Data1", wt.DWORD), ("Data2", wt.WORD), ("Data3", wt.WORD),
                    ("Data4", ctypes.c_ubyte * 8)]
    folderid_documents = GUID(0xFDD39AD0, 0x238F, 0x46AF,
                              (ctypes.c_ubyte * 8)(0xAD, 0xB4, 0x6C, 0x85, 0x48, 0x03, 0x69, 0xC7))
    out = ctypes.c_wchar_p()
    try:
        if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(folderid_documents), 0, None,
                                                      ctypes.byref(out)) == 0:
            path = out.value
            ctypes.windll.ole32.CoTaskMemFree(out)
            return Path(path)
    except OSError:
        pass
    return Path.home() / "Documents"


# 사용자 데이터 폴더: 업데이트해도 유지 (LOA_OVERLAY_DATA_DIR 환경변수로 바꿀 수 있음 — 테스트용)
DATA_DIR = Path(os.environ.get("LOA_OVERLAY_DATA_DIR") or documents_dir() / APP_NAME)
CONFIG_PATH = DATA_DIR / "config.json"
PATTERNS_DIR = DATA_DIR / "패턴"
LOG_PATH = DATA_DIR / "log.txt"

log = logging.getLogger("overlay")


def setup_logging(debug):
    log.setLevel(logging.DEBUG if debug else logging.INFO)
    fh = logging.handlers.RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=1,
                                              encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    log.addHandler(fh)
    if sys.stdout is not None:  # 콘솔이 있을 때만 (exe는 콘솔 없음)
        log.addHandler(logging.StreamHandler(sys.stdout))

    def excepthook(etype, value, tb):
        log.error("처리되지 않은 오류:\n%s", "".join(traceback.format_exception(etype, value, tb)))
    sys.excepthook = excepthook


def prepare_data_dir():
    """사용자 데이터 폴더를 만들 수 있는지 확인."""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        probe = DATA_DIR / ".write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


_mutex = None


def already_running():
    global _mutex
    _mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "LoaPatternOverlay_SingleInstance")
    return ctypes.windll.kernel32.GetLastError() == 183  # ERROR_ALREADY_EXISTS


def find_old_version():
    """v1.0.0처럼 프로그램 폴더 안에 config/patterns를 저장하던 예전 버전 폴더 찾기.
    (이 폴더 자체, 또는 같은 위치에 풀어둔 LoaPatternOverlay_v* 폴더 중 가장 최근 것)"""
    candidates = [BASE] + [p for p in BASE.parent.glob("LoaPatternOverlay_v*")
                           if p.is_dir() and p.resolve() != BASE]
    found = [p for p in candidates if (p / "config.json").exists() or (p / "patterns").is_dir()]
    if not found:
        return None

    def mtime(p):
        files = [p / "config.json"] + list((p / "patterns").glob("*.txt"))
        return max((f.stat().st_mtime for f in files if f.exists()), default=0)
    return max(found, key=mtime)


def _copy_txt(src_dir, dst_dir):
    copied = []
    for f in sorted(src_dir.glob("*.txt")):
        if f.name.startswith("_"):
            continue
        shutil.copy2(f, dst_dir / f.name)
        copied.append(f.name)
    return copied


def prepare_patterns_dir():
    """패턴 폴더가 없을 때(첫 실행) 한 번만 채워 넣음. 알림 메시지 목록 반환.
    1) 예전 버전 폴더가 있으면 그 패턴/설정을 옮겨 옴  2) 없으면 '처음 패턴'을 복사."""
    msgs = []
    if not PATTERNS_DIR.exists():
        PATTERNS_DIR.mkdir(parents=True)
        old = find_old_version()
        if old is not None:
            copied = _copy_txt(old / "patterns", PATTERNS_DIR) if (old / "patterns").is_dir() else []
            if (old / "config.json").exists() and not CONFIG_PATH.exists():
                shutil.copy2(old / "config.json", CONFIG_PATH)
            log.info("예전 버전에서 옮겨 옴: %s (패턴 %s)", old, copied)
            msgs.append(f"예전 버전의 설정과 패턴 {len(copied)}개를 옮겨 왔어요.")
        elif STARTER_DIR.is_dir():
            copied = _copy_txt(STARTER_DIR, PATTERNS_DIR)
            log.info("처음 패턴 복사: %s", copied)
        msgs.append(f"패턴 폴더: {PATTERNS_DIR}")
    guide = PATTERNS_DIR / "_작성법.txt"
    guide_text = "// 이 파일은 읽지 않습니다 (이름이 _ 로 시작)\n" + GUIDE_TEXT
    if not guide.exists() or guide.read_text(encoding="utf-8-sig") != guide_text:
        guide.write_text(guide_text, encoding="utf-8")
    return msgs


DEFAULT_CONFIG = {
    # regions_base 해상도 기준 [x, y, 너비, 높이]. 다른 해상도에선 자동 환산.
    # 트레이 메뉴 '인식 영역 설정'으로 다시 지정 가능
    "regions_base": [1920, 1080],
    "regions": {"name": [660, 30, 600, 26], "bars": [1040, 62, 250, 26],
                "title": [20, 44, 260, 28], "gate": [10, 95, 280, 72]},
    "accepted_notice": 0,  # 동의한 안내 문구 버전 (NOTICE_VERSION 참고)
    "interval_ms": 250,
    "name_match_threshold": 0.6,
    "lost_timeout_s": 4,
    "hotkeys": {"move": "ctrl+shift+m", "toggle": "ctrl+shift+h", "quit": "ctrl+shift+q"},
    "overlay": {
        "x": 1174, "y": 560,
        "font": "맑은 고딕", "font_size": 12,
        "color_prev": "#ffffff", "color_next": "#ffd54a", "color_after": "#ffffff",
        "color_prep": "#bfe6ff",
        "max_width": 720,
        "background_alpha": 0,
        "show_status": True,
    },
}


# ---------------------------------------------------------------- 설정 파일
def _deep_merge(default, loaded):
    out = copy.deepcopy(default)
    for k, v in loaded.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_config():
    if not CONFIG_PATH.exists():
        write_json(CONFIG_PATH, DEFAULT_CONFIG)
    try:
        loaded = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as e:
        log.error("config.json 읽기 실패, 기본값 사용: %s", e)
        loaded = {}
    return _deep_merge(DEFAULT_CONFIG, loaded)


# ---------------------------------------------------------------- 해상도 환산
# 보스 이름/줄 수는 화면 가운데 기준, 콘텐츠 제목/관문 표시는 왼쪽 위 기준으로 붙어 있다.
CENTER_ANCHORED = {"name", "bars"}


def scale_region(key, region, base, screen):
    """base 해상도 기준 영역을 screen 해상도로 환산 (UI가 화면 높이에 비례한다고 가정)."""
    (bw, bh), (sw, sh) = base, screen
    s = sh / bh
    x, y, w, h = region
    if key in CENTER_ANCHORED:
        nx = sw / 2 + (x - bw / 2) * s
    else:
        nx = x * s
    return [int(round(nx)), int(round(y * s)), max(1, int(round(w * s))), max(1, int(round(h * s)))]


def effective_regions(cfg, screen):
    base = cfg.get("regions_base", [1920, 1080])
    return {k: scale_region(k, r, base, screen) for k, r in cfg["regions"].items()}


def primary_screen_size():
    with _MSS() as sct:
        m = sct.monitors[1]
        return m["width"], m["height"]



# ---------------------------------------------------------------- 전역 단축키
user32 = ctypes.windll.user32
user32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, wt.UINT]
_MODS = {"alt": 0x1, "ctrl": 0x2, "shift": 0x4, "win": 0x8}


def parse_hotkey(combo):
    mods, vk = 0x4000, None  # MOD_NOREPEAT
    for part in combo.lower().replace(" ", "").split("+"):
        if part in _MODS:
            mods |= _MODS[part]
        elif len(part) == 1 and part.isalnum():
            vk = ord(part.upper())
        elif part.startswith("f") and part[1:].isdigit():
            vk = 0x6F + int(part[1:])
        else:
            raise ValueError(combo)
    if vk is None:
        raise ValueError(combo)
    return mods, vk


class HotkeyThread(QThread):
    triggered = Signal(str)
    failed = Signal(str)

    def __init__(self, mapping):
        super().__init__()
        self.mapping = mapping
        self._tid = None

    def run(self):
        self._tid = ctypes.windll.kernel32.GetCurrentThreadId()
        ids = {}
        for i, (name, combo) in enumerate(self.mapping.items(), start=1):
            try:
                mods, vk = parse_hotkey(combo)
            except ValueError:
                self.failed.emit(combo)
                continue
            if user32.RegisterHotKey(None, i, mods, vk):
                ids[i] = name
            else:
                self.failed.emit(combo)
        msg = wt.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == 0x0312 and msg.wParam in ids:  # WM_HOTKEY
                self.triggered.emit(ids[msg.wParam])
        for i in ids:
            user32.UnregisterHotKey(None, i)

    def stop(self):
        if self._tid:
            user32.PostThreadMessageW(self._tid, 0x0012, 0, 0)  # WM_QUIT
        self.wait(1000)


# ---------------------------------------------------------------- OCR 스레드
def grab_region(sct, region):
    x, y, w, h = region
    shot = sct.grab({"left": x, "top": y, "width": w, "height": h})
    return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")


class OcrWorker(QThread):
    state = Signal(object)
    error = Signal(str)

    def __init__(self, app_state, test_image=None):
        super().__init__()
        self.app_state = app_state
        self.test_image = Image.open(test_image).convert("RGB") if test_image else None
        self._running = True

    def stop(self):
        self._running = False
        self.wait(2000)

    def _screen_size(self, sct):
        if self.test_image is not None:
            return self.test_image.size
        m = sct.monitors[1]
        return m["width"], m["height"]

    def _grab(self, sct, key):
        """해상도에 맞게 환산한 영역을 캡처한 뒤, 기준 해상도 크기로 되돌려서 반환.
        (OCR 전처리/관문 체크 판정 기준을 해상도와 무관하게 유지)"""
        x, y, w, h = self._regions[key]
        if self.test_image is not None:
            img = self.test_image.crop((x, y, x + w, y + h))
        else:
            img = grab_region(sct, (x, y, w, h))
        bw, bh = self.app_state.cfg["regions"][key][2:]
        if abs(w - bw) > 2 or abs(h - bh) > 2:  # 1~2px 차이는 리사이즈하면 오히려 흐려짐
            img = img.resize((bw, bh), Image.LANCZOS)
        return img

    def _grab_hpbar(self, sct):
        """피통 막대 안쪽 띠. 보스 이름 영역 바로 아래(같은 폭)라서 이름 영역에서 계산."""
        x, y, w, h = self._regions["name"]
        k = h / 26  # 기준 이름 영역 높이 26px
        y, h = y + int(round(36 * k)), max(4, int(round(14 * k)))
        if self.test_image is not None:
            return self.test_image.crop((x, y, x + w, y + h))
        return grab_region(sct, (x, y, w, h))

    def run(self):
        try:
            ocr = Ocr()
        except Exception as e:  # noqa: BLE001
            log.error("OCR 초기화 실패: %s", e)
            self.error.emit(str(e))
            return
        sct = _MSS()
        boss, bars, pending = None, None, None
        title, gate, gate_pending = None, None, None
        hidden, hidden_streak = False, 0
        last_seen, last_name_check = 0.0, 0.0
        title_seen, last_title_check = 0.0, 0.0
        last_screen_check, screen = 0.0, None
        while self._running:
            t0 = time.monotonic()
            cfg = self.app_state.cfg
            data = self.app_state.data
            if t0 - last_screen_check >= 5.0:  # 해상도 변경 대비
                last_screen_check = t0
                new_screen = self._screen_size(sct)
                if new_screen != screen:
                    screen = new_screen
                    log.info("화면 해상도 %sx%s", *screen)
            self._regions = effective_regions(cfg, screen)
            raw_name = raw_title = None
            try:
                # 콘텐츠 제목 + 관문 체크 표시 (1초마다)
                if data["raids"] and t0 - last_title_check >= 1.0:
                    last_title_check = t0
                    cand, score, raw_title = ocr.read_title(self._grab(sct, "title"),
                                                            list(data["raids"]))
                    if cand and score >= cfg["name_match_threshold"]:
                        if cand != title:
                            title, gate, gate_pending = cand, None, None
                        title_seen = t0
                    elif title and t0 - title_seen > cfg["lost_timeout_s"]:
                        title, gate, gate_pending = None, None, None
                    if title:
                        g = ocr.read_gate(self._grab(sct, "gate"))
                        # 관문 표시가 접혀 있으면(None) 마지막으로 본 관문 유지
                        if g is not None and g != gate:
                            if gate is None or g == gate_pending:
                                gate, gate_pending = g, None
                            else:
                                gate_pending = g

                if boss is None or t0 - last_name_check >= 1.0:
                    last_name_check = t0
                    names = all_boss_names(data)
                    cand, score, raw_name = ocr.read_name(self._grab(sct, "name"), names)
                    if cand and score >= cfg["name_match_threshold"]:
                        if cand != boss:
                            boss, bars, pending = cand, None, None
                        last_seen = t0
                    elif boss and t0 - last_seen > cfg["lost_timeout_s"]:
                        boss, bars, pending = None, None, None

                # 피통이 ???(회색)인지: 글자 대신 색으로 판단, 2번 연속 같은 결과일 때 전환
                now_hidden = ocr.is_hidden_hp_bar(self._grab_hpbar(sct))
                hidden_streak = hidden_streak + 1 if now_hidden != hidden else 0
                if hidden_streak >= 2:
                    hidden, hidden_streak = now_hidden, 0

                if boss is not None and not hidden:
                    val, raw_bars = ocr.read_bars(self._grab(sct, "bars"))
                    # 같은 값이 2번 연속 읽혀야 반영 (OCR 오인식 방지)
                    if val is not None and val != bars:
                        if val == pending:
                            bars, pending = val, None
                        else:
                            pending = val
                else:
                    raw_bars = []
                self.state.emit({"boss": boss, "bars": bars, "title": title, "gate": gate,
                                 "hidden": hidden, "raw_name": raw_name, "raw_bars": raw_bars,
                                 "raw_title": raw_title})
            except Exception as e:  # noqa: BLE001  (화면 잠금 등 일시적 캡처 실패)
                self.state.emit({"boss": boss, "bars": bars, "title": title, "gate": gate,
                                 "hidden": hidden, "raw_name": f"오류: {e}", "raw_bars": [],
                                 "raw_title": None})
            dt = time.monotonic() - t0
            time.sleep(max(0.02, cfg["interval_ms"] / 1000 - dt))


# ---------------------------------------------------------------- 오버레이 창
class Overlay(QWidget):
    PAD = 10

    def __init__(self, app_state):
        super().__init__()
        self.app_state = app_state
        self.locked = True
        self.user_hidden = False
        self.rows = []        # (kind, bar, text)
        self.status = ""
        self._drag = None
        self._layout_lines = []
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self._apply_flags()
        o = app_state.cfg["overlay"]
        self.move(o["x"], o["y"])
        self._relayout()

        self._topmost = QTimer(self)
        self._topmost.timeout.connect(self._keep_topmost)
        self._topmost.start(2000)

    # --- 창 속성
    def _apply_flags(self):
        flags = (Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
                 | Qt.WindowDoesNotAcceptFocus)
        if self.locked:
            flags |= Qt.WindowTransparentForInput  # 클릭 통과
        self.setWindowFlags(flags)
        if not self.user_hidden:
            self.show()

    def _keep_topmost(self):
        if self.isVisible():
            # HWND_TOPMOST, SWP_NOSIZE|SWP_NOMOVE|SWP_NOACTIVATE
            user32.SetWindowPos(int(self.winId()), wt.HWND(-1), 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)

    def set_move_mode(self, on):
        self.locked = not on
        if on:
            self.user_hidden = False
        else:
            o = self.app_state.cfg["overlay"]
            o["x"], o["y"] = self.x(), self.y()
            self.app_state.save_config()
        self._apply_flags()
        self._relayout()

    def toggle_visible(self):
        self.user_hidden = not self.user_hidden
        self.setVisible(not self.user_hidden)

    # --- 내용
    def set_content(self, rows, status):
        if rows != self.rows or status != self.status:
            self.rows, self.status = rows, status
            self._relayout()

    def _fonts(self):
        o = self.app_state.cfg["overlay"]
        normal = QFont(o["font"], o["font_size"])
        bold = QFont(o["font"], o["font_size"])
        bold.setBold(True)
        small = QFont(o["font"], max(8, o["font_size"] - 5))
        return normal, bold, small

    @staticmethod
    def _wrap(text, font, width):
        """줄바꿈(\\n) 유지 + width를 넘으면 단어 단위(안 되면 글자 단위)로 줄바꿈."""
        fm = QFontMetrics(font)
        out = []
        for para in text.split("\n"):
            line = ""
            for word in para.split(" "):
                cand = f"{line} {word}" if line else word
                if fm.horizontalAdvance(cand) <= width:
                    line = cand
                    continue
                if line:
                    out.append(line)
                line = ""
                for ch in word:  # 단어 하나가 너무 길면 글자 단위로 자름
                    if fm.horizontalAdvance(line + ch) > width and line:
                        out.append(line)
                        line = ""
                    line += ch
            out.append(line)
        return out

    def _lines(self):
        """(font, color, [(x, 문자열), ...]) 목록"""
        o = self.app_state.cfg["overlay"]
        normal, bold, small = self._fonts()
        fm_b = QFontMetrics(bold)
        marker_w = fm_b.horizontalAdvance("▶ ")
        text_x = marker_w + fm_b.horizontalAdvance("x0000  ")
        bullet_w = fm_b.horizontalAdvance("•  ")
        max_w = max(200, int(o.get("max_width", 720)))
        lines = []
        if not self.locked:
            mv = self.app_state.cfg["hotkeys"]["move"].upper()
            lines.append((small, "#7fd3ff", [(0, f"이동 모드 — 드래그로 옮기고 {mv}로 고정")]))
        if self.status and o["show_status"]:
            lines.append((small, "#b0b0b0", [(0, self.status)]))
        rows = self.rows
        if not rows and not self.locked:
            rows = [("prev", 250, "이전 패턴 (예시)"), ("next", 200, "다음 패턴 (예시)"),
                    ("after", 150, "그 다음 패턴 (예시)")]
        for kind, bar, text in rows:
            if kind == "prep":
                font, color = normal, o.get("color_prep", "#ffffff")
                for i, seg in enumerate(self._wrap(text, font, max_w - bullet_w)):
                    lines.append((font, color, ([(0, "•")] if i == 0 else []) + [(bullet_w, seg)]))
                continue
            label = f"{bar:g}%" if kind == "pct" else f"x{bar}"
            if kind == "next":
                font, color, marker = bold, o["color_next"], "▶"
            else:
                font, marker = normal, ""
                color = o["color_prev"] if kind == "prev" else o["color_after"]
            for i, seg in enumerate(self._wrap(text, font, max_w - text_x)):
                cells = [(0, marker), (marker_w, label)] if i == 0 else []
                lines.append((font, color, cells + [(text_x, seg)]))
        return lines

    def _relayout(self):
        lines = self._lines()
        width, height = 120, self.PAD * 2
        for font, _, cells in lines:
            fm = QFontMetrics(font)
            for x, s in cells:
                width = max(width, x + fm.horizontalAdvance(s) + self.PAD * 2 + 6)
            height += fm.height() + 2
        self._layout_lines = lines
        self.resize(width, height)
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        alpha = self.app_state.cfg["overlay"]["background_alpha"]
        if not self.locked:
            p.fillRect(self.rect(), QColor(0, 0, 0, 150))
            p.setPen(QPen(QColor("#7fd3ff"), 2, Qt.DashLine))
            p.drawRect(self.rect().adjusted(1, 1, -1, -1))
        elif alpha and self._layout_lines:
            p.fillRect(self.rect(), QColor(0, 0, 0, int(alpha)))
        y = self.PAD
        outline = QPen(QColor(0, 0, 0, 230), 3.5)
        for font, color, cells in self._layout_lines:
            fm = QFontMetrics(font)
            base = y + fm.ascent()
            for px, s in cells:
                if not s:
                    continue
                path = QPainterPath()
                path.addText(self.PAD + px, base, font, s)
                p.strokePath(path, outline)
                p.fillPath(path, QColor(color))
            y += fm.height() + 2

    # --- 드래그 이동 (이동 모드에서만 이벤트가 들어옴)
    def mousePressEvent(self, e):
        if not self.locked and e.button() == Qt.LeftButton:
            self._drag = e.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e):
        if self._drag is not None:
            self.move(e.globalPosition().toPoint() - self._drag)

    def mouseReleaseEvent(self, _):
        if self._drag is not None:
            self._drag = None
            o = self.app_state.cfg["overlay"]
            o["x"], o["y"] = self.x(), self.y()
            self.app_state.save_config()


# ---------------------------------------------------------------- 인식 영역 지정
class RegionSelector(QWidget):
    done = Signal(dict)
    STEPS = [("name", "① 보스 이름 영역을 드래그하세요 (예: 대악마, 카제로스)"),
             ("bars", "② 남은 줄 수 영역을 드래그하세요 (예: X 205)"),
             ("title", "③ 왼쪽 위 콘텐츠 제목을 드래그하세요 (예: 종막 : 최후의 날)"),
             ("gate", "④ 관문 진행 표시(체크 V 아이콘들)를 드래그하세요")]

    def __init__(self):
        super().__init__()
        with _MSS() as sct:
            mon = sct.monitors[1]
            shot = sct.grab(mon)
        self.mon_left, self.mon_top = mon["left"], mon["top"]
        img = QImage(shot.bgra, shot.width, shot.height, QImage.Format_RGB32).copy()
        self.pix = QPixmap.fromImage(img)
        screen = QApplication.primaryScreen()
        self.dpr = screen.devicePixelRatio()
        self.pix.setDevicePixelRatio(self.dpr)
        self.step = 0
        self.result = {}
        self.chosen = []
        self.start = self.cur = None
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setCursor(Qt.CrossCursor)
        self.setGeometry(screen.geometry())
        self.showFullScreen()
        self.activateWindow()

    def paintEvent(self, _):
        p = QPainter(self)
        p.drawPixmap(0, 0, self.pix)
        p.fillRect(self.rect(), QColor(0, 0, 0, 90))
        p.setPen(QPen(QColor("#4cff7a"), 2))
        for r in self.chosen:
            p.drawRect(r)
        if self.start and self.cur:
            p.setPen(QPen(QColor("#ffd54a"), 2))
            p.drawRect(QRect(self.start, self.cur).normalized())
        f = QFont("맑은 고딕", 16)
        f.setBold(True)
        p.setFont(f)
        msg = self.STEPS[self.step][1] + "    (Esc: 여기까지만 저장하고 종료)"
        box = QRect(0, 140, self.width(), 50)
        p.fillRect(box, QColor(0, 0, 0, 180))
        p.setPen(QColor("white"))
        p.drawText(box, Qt.AlignCenter, msg)

    def mousePressEvent(self, e):
        self.start = self.cur = e.position().toPoint()
        self.update()

    def mouseMoveEvent(self, e):
        if self.start:
            self.cur = e.position().toPoint()
            self.update()

    def mouseReleaseEvent(self, e):
        r = QRect(self.start, e.position().toPoint()).normalized()
        self.start = self.cur = None
        if r.width() < 5 or r.height() < 5:
            self.update()
            return
        d = self.dpr
        self.result[self.STEPS[self.step][0]] = [
            int(self.mon_left + r.x() * d), int(self.mon_top + r.y() * d),
            int(r.width() * d), int(r.height() * d)]
        self.chosen.append(r)
        self.step += 1
        if self.step >= len(self.STEPS):
            self.done.emit(self.result)
            self.close()
        else:
            self.update()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            if self.result:
                self.done.emit(self.result)
            self.close()


# ---------------------------------------------------------------- 앱
def make_icon():
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor("#ffd54a"))
    p.setPen(QPen(QColor("#222"), 4))
    p.drawEllipse(4, 4, 56, 56)
    p.setPen(QColor("#222"))
    f = QFont("맑은 고딕", 26)
    f.setBold(True)
    p.setFont(f)
    p.drawText(pm.rect(), Qt.AlignCenter, "L")
    p.end()
    return QIcon(pm)


class App:
    def __init__(self, qapp, cfg, startup_msgs, test_image=None, debug=False):
        self.qapp = qapp
        self.debug = debug
        self.cfg = cfg
        test_size = Image.open(test_image).size if test_image else None
        self.data = empty_data()
        self._patterns_sig = None
        self.last_state = {"boss": None, "bars": None, "title": None, "gate": None}
        startup_msgs = list(startup_msgs)
        startup_msgs += self.reload_patterns(initial=True) or []

        self.overlay = Overlay(self)

        self.tray = QSystemTrayIcon(make_icon())
        self.tray.setToolTip(f"{APP_NAME} v{VERSION}")
        menu = QMenu()
        hk = self.cfg["hotkeys"]
        self.act_move = QAction(f"이동 모드  ({hk['move']})", checkable=True)
        self.act_move.triggered.connect(self.set_move_mode)
        menu.addAction(self.act_move)
        menu.addAction(f"보이기/숨기기  ({hk['toggle']})", self.overlay.toggle_visible)
        menu.addSeparator()
        menu.addAction("인식 영역 설정...", self.select_regions)
        menu.addAction("패턴 폴더 열기", lambda: os.startfile(PATTERNS_DIR))
        menu.addAction("설정 파일 열기 (config.json)", lambda: os.startfile(CONFIG_PATH))
        menu.addAction("데이터 폴더 열기 (설정·패턴·로그)", lambda: os.startfile(DATA_DIR))
        menu.addSeparator()
        menu.addAction(f"정보 (v{VERSION})", self.show_about)
        menu.addAction(f"종료  ({hk['quit']})", self.quit)
        self.tray.setContextMenu(menu)
        self._menu = menu
        self.tray.show()

        self.hotkeys = HotkeyThread(self.cfg["hotkeys"])
        self.hotkeys.triggered.connect(self.on_hotkey)
        self.hotkeys.failed.connect(
            lambda c: self.notify(f"단축키 등록 실패: {c} (다른 프로그램이 사용 중일 수 있어요)"))
        self.hotkeys.start()

        self.worker = OcrWorker(self, test_image)
        self.worker.state.connect(self.on_state)
        self.worker.error.connect(self.on_worker_error)
        self.worker.start()

        self._watch = QTimer()
        self._watch.timeout.connect(self.reload_patterns)
        self._watch.start(1000)
        startup_msgs.insert(0, f"실행됨. {hk['move']}: 이동 모드 / {hk['toggle']}: 숨기기 / "
                               f"{hk['quit']}: 종료")
        screen = test_size or primary_screen_size()
        if list(screen) != list(self.cfg["regions_base"]):
            startup_msgs.append(f"해상도 {screen[0]}x{screen[1]}: 인식 위치를 자동 환산했어요. "
                                "안 맞으면 트레이 메뉴 > 인식 영역 설정")
        self.notify("\n".join(startup_msgs))

    def notify(self, msg):
        if hasattr(self, "tray"):
            self.tray.showMessage(APP_NAME, msg, QSystemTrayIcon.Information, 4000)
        log.info(msg.replace("\n", " / "))

    def show_about(self):
        QMessageBox.information(
            None, APP_NAME,
            f"{APP_NAME} v{VERSION}\n\n"
            "화면의 보스 이름과 남은 줄 수를 읽어 미리 적어둔 패턴을 띄워주는 도구입니다.\n"
            "게임 메모리나 입력은 건드리지 않습니다.\n\n"
            "⚠️ 게임사로부터 사용 허가를 받지 않은 프로그램입니다. 게임사 고객센터는 사용 이후 "
            "운영정책 위반이 확인되면 계정에 불이익이 발생할 수 있다고 안내했습니다. "
            "사용에 따른 책임은 사용자 본인에게 있습니다.\n\n"
            f"프로그램 폴더: {BASE}\n"
            f"데이터 폴더(설정·패턴): {DATA_DIR}\n\n"
            "업데이트할 땐 새 버전을 풀고 예전 프로그램 폴더만 지우면 됩니다.\n"
            "(설정과 패턴은 데이터 폴더에 그대로 남습니다)\n"
            "문제가 생기면 데이터 폴더의 log.txt를 함께 보내주세요.")

    def save_config(self):
        write_json(CONFIG_PATH, self.cfg)

    def reload_patterns(self, initial=False):
        """patterns 폴더가 바뀌었으면 다시 읽기. 처음 실행 시엔 알림 대신 메시지 목록 반환."""
        try:
            sig = dir_signature(PATTERNS_DIR)
            if sig == self._patterns_sig and not initial:
                return None
            self._patterns_sig = sig
            self.data, warnings = load_dir(PATTERNS_DIR)
        except Exception as e:  # noqa: BLE001
            msgs = [f"패턴 폴더 읽기 오류: {e}"]
        else:
            msgs = [] if initial else [
                f"패턴 다시 불러옴 (레이드 {len(self.data['raids'])}개, "
                f"보스 {len(all_boss_names(self.data))}개)"]
            msgs += warnings[:5]
            if len(warnings) > 5:
                msgs.append(f"...외 경고 {len(warnings) - 5}개")
            for w in warnings:
                log.warning("패턴 파일: %s", w)
            if not initial:
                self.on_state(self.last_state)
        if initial:
            return msgs
        self.notify("\n".join(msgs))
        return None

    def on_state(self, st):
        self.last_state = st
        boss, bars = st.get("boss"), st.get("bars")
        title, gate = st.get("title"), st.get("gate")
        if self.debug and (st.get("raw_name") is not None or st.get("raw_title") is not None):
            log.debug(f"[OCR] 제목={st.get('raw_title')!r} 이름={st.get('raw_name')!r} "
                      f"줄={st.get('raw_bars')} ???={st.get('hidden')} → {title} {gate}관문 / {boss} x{bars}")
        rows, status = [], ""
        hidden_boss, hidden_pats = (find_hidden_hp_patterns(self.data, boss, title, gate)
                                    if st.get("hidden") else (None, []))
        if hidden_pats:
            # 피통이 ??? → 체력을 알 수 없으므로 % 패턴 전체를 강조 없이 표시
            status = f"{hidden_boss} · 체력 비공개"
            rows = [("pct", p["pct"], p["text"]) for p in hidden_pats]
        elif boss:
            status = f"{boss} · x{bars}" if bars is not None else f"{boss} · 줄 수 인식 중..."
            pats = bar_patterns(find_patterns(self.data, boss, title, gate))
            if bars is not None and pats:
                rows = [(k, p["bar"], p["text"]) for k, p in pattern_window(pats, bars)]
        elif title:
            # 보스 피통이 없으면 정비소로 보고 해당 관문 준비 텍스트 표시
            gate_txt = f"{gate}관문" if gate else "관문 미확인"
            status = f"{title} · {gate_txt} · 정비소"
            rows = [("prep", None, line) for line in prep_lines(self.data, title, gate)]
        self.overlay.set_content(rows, status)

    def on_worker_error(self, msg):
        QMessageBox.critical(None, APP_NAME, msg)
        self.quit()

    def set_move_mode(self, on):
        self.act_move.setChecked(on)
        self.overlay.set_move_mode(on)

    def on_hotkey(self, name):
        if name == "move":
            self.set_move_mode(self.overlay.locked)
        elif name == "toggle":
            self.overlay.toggle_visible()
        elif name == "quit":
            self.quit()

    def select_regions(self):
        self.overlay.hide()
        QTimer.singleShot(200, self._open_selector)

    def _open_selector(self):
        self._selector = RegionSelector()
        self._selector.done.connect(self._regions_done)
        self._selector.setAttribute(Qt.WA_DeleteOnClose)
        self._selector.destroyed.connect(
            lambda: (not self.overlay.user_hidden) and self.overlay.show())

    def _regions_done(self, regions):
        # 지정한 영역은 현재 해상도 좌표 → 나머지 영역도 현재 해상도로 환산해서 기준을 통일
        screen = primary_screen_size()
        merged = effective_regions(self.cfg, screen)
        merged.update(regions)
        self.cfg["regions"] = merged
        self.cfg["regions_base"] = list(screen)
        self.save_config()
        log.info("인식 영역 저장 (%sx%s 기준): %s", *screen, merged)
        self.notify("인식 영역을 저장했습니다.")

    def quit(self):
        self.worker.stop()
        self.hotkeys.stop()
        self.tray.hide()
        self.qapp.quit()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", help="화면 대신 이 스크린샷 파일로 인식 테스트")
    ap.add_argument("--debug", action="store_true", help="OCR 원문을 log.txt(와 콘솔)에 기록")
    args = ap.parse_args()

    qapp = QApplication(sys.argv)
    qapp.setQuitOnLastWindowClosed(False)

    if not prepare_data_dir():
        QMessageBox.critical(None, APP_NAME,
                             f"설정·패턴을 저장할 폴더를 만들 수 없습니다:\n{DATA_DIR}\n\n"
                             "문서 폴더에 쓰기 권한이 있는지 확인해주세요.")
        return
    setup_logging(args.debug)
    log.info("=== %s v%s 시작 / 프로그램: %s / 데이터: %s ===", APP_NAME, VERSION, BASE, DATA_DIR)

    if already_running():
        QMessageBox.information(None, APP_NAME, "이미 실행 중입니다. 작업표시줄 트레이 아이콘을 확인하세요.")
        return

    startup_msgs = prepare_patterns_dir()  # 예전 버전 설정을 옮겨 올 수 있으므로 설정 읽기 전에
    cfg = load_config()
    # 안내 문구가 바뀌면 NOTICE_VERSION을 올려서, 예전 문구에 동의한 사용자도 다시 보게 함
    # (v1.1.0 이하는 accepted_notice=True 로 저장 → 1로 취급)
    if int(cfg.get("accepted_notice") or 0) < NOTICE_VERSION:
        answer = QMessageBox.question(
            None, APP_NAME,
            "⚠️ 이 프로그램은 게임사(스마일게이트)로부터 사용 허가를 받지 않았습니다.\n\n"
            "제작자가 로스트아크 고객센터에 문의한 결과, 이 프로그램이 게임에 어떤 영향을 주는지 "
            "명확하지 않아 허용 여부를 안내하기 어렵고, 사용 이후 운영정책 위반이 확인되면 "
            "계정에 불이익이 발생할 수 있다는 답변을 받았습니다.\n\n"
            "운영정책상 불법 프로그램으로 판단되면 1차부터 영구 이용 정지 대상입니다.\n"
            "(이 프로그램은 화면을 캡처해 글자만 읽으며 게임 메모리·입력은 건드리지 않지만, "
            "이것이 제재받지 않는다는 보장은 아닙니다)\n\n"
            "제재 등 사용으로 인한 모든 결과의 책임은 사용자 본인에게 있습니다.\n"
            "이 내용을 이해했고, 본인 책임으로 사용하시겠습니까?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer != QMessageBox.Yes:
            return
        cfg["accepted_notice"] = NOTICE_VERSION
        write_json(CONFIG_PATH, cfg)

    app = App(qapp, cfg, startup_msgs, test_image=args.test, debug=args.debug)  # noqa: F841
    sys.exit(qapp.exec())


if __name__ == "__main__":
    main()
