<!--
SPDX-FileCopyrightText: Copyright 2026 SmartDispatch contributors
SPDX-License-Identifier: Apache-2.0
-->

# 알뜰배차 개발 문서

설계 결정과 그 근거, 구현 구조, 재현 절차를 한 문서로 정리합니다.
모든 수치는 공개 자료와 공식 채점기로 재현할 수 있습니다.

## 1. 문제 정의

라우터는 문항마다 프롬프트 내용만 보고 세 모델(`ax31-light` /
`ax31` / `axk1-think`) 중 하나를 고릅니다. 모델을 실제로 호출하지 않으며,
운영자가 미리 계산한 결과와 선택을 결합해 채점합니다.

- 비용(모델별, 백만 토큰당): light 1/4, ax31 2.127/8.509, think 6.565/26.260 —
  think는 출력이 길어 실효 비용이 light의 약 23배입니다.
- 등급별 예산: 전량 light 비용 대비 Fast 1.25배 / Balanced 2.0배 /
  Premium 4.0배. **한도를 조금이라도 넘으면 해당 등급이 0점**입니다.
- 최종 점수 = 0.4×Fast + 0.3×Balanced + 0.3×Premium (등급 품질 평균).

따라서 이 문제는 "품질 예측 + 예산 제약 최적화"이고, 가장 치명적인 실패는
예산 초과입니다. 공식 베이스라인이 공개 Dev 비용비 3.985로 보정했다가 실제
채점셋에서 약 4.2를 기록해 Premium 0점 처리된 전례(baselines/README.md)가
설계의 출발점이었습니다.

## 2. 시스템 아키텍처

```
inputs.json (prompt 또는 messages)
   │  src/ossp_router/heuristic.py: episode_text / extract_features
   ▼
특징 벡터 (2,062차원)
   dense 14종(길이·한글비율·코드/수학 마커·추론 어휘 등)
 + 단어 1–2gram signed feature hashing 2,048 bins (FNV-1a 64bit)
   │  src/ossp_router/hashregex_router.py: predict_episode
   ▼
모델별 예측  품질 3종(clip [0,1]) · log-비용 3종(단조 보정)
   │  select_models: argmax(품질 − λ·비용), λ 이분탐색으로
   │  cap = 예측 light합 × 등급배수 × 안전계수 에 정합
   │  premium은 fill_ax31_upgrades로 잔여 예산을 ax31 승급에 사용
   ▼
submission.json (등급별, 원자적 쓰기·UTF-8·모든 문항 정확히 1회)
```

- 추론은 파이썬 표준 라이브러리만 사용합니다(서드파티 무의존). 공개
  Dev 880문항 기준 약 2.5초, 공식 격리 컨테이너에서 등급당 약 9.5초입니다.
- 선택은 입력 순서·문항 ID와 무관한 결정적 알고리즘입니다. ID 재부여와
  순서 셔플 후에도 880문항×3등급의 선택이 100% 일치함을 확인했습니다.
- 학습된 계수는 JSON 아티팩트
  (`src/ossp_router/resources/router-artifact.v1.json`)로 배포합니다 —
  바이너리 없이 저장소에서 내용을 직접 검증할 수 있습니다.

## 3. 학습 절차

```console
python3 -m venv .venv-data
.venv-data/bin/pip install -r data/sources/requirements-materialize-public-data.txt
.venv-data/bin/python tools/materialize_public_data.py

pip install numpy
PYTHONPATH=src:baselines python3 baselines/train_hash_regex.py \
  --input data/materialized/train/inputs.json \
  --outcomes data/train/outcomes.json \
  --validation-input data/materialized/dev/inputs.json \
  --validation-outcomes data/dev/outcomes.json \
  --artifact build/artifact.json --report build/report.json \
  --hash-bins 2048 --alphas 1000,1700,3000,5600,10000
```

- ridge 정규화 강도는 out-of-fold 교차검증으로 선택합니다. 공개 학습기의
  기본 탐색 범위(alpha ≤ 100)는 2,048차원 해시 공간에서 과소정규화 상태라,
  1,000–10,000 영역을 탐색해 alpha 3,000을 선택했습니다.
- 학습기가 산출한 Dev 최적 안전계수는 그대로 쓰지 않고, 4절의 강건성
  스위트로 클램프한 값으로 교체합니다.

## 4. 예산 안전계수 — 강건성 우선 방법론

