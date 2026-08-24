<!--
SPDX-FileCopyrightText: Copyright 2026 SmartDispatch contributors
SPDX-License-Identifier: Apache-2.0
-->

# 검증 실험 도구

배포 안전계수를 선택한 강건성 스위트와, 그 배경이 된 방법론 요약입니다.
모든 수치는 공개 자료와 공식 채점 규칙만으로 재현됩니다.

## 강건성 스위트 (robustness_suite.py)

비공개 채점셋이 공개 Dev와 다를 수 있는 세 축 — 문항 수, 표본, 도메인
구성 — 을 300개 가상 채점셋(무작위 부분집합 200 + 부트스트랩 50 +
Dirichlet 도메인 재구성 100)으로 시뮬레이션하고, 각 셋을 기준/비용 이동
(light −5%, ax31 +10%, think +15%) 두 시나리오로 평가합니다.

```console
PYTHONPATH=src python3 experiments/robustness_suite.py
PYTHONPATH=src python3 experiments/robustness_suite.py --safety 1.0 0.985 0.83 --fill 0.65
```

두 번째 명령(공개 Dev에 맞춰 공격적으로 보정한 세팅)은 기준 시나리오에서만
약 22%의 가상 채점셋에서 예산을 초과합니다. 공식 베이스라인이 공개 Dev
비용비 3.985로 보정되었다가 실제 채점셋에서 약 4.2를 기록해 Premium 0점
처리된 전례(baselines/README.md)와 같은 실패 양식입니다. 배포 세팅
{0.86, 0.78, 0.58}은 시드 4종 × 300셋에서 기준 실패 0/1,200입니다.

## 방법론 요약과 기각 기록

- 품질 예측: 선형(ridge)·GBDT·kNN·사전학습 임베딩(multilingual-e5-small)
  네 계열과 블렌딩, 학습 데이터 증량(train+dev 합산)을 동일 프로토콜로
  비교했으나 모두 배포 구성 대비 이득이 없어 기각했습니다.
- 비용 예측의 도메인 편향(수학 문항 실제 비용이 예측의 2배 이상)을
  규명했으나, 도메인별 보정계수는 강건성 기준을 맞추도록 조이면 무보정
  안전계수보다 품질이 낮아져(지배됨) 기각했습니다.
- 결론: 단순한 선형 모델 + 넉넉한 안전계수가 시도한 모든 대안을
  파레토 지배합니다. 남은 오차는 프롬프트 내용만으로 예측 불가능한
  모델의 확률적 성공/실패 영역입니다.
