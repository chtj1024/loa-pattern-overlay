# 로아 패턴 오버레이

로스트아크 화면에 뜬 **보스 이름**과 **남은 줄 수(X 205)**를 읽어서, 미리 적어둔 패턴 메모를 게임 화면 위에 띄워주는 Windows 프로그램입니다.
보스가 없을 때(정비소)는 레이드·관문별 준비 메모(아군 위치, 배템 등)를 보여줍니다.

- 화면을 캡처해 Windows 내장 OCR로 글자만 읽습니다. **게임 메모리 읽기, 키 입력, 패킷 분석을 하지 않습니다.**
- 평소에는 클릭이 게임으로 통과되고, 이동 모드(Ctrl+Shift+M)일 때만 드래그로 위치를 옮길 수 있습니다.
- 패턴은 레이드별 메모장 txt 파일로 관리하고, 다른 사람과 파일만 주고받아 공유할 수 있습니다.

> ⚠️ 게임사가 공식 허용한 프로그램이 아닙니다. 사용에 따른 책임은 사용자 본인에게 있습니다.

## 다운로드 / 사용법
[Releases](../../releases)에서 최신 `LoaPatternOverlay_v*.zip`을 받아 압축을 풀고 `LoaPatternOverlay.exe`를 실행하세요.
자세한 사용법은 압축 안의 `사용법.txt`를 참고하세요.

- 게임은 **창 모드 / 전체 창 모드**여야 합니다.
- 1920×1080 기준이며 다른 해상도는 자동 환산합니다. 안 맞으면 트레이 메뉴 → 인식 영역 설정.

## 패턴 파일 예시 (`patterns/레이드이름.txt`)
```
# 종막 : 최후의 날

== 1관문 ==
[정비소]
아군 위치: 1파티 좌측
배템: 회오리 수류탄

[대악마, 카제로스]
420  첫 번째 패턴 설명
     들여쓰면 윗줄에 이어서 줄바꿈
350  두 번째 패턴 설명
```

## 직접 빌드하기 (제작자용)
Python 3.12 (Windows) 필요.
```
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe build.py
```
결과물: `release\LoaPatternOverlay_v<버전>.zip`

소스로 바로 실행: `.venv\Scripts\python.exe src\main.py --debug`

## 구성
| 파일 | 내용 |
|---|---|
| `src/main.py` | 오버레이 창, 트레이, 단축키, 화면 캡처 루프 |
| `src/ocr.py` | Windows OCR 호출, 보스 이름/줄 수/관문 인식 |
| `src/patterns.py` | 패턴 txt 파일 읽기, 표시할 패턴 고르기 |
| `build.py` | PyInstaller로 exe 생성 + 배포 zip |
| `assets/` | 아이콘, 사용법, 예시 패턴, 라이선스 고지 |

## 라이선스
**All rights reserved.** 공식 배포 파일을 수정 없이 개인적으로 사용하는 것과 소스 열람만 허용되며,
수정·재배포·영리 이용은 제작자의 허락 없이 금지됩니다. 자세한 내용은 [LICENSE.txt](LICENSE.txt)를 참고하세요.

사용한 오픈소스 라이선스: `assets/THIRD_PARTY_LICENSES.txt`
