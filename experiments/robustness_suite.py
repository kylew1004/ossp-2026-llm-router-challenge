# SPDX-FileCopyrightText: Copyright 2026 SmartDispatch contributors
# SPDX-License-Identifier: Apache-2.0

"""비공개 채점셋 강건성 스위트 — 예산 초과(등급 0점) 리스크 측정 도구.

공개 Dev를 재조합해 '비공개 채점셋이 다를 수 있는 축'을 시뮬레이션한다.

  1. 크기별 무작위 부분집합 (n=250/440/660 × 각 50회): 표본 변동
  2. 동일 크기 부트스트랩 (880 복원추출 × 50회): 크기 효과를 제거한 표본 변동
  3. Dirichlet 도메인 재구성 (100회): 수학/코드/한국어/기타 비율 변화

각 셋을 두 비용 시나리오(기준 / 이동: light -5%, ax31 +10%, think +15%)로
평가한다. 배포 안전계수 {fast 0.86, balanced 0.78, premium 0.58}(+premium
fill 0.40)는 이 스위트로 선택했다: 시드 4종(42/7/123/2026) × 300셋에서
기준 시나리오 실패 0/1200, 이동 시나리오 실패 4/1200(모두 이중 극단 조건).
공개 Dev 최종 점수 비용은 0.6966 → 0.6773.

사용법 (공개 자료 생성 후):
  PYTHONPATH=src python3 experiments/robustness_suite.py
  PYTHONPATH=src python3 experiments/robustness_suite.py \
      --safety 1.0 0.985 0.83 --fill 0.65   # 이전(공격적) 세팅과 비교
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from ossp_router import hashregex_router as hr  # noqa: E402
from ossp_router.protocol import parse_input  # noqa: E402

RATES = {"ax31-light": (1.0, 4.0), "ax31": (2.127, 8.509), "axk1-think": (6.565, 26.260)}
MODELS = ["ax31-light", "ax31", "axk1-think"]
CAPS = {"fast": 1.25, "balanced": 2.0, "premium": 4.0}
SCEN = {
    "base": {"ax31-light": 1.0, "ax31": 1.0, "axk1-think": 1.0},
    "shift": {"ax31-light": 0.95, "ax31": 1.10, "axk1-think": 1.15},
}
MATH = re.compile(r"\*\*|\b(?:Let|Suppose|Find|Solve|Calculate|derivative|Simplify|prime|remainder)\b")
CODE = re.compile(r"\bdef |return|```|print\(")


def domain(p: str) -> str:
    hangul = sum(1 for ch in p if "가" <= ch <= "힣") / max(len(p), 1)
    if hangul > 0.15:
        return "korean"
    if CODE.search(p):
        return "code"
    if MATH.search(p):
        return "math"
    return "other"


def make_subsets(n: int, doms: np.ndarray, seed: int):
    rng = np.random.RandomState(seed)
    subsets = []
    for size in (250, 440, 660, 880):
        for _ in range(50):
            subsets.append(
                np.unique(rng.randint(0, n, n)) if size == n
                else rng.choice(n, size, replace=False))
    names = np.array(["math", "code", "korean", "other"])
    idx = {d: np.where(doms == d)[0] for d in names}
    for _ in range(100):
        props = rng.dirichlet((2, 2, 2, 2))
        take = []
        for d, pr in zip(names, props):
            k = min(int(round(pr * 500)), len(idx[d]))
            if k > 0:
                take.append(rng.choice(idx[d], k, replace=False))
        subsets.append(np.concatenate(take))
    return subsets


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--safety", nargs=3, type=float, default=[0.86, 0.78, 0.58],
                    metavar=("FAST", "BALANCED", "PREMIUM"))
    ap.add_argument("--fill", type=float, default=0.40)
    ap.add_argument("--seeds", nargs="+", type=int, default=[42, 7, 123, 2026])
    args = ap.parse_args()
    safety = dict(zip(("fast", "balanced", "premium"), args.safety))

    inputs = parse_input(json.loads(
        (ROOT / "data/materialized/dev/inputs.json").read_text()))
    out_doc = json.loads((ROOT / "data/dev/outcomes.json").read_text())
    outc = {e["episode_id"]: e["models"] for e in out_doc["episodes"]}
    artifact = hr.load_artifact(
        ROOT / "src/ossp_router/resources/router-artifact.v1.json")
    pred = [hr.predict_episode(e, artifact) for e in inputs.episodes]
    p_scores = [p[0] for p in pred]
    p_costs = [p[1] for p in pred]
    ids = [e.episode_id for e in inputs.episodes]
    doms = np.array([domain(e.prompt) for e in inputs.episodes])
    actual = {}
    for scen, f in SCEN.items():
        a = np.zeros((len(ids), 3))
        for i, eid in enumerate(ids):
            for j, m in enumerate(MODELS):
                r = outc[eid][m]
                ot = round(r["output_tokens"] * f[m])
                a[i, j] = (r["input_tokens"] * RATES[m][0] + ot * RATES[m][1]) / 1e6
        actual[scen] = a

    def select(idx):
        ps = [p_scores[i] for i in idx]
        pc = [p_costs[i] for i in idx]
        sels = {}
        for tier in ("fast", "balanced", "premium"):
            sel, _ = hr.select_models(
                ps, pc, budget_multiplier=CAPS[tier], safety_ratio=safety[tier])
            if tier == "premium":
                sel, _ = hr.fill_ax31_upgrades(
                    sel, ps, pc, budget_multiplier=CAPS[tier],
                    safety_ratio=args.fill)
            sels[tier] = np.array([MODELS.index(m) for m in sel])
        return sels

    print(f"안전계수 {safety}, premium fill {args.fill}")
    grand = {"base": 0, "shift": 0}
    total = 0
    for seed in args.seeds:
        subsets = make_subsets(len(ids), doms, seed)
        total += len(subsets)
        fails = {"base": 0, "shift": 0}
        wm = {"base": 1e9, "shift": 1e9}
        for idx in subsets:
            sels = select(idx)
            for scen, a in actual.items():
                sub = a[idx]
                light = sub[:, 0].sum()
                mm = min(
                    CAPS[t] - sub[np.arange(len(idx)), sels[t]].sum() / light
                    for t in sels)
                wm[scen] = min(wm[scen], mm)
                if mm < -1e-6:
                    fails[scen] += 1
        for scen in fails:
            grand[scen] += fails[scen]
        print(f"  seed{seed}: 기준 실패 {fails['base']}/{len(subsets)} "
              f"(최악 {wm['base']:+.3f}) · 이동 실패 {fails['shift']}/{len(subsets)} "
              f"(최악 {wm['shift']:+.3f})")
    print(f"합계: 기준 {grand['base']}/{total} · 이동 {grand['shift']}/{total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
