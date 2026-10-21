"""patterns 폴더의 레이드별 .txt 파일 읽기/쓰기.

형식 (메모장으로 편집):

    # 종막 : 최후의 날            ← 콘텐츠 제목 (왼쪽 위에 뜨는 그대로)

    [정비소]                      ← 관문 머리말 전에 쓰면 모든 관문 공통
    공통 배템: 회오리 수류탄

    == 1관문 ==
    [정비소]
    아군 위치: 1파티 좌측
        (들여쓰면 윗줄에 이어서 줄바꿈)

    [심연의 군주, 카제로스]        ← 보스 이름 (피통 위에 뜨는 그대로)
    420  부메랑던짐 - 부메랑 4갈래
         * 파란 구슬 : 출혈 디버프 없애줌
    350  350줄 무덤 : ...

  - 숫자로 시작하는 줄 = 새 패턴 (그 숫자 = 남은 줄 수)
  - 들여쓴 줄 = 윗줄 텍스트에 이어서 줄바꿈
  - // 로 시작하는 줄 = 메모(무시)
  - '# 제목'이 없는 파일의 보스는 어느 레이드에서든 사용
  - 파일 이름이 _ 로 시작하면 읽지 않음
"""
import json
import re
from pathlib import Path

GATE_RE = re.compile(r"^==\s*(\d+)\s*관문\s*==$")
SECTION_RE = re.compile(r"^\[(.+)\]$")
PATTERN_RE = re.compile(r"^[xX]?(\d+)(?:\s+(.*))?$")
PREP = "정비소"

GUIDE_TEXT = __doc__.split("형식 (메모장으로 편집):", 1)[1].rstrip() + "\n"


def empty_data():
    return {"bosses": {}, "raids": {}}


def _read_text(path: Path) -> str:
    raw = path.read_bytes()
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp949")  # 메모장 'ANSI'로 저장한 경우


def _slot(data, title, gate):
    gates = data["raids"].setdefault(title, {})
    return gates.setdefault(gate, {"prep": [], "bosses": {}})


def parse_file(path: Path, data, warnings):
    title, gate, section = None, 0, None
    prep, pats = None, None
    for no, raw in enumerate(_read_text(path).splitlines(), start=1):
        line = raw.rstrip()
        s = line.strip()
        if not s or s.startswith("//"):
            continue
        indented = line[0] in " \t　"
        where = f"{path.name} {no}번째 줄"

        if not indented:
            if s.startswith("#"):
                title, gate, section = s.lstrip("#").strip(), 0, None
                continue
            m = GATE_RE.match(s)
            if m:
                if title is None:
                    warnings.append(f"{where}: '# 제목' 없이 관문이 나옴 (무시)")
                gate, section = int(m.group(1)), None
                continue
            m = SECTION_RE.match(s)
            if m:
                name = m.group(1).strip()
                if name == PREP:
                    if title is None:
                        warnings.append(f"{where}: '# 제목' 없는 파일의 [정비소]는 쓸 수 없음")
                        section = None
                        continue
                    section, prep = PREP, _slot(data, title, gate)["prep"]
                else:
                    bosses = data["bosses"] if title is None else _slot(data, title, gate)["bosses"]
                    section, pats = name, bosses.setdefault(name, [])
                continue

        if section is None:
            warnings.append(f"{where}: [정비소]나 [보스 이름] 아래에 있지 않은 줄 (무시)")
        elif section == PREP:
            if indented and prep:
                prep[-1] += "\n" + s
            else:
                prep.append(s)
        else:
            m = None if indented else PATTERN_RE.match(s)
            if m:
                pats.append({"bar": int(m.group(1)), "text": (m.group(2) or "").strip()})
            elif pats:
                pats[-1]["text"] += ("\n" if pats[-1]["text"] else "") + s
            else:
                warnings.append(f"{where}: 줄 수(숫자)로 시작하는 패턴이 먼저 와야 함 (무시)")


def load_dir(folder: Path):
    """폴더의 *.txt를 모두 읽어 (data, warnings) 반환."""
    data, warnings = empty_data(), []
    for path in sorted(folder.glob("*.txt")):
        if path.name.startswith("_"):
            continue
        parse_file(path, data, warnings)
    for gates in data["raids"].values():
        for g in gates.values():
            for pats in g["bosses"].values():
                pats.sort(key=lambda p: -p["bar"])
    for pats in data["bosses"].values():
        pats.sort(key=lambda p: -p["bar"])
    return data, warnings


def dir_signature(folder: Path):
    return tuple(sorted((p.name, p.stat().st_mtime) for p in folder.glob("*.txt")))


