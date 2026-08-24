<!--
SPDX-FileCopyrightText: Copyright 2026 SmartDispatch contributors
SPDX-License-Identifier: Apache-2.0
-->

# 알뜰배차 시연 도구

라우터의 동작을 눈으로 확인하는 데모와, 제출한 시연 영상을 재현하는
파이프라인입니다. 모든 수치는 스크립트가 실행 시점에 실제로 라우팅·채점한
결과이며, 미리 저장된 값이 아닙니다.

## 준비

공개 Train/Dev 입력이 먼저 필요합니다 (저장소 루트 README의 자료 생성 절차).

```console
pip install rich
```

컨테이너 검증 장면(장면 5)까지 보려면 Docker가 실행 중이어야 하고,
제출 이미지를 로컬에서 빌드해 둡니다.

```console
docker build --platform linux/arm64 --file container/Dockerfile \
  --tag docker.io/gangyub/ossp-router:submission .
```

## 1. 터미널 라이브 데모

```console
python3 demo/demo_rich.py              # 자동 진행 (~80초)
DEMO_PAUSE=1 python3 demo/demo_rich.py # Enter로 장면 전환 (발표·녹화용)
```

여섯 장면: 입력 데이터 → 등급별 라우팅 → 공식 채점기 검증 → 셔플 결정성
→ 공식 자원 한도(90초) 검증 → 요약. 마지막에 결과 대시보드
(`demo/dashboard.html`)가 브라우저로 열립니다.

## 2. 시연 영상 재현

제출한 영상(무음·자막)은 다음 파이프라인으로 만들었습니다.

```console
python3 demo/fetch_font.py     # D2Coding(OFL-1.1) 다운로드 + SHA-256 검증
python3 demo/record_run.py     # 실제 실행 → 장면별 HTML(scenes.json)
python3 demo/build_player.py   # build/demo-video/player.html 생성
```

`build/demo-video/player.html`을 브라우저로 열면 영상과 동일한 재생을
볼 수 있습니다. mp4로 만들려면 헤드리스 크롬으로 프레임을 캡처해
ffmpeg로 조립합니다(우리는 puppeteer-core + `ffmpeg -f concat` 사용,
프레임별 실측 시각 기반).

## 라이선스 메모

- 이 폴더의 코드: Apache-2.0
- D2Coding 폰트: SIL Open Font License 1.1 (커밋하지 않고 스크립트로 수급,
  영상 렌더링에만 사용)
- 대시보드 색상은 다크 배경 기준 색각 이상 구분성 검증(인접쌍 ΔE ≥ 8)을
  통과한 팔레트입니다
