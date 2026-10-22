"""Windows 내장 OCR(Windows.Media.Ocr) 래퍼 + 로스트아크 보스 UI 전용 파싱."""
import asyncio
import re
from collections import Counter
from difflib import SequenceMatcher

import numpy as np
from PIL import Image, ImageOps

from winrt.windows.globalization import Language
from winrt.windows.graphics.imaging import BitmapAlphaMode, BitmapPixelFormat, SoftwareBitmap
from winrt.windows.media.ocr import OcrEngine
from winrt.windows.storage.streams import DataWriter


def _make_engine(tag):
    lang = Language(tag)
    if not OcrEngine.is_language_supported(lang):
        return None
    return OcrEngine.try_create_from_language(lang)


def normalize(text: str) -> str:
    """비교용: 한글/영문/숫자만 남김."""
    return re.sub(r"[^0-9A-Za-z가-힣]", "", text)


def _to_bw(img: Image.Image, mode: str, scale: int) -> Image.Image:
    a = np.asarray(img.convert("RGB")).astype(np.int16)
    if mode == "white":      # 흰 글씨 (줄 수)
        out = np.where(a.min(axis=2) > 140, 0, 255).astype(np.uint8)
    elif mode == "red":      # 붉은 글씨 (보스 이름)
        r, g, b = a[..., 0], a[..., 1], a[..., 2]
        out = np.where((r > 100) & (r - g > 30) & (r - b > 30), 0, 255).astype(np.uint8)
    else:                    # 가장 밝은 채널 반전 (색 무관)
        out = (255 - a.max(axis=2)).astype(np.uint8)
    pil = Image.fromarray(out).resize((img.width * scale, img.height * scale), Image.LANCZOS)
    return ImageOps.expand(pil, border=30, fill=255)


_DIGIT_FIX = str.maketrans({"O": "0", "o": "0", "D": "0", "I": "1", "l": "1", "|": "1",
                            "S": "5", "s": "5", "B": "8", "Z": "2", "z": "2"})


def parse_bars(text: str):
    t = text.translate(_DIGIT_FIX)
    m = re.search(r"[Xx×]\s*(\d{1,4})", t) or re.search(r"(\d{1,4})", t)
    return int(m.group(1)) if m else None


class Ocr:
    def __init__(self):
        self.ko = _make_engine("ko")
        if self.ko is None:
            raise RuntimeError(
                "Windows 한국어 OCR이 없습니다.\n설정 > 시간 및 언어 > 언어 및 지역 > 한국어 > "
                "언어 옵션에서 '광학 문자 인식'을 설치하세요."
            )
        self.en = _make_engine("en-US") or self.ko
        self._loop = asyncio.new_event_loop()

    def recognize(self, engine, img: Image.Image) -> str:
        img = img.convert("RGBA")
        writer = DataWriter()
        writer.write_bytes(img.tobytes())
        bmp = SoftwareBitmap.create_copy_with_alpha_from_buffer(
            writer.detach_buffer(), BitmapPixelFormat.RGBA8, img.width, img.height,
            BitmapAlphaMode.PREMULTIPLIED,
        )

        async def run():
            return await engine.recognize_async(bmp)

        result = self._loop.run_until_complete(run())
        return " ".join(line.text for line in result.lines)

    def read_name(self, img: Image.Image, candidates, modes=("gray", "red")):
        """글자를 읽어 candidates 중 가장 비슷한 것을 반환. (이름|None, 점수, 원문)"""
        best = (None, 0.0, "")
        for mode in modes:
            raw = self.recognize(self.ko, _to_bw(img, mode, 3))
            got = normalize(raw)
            if not got:
                continue
            for cand in candidates:
                score = SequenceMatcher(None, got, normalize(cand)).ratio()
                if score > best[1]:
                    best = (cand, score, raw)
            if best[1] >= 0.85:
                break
            if best[0] is None:
                best = (None, 0.0, raw)
        return best

    def read_title(self, img: Image.Image, candidates):
        """왼쪽 위 콘텐츠 제목 (예: 종막 : 최후의 날)."""
        return self.read_name(img, candidates, modes=("white", "gray"))

    @staticmethod
    def read_gate(img: Image.Image):
        """관문 진행 표시에서 초록 체크(V) 개수를 세어 현재 관문 번호를 반환.
        관문 표시가 접혀 있어 판단할 수 없으면 None."""
        a = np.asarray(img.convert("RGB")).astype(np.int16)
        r, g, b = a[..., 0], a[..., 1], a[..., 2]
        green = (g > 150) & (g - r > 50) & (g - b > 60)
        blue = (b > 200) & (b - r > 100)
        if green.sum() < 20 and blue.sum() < 200:
            return None
        # 초록 픽셀이 있는 열을 가로로 묶어 체크 표시 개수 세기
        cols = np.nonzero(green.sum(axis=0) >= 1)[0]
        checks, run_start, prev = 0, None, None
        for c in cols:
            if prev is None or c - prev > 8:
                if run_start is not None and prev - run_start >= 6:
                    checks += 1
                run_start = c
            prev = c
        if run_start is not None and prev - run_start >= 6:
            checks += 1
        return checks + 1

    @staticmethod
    def is_hidden_hp_bar(img: Image.Image) -> bool:
        """피통이 ??? 상태(체력 비공개)인지. 이때 막대는 색 없이 고른 회색이다.
        (일반 피통은 빨강/주황/초록 등 색이 진하고, 게임 화면은 밝기가 고르지 않음)"""
        a = np.asarray(img.convert("RGB")).astype(np.int16)
        if a.size == 0:
            return False
        saturation = float((a.max(axis=2) - a.min(axis=2)).mean())
        lum = a.mean(axis=2)
        chunks = np.array_split(lum.mean(axis=0), 10)  # 가로 10구간 평균 밝기
        evenness = float(np.std([c.mean() for c in chunks]))
        return saturation < 12 and evenness < 10 and 35 < float(lum.mean()) < 160

    def read_bars(self, img: Image.Image):
        """줄 수 영역을 여러 방식으로 읽고 다수결. (줄수|None, 원문들)"""
        votes, raws = [], []
        for engine, scale in ((self.en, 2), (self.ko, 2), (self.ko, 4)):
            raw = self.recognize(engine, _to_bw(img, "white", scale))
            raws.append(raw)
            v = parse_bars(raw)
            if v is not None:
                votes.append(v)
        if not votes:
            return None, raws
        value, count = Counter(votes).most_common(1)[0]
        if count == 1 and len(votes) > 1:
            return None, raws  # 의견이 모두 다르면 신뢰 안 함
        return value, raws