# ---------------------------------------------------------------- 조회
def all_boss_names(data):
    names = set(data["bosses"])
    for gates in data["raids"].values():
        for g in gates.values():
            names.update(g["bosses"])
    return sorted(names)


def find_patterns(data, boss, title, gate):
    """현재 레이드/관문 → 같은 레이드 → 공통 → 다른 레이드 순으로 패턴 찾기."""
    gates = data["raids"].get(title, {})
    if gate in gates and boss in gates[gate]["bosses"]:
        return gates[gate]["bosses"][boss]
    for g in gates.values():
        if boss in g["bosses"]:
            return g["bosses"][boss]
    if boss in data["bosses"]:
        return data["bosses"][boss]
    for gs in data["raids"].values():
        for g in gs.values():
            if boss in g["bosses"]:
                return g["bosses"][boss]
    return None


def prep_lines(data, title, gate):
    """공통 [정비소] + 현재 관문 [정비소]. 관문을 모르면 가장 앞 관문."""
    gates = data["raids"].get(title, {})
    real = sorted(g for g in gates if g > 0)
    if gate is None and real:
        gate = real[0]
    lines = list(gates.get(0, {}).get("prep", []))
    if gate and gate in gates:
        lines += gates[gate]["prep"]
    return lines


def pattern_window(patterns, bars, before=2, after=2):
    """현재 줄 수 기준 [이전 before개, 진행 중인 패턴(강조), 그 다음 after개]. (kind, pattern) 목록.

    진행 중인 패턴 = 현재 줄 수보다 큰 것 중 가장 작은 패턴.
    예) 패턴 250/150, 현재 200 → 250 강조 / 현재 150 → 250 강조 / 현재 149 → 150 강조
    첫 패턴 전(현재 줄 수 >= 첫 패턴)에는 첫 패턴을 강조.
    """
    passed = sum(1 for p in patterns if p["bar"] > bars)  # 내림차순 정렬 기준
    cur = max(0, passed - 1)
    rows = []
    for i in range(cur - before, cur + after + 1):
        if 0 <= i < len(patterns):
            kind = "prev" if i < cur else ("next" if i == cur else "after")
            rows.append((kind, patterns[i]))
    return rows


# ---------------------------------------------------------------- 쓰기 (변환용)
def _text_block(first_prefix, text, indent):
    parts = text.split("\n")
    out = [f"{first_prefix}{parts[0].strip()}".rstrip()]
    out += [f"{indent}{p.strip()}" for p in parts[1:] if p.strip()]
    return out


def _boss_block(name, pats):
    lines = [f"[{name}]"]
    for p in sorted(pats, key=lambda p: -p["bar"]):
        lines += _text_block(f"{p['bar']:<5}", p["text"], " " * 5)
    return lines + [""]


def _prep_block(prep):
    lines = [f"[{PREP}]"]
    for item in prep:
        lines += _text_block("", item, "    ")
    return lines + [""]


def raid_to_text(title, gates):
    lines = [f"# {title}", ""]
    for g in sorted(gates):
        slot = gates[g]
        if g > 0:
            lines += [f"== {g}관문 ==", ""]
        if slot["prep"]:
            lines += _prep_block(slot["prep"])
        for name, pats in slot["bosses"].items():
            lines += _boss_block(name, pats)
    return "\n".join(lines).rstrip() + "\n"


def bosses_to_text(bosses):
    lines = ["// 어느 레이드에서든 쓰는 보스 패턴 ('# 제목' 없음)", ""]
    for name, pats in bosses.items():
        lines += _boss_block(name, pats)
    return "\n".join(lines).rstrip() + "\n"


def safe_filename(title):
    name = re.sub(r'[\\/:*?"<>|]', " ", title)
    return re.sub(r"\s+", " ", name).strip() or "레이드"


def migrate_json(json_path: Path, folder: Path):
    """예전 bosses.json → patterns/*.txt. 만든 파일 이름 목록 반환."""
    entries = json.loads(json_path.read_text(encoding="utf-8-sig"))
    folder.mkdir(exist_ok=True)
    written = []
    common = {}
    for e in entries:
        if "title" in e:
            gates = {}
            for g in e.get("gates", []):
                prep = g.get("prep", [])
                gates[int(g["gate"])] = {
                    "prep": [prep] if isinstance(prep, str) else list(prep),
                    "bosses": {b["name"]: b["patterns"] for b in g.get("bosses", [])},
                }
            path = folder / f"{safe_filename(e['title'])}.txt"
            path.write_text(raid_to_text(e["title"], gates), encoding="utf-8")
            written.append(path.name)
        else:
            common[e["name"]] = e["patterns"]
    if common:
        path = folder / "공통 보스.txt"
        path.write_text(bosses_to_text(common), encoding="utf-8")
        written.append(path.name)
    return written