비공개 채점셋이 공개 Dev와 다를 수 있는 축(문항 수·표본·도메인 구성·모델별
비용 수준)을 300개 가상 채점셋 × 2개 비용 시나리오로 모사했습니다
(`experiments/robustness_suite.py`).

| 안전계수 | 공개 Dev | 기준 시나리오 실패 | 비용 이동 결합 실패 |
| --- | ---: | ---: | ---: |
| Dev 최적(1.0/0.985/0.83) | 0.6966 | 22% | 73% |
| **배포(0.86/0.78/0.58)** | **0.6773** | **0/1,200 (시드 4종)** | 0.3% |

배포값은 "기준 시나리오 전부 통과 + 호스트 실측 수준(+5~7%)의 비용 이동
결합에서도 전부 통과"를 만족하는 구성 중 공개 Dev 점수가 가장 높은
지점입니다. 공개 점수 0.019를 보험료로 지불하고 등급 0점 리스크를 제거하는
선택이며, 그 판단 도구와 수치 전부를 저장소에 공개합니다.

## 5. 검증 체계

| 검증 | 방법 | 결과 |
| --- | --- | --- |
| 점수·예산 | 공식 Decimal 채점기 self-check | 0.677301, 3등급 예산 통과 |
| 자원 한도 | tools/check_runtime.py (2코어·2GiB·90초·네트워크 차단) | 등급당 9.3–9.8초 |
| 결정성 | ID 재부여+순서 셔플 재실행 대조 | 880×3 선택 100% 일치 |
| 재현 빌드 | 새 클론 → 고정 커밋 → docker build | 제출 이미지와 image ID 동일 |
| 입력 형식 | messages형·초장문·유니코드·초단문 합성 배치 | 전 등급 정상 처리 |
| 강건성 | 300 가상 채점셋 × 시드 4종 × 2 시나리오 | 기준 0/1,200 실패 |

## 6. 실험 기록 (채택·기각)

같은 프로토콜(공식 채점기 + 강건성 클램프)로 비교한 결과입니다.

| 시도 | 공개 Dev | 판정 |
| --- | ---: | --- |
| hash 2,048 + alpha 3,000 (배포) | 0.6773 | 채택 |
| 문자 3–4gram 해시 · gain head · 정규화 분리 | 0.689–0.695 | 기각 |
| GBDT (2개 설정) | ≤ 0.6962 | 기각 |
| TF-IDF kNN (k=15/40/80) 및 블렌딩 | ≤ 0.6964 | 기각 |
| 사전학습 임베딩 (multilingual-e5-small) | ≤ 0.6970* | 기각 (*강건성 미달) |
| 학습 데이터 증량 (train+dev 합산) | 선택 후회 개선 없음 | 기각 |
| 도메인별 비용 보정 | 강건성 충족 시 0.6713–0.6760 | 기각 (지배됨) |

네 가지 모델 계열과 데이터 증량, 비용 보정이 모두 배포 구성을 넘지
못했습니다. 남은 오차는 프롬프트 내용만으로 예측할 수 없는 모델의 확률적
성공/실패 영역이라고 판단합니다. 상세 수치는
[`experiments/README.md`](experiments/README.md)를 참고하십시오.

## 7. 저장소 구조

| 경로 | 내용 |
| --- | --- |
| `src/ossp_router/hashregex_router.py` | 배포 라우터 (표준 라이브러리 추론) |
| `src/ossp_router/resources/router-artifact.v1.json` | 학습된 계수 + 안전계수 (JSON) |
| `container/` | 평가 컨테이너 (entrypoint → hashregex_router) |
| `experiments/` | 강건성 스위트와 방법론·기각 기록 |
| `demo/` | 터미널 라이브 데모와 시연 영상 재현 파이프라인 |
| `baselines/`, `docs/`, `tools/` | 과제(fork 원본) 제공 자료 |

## 8. 한계와 로드맵

- 과제 규격상 모델 출력을 볼 수 없어 사전 예측 라우팅만 구현했습니다.
  실서비스에서는 저비용 모델 출력의 품질을 추정해 승급하는 cascade 구조로
  확장할 계획입니다.
- 학습 데이터가 확충되면 임베딩 백본을 재검토합니다(기각 근거와 재시도
  기준은 6절과 experiments/README.md 참조).
- 수학 도메인의 비용 과소예측(실제의 2배 이상)을 규명했으며, 분위수 회귀
  등으로 해소하는 것이 남은 과제입니다.

세부 로드맵은 저장소 GitHub Issues에서 관리합니다.
