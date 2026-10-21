"""배포용 빌드: exe 생성 → 배포 폴더 구성 → zip.

사용법 (이 폴더에서):
    .venv\\Scripts\\python.exe build.py
결과: release\\LoaPatternOverlay_v<버전>.zip
"""
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
ASSETS = ROOT / "assets"
WORK = ROOT / "build_out"
RELEASE = ROOT / "release"
NAME = "LoaPatternOverlay"

version = re.search(r'^VERSION = "([^"]+)"', (SRC / "main.py").read_text(encoding="utf-8"), re.M).group(1)
print(f"== {NAME} v{version} 빌드")

if not (ASSETS / "icon.ico").exists():
    subprocess.check_call([sys.executable, str(ASSETS / "make_icon.py")])

shutil.rmtree(WORK, ignore_errors=True)
subprocess.check_call([
    sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
    "--onedir", "--windowed", "--name", NAME,
    "--icon", str(ASSETS / "icon.ico"),
    "--paths", str(SRC),
    "--distpath", str(WORK / "dist"), "--workpath", str(WORK / "work"), "--specpath", str(WORK),
    "--exclude-module", "tkinter", "--exclude-module", "unittest", "--exclude-module", "pydoc",
    str(SRC / "main.py"),
])

app_dir = WORK / "dist" / NAME
# 사용자에게 보이는 파일들 (exe 옆)
shutil.copy(ASSETS / "사용법.txt", app_dir / "사용법.txt")
shutil.copy(ASSETS / "THIRD_PARTY_LICENSES.txt", app_dir / "THIRD_PARTY_LICENSES.txt")
shutil.copy(ROOT / "LICENSE.txt", app_dir / "LICENSE.txt")
# 첫 실행 때 문서\로아 패턴 오버레이\패턴 으로 한 번만 복사되는 패턴
shutil.copytree(ASSETS / "patterns", app_dir / "처음 패턴")
# 설정·패턴·로그는 문서 폴더에 저장되므로 프로그램 폴더에는 넣지 않음

RELEASE.mkdir(exist_ok=True)
zip_path = RELEASE / f"{NAME}_v{version}.zip"
zip_path.unlink(missing_ok=True)
top = f"{NAME}_v{version}"
with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
    for f in sorted(app_dir.rglob("*")):
        if f.is_file():
            z.write(f, f"{top}/{f.relative_to(app_dir).as_posix()}")  # 한글 파일명은 UTF-8로 저장됨

size_mb = zip_path.stat().st_size / 1024 / 1024
print(f"== 완료: {zip_path} ({size_mb:.1f} MB)")
