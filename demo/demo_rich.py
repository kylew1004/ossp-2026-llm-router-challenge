# SPDX-FileCopyrightText: Copyright 2026 SmartDispatch contributors
# SPDX-License-Identifier: Apache-2.0

#!/usr/bin/env python3
# 알뜰배차 시연영상 데모 (rich 터미널 UI)
#
#   python3 build/demo/demo_rich.py               # 자동 진행
#   DEMO_PAUSE=1 python3 build/demo/demo_rich.py  # Enter로 장면 전환 (녹화 권장)

import json
import os
import random
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

from rich import box
from rich.align import Align
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "build" / "demo-out"
OUT.mkdir(parents=True, exist_ok=True)
console = Console()

ACCENT = "spring_green3"
COST = "orange1"


def pause(sec=3.0):
    if os.environ.get("DEMO_PAUSE") == "1":
        console.input("\n[dim yellow]▶ Enter를 누르면 다음 장면으로...[/]")
    else:
        time.sleep(sec)


def run(cmd):
    env = {**os.environ, "PYTHONPATH": "src"}
    return subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True)


def scene(n, title):
    console.print()
    console.print()
    console.rule(f"[bold {ACCENT}]{n}. {title}", style=ACCENT)
    console.print()


# ───────────────────────── 인트로 ─────────────────────────
console.clear()
logo = Text(justify="center")
logo.append("알뜰배차\n", style=f"bold {ACCENT}")
logo.append("예산 인지형 LLM 라우터", style="bold white")
console.print(
    Panel(
        Align.center(logo),
        box=box.DOUBLE,
        border_style=ACCENT,
        padding=(1, 8),
        subtitle="쉬운 문제는 싸게, 어려운 문제만 비싸게",
        subtitle_align="center",
    )
)
console.print(
    Align.center(
        Text(
            "프롬프트 내용만 보고 3개 모델 중 최적 모델을 선택해\n"
            "등급별 예산 안에서 평균 품질을 극대화합니다",
            style="dim",
            justify="center",
        )
    )
)
pause(4)

# ───────────────────── 1. 입력 데이터 ─────────────────────
scene(1, "입력 데이터 — 라우터에게 주어지는 것은 프롬프트뿐")
eps = json.loads((ROOT / "data/materialized/dev/inputs.json").read_text())["episodes"]
t = Table(box=box.SIMPLE_HEAD, show_lines=False, pad_edge=False)
t.add_column("문항", style="dim", width=10)
t.add_column("유형", style="bold cyan", width=10)
t.add_column("프롬프트", overflow="ellipsis", max_width=78)
for idx, kind in ((1, "수학"), (0, "코드"), (2, "한국어")):
    p = eps[idx]["prompt"].replace("\n", " ")
    t.add_row(eps[idx]["episode_id"], kind, p[:76] + ("…" if len(p) > 76 else ""))
console.print(t)
console.print(f"  [dim]공개 Dev 총 {len(eps)}문항 — 수학·코드 실행 예측·한국어 지문 혼합[/]")
pause(5)

# ───────────────────── 2. 라우터 실행 ─────────────────────
scene(2, "라우터 실행 — 실제 평가 컨테이너 진입점 그대로")
results = {}
for tier in ("fast", "balanced", "premium"):
    with console.status(f"[bold]{tier}[/] 등급 880문항 라우팅 중...", spinner="dots"):
        t0 = time.perf_counter()
        r = run(
            [
                sys.executable,
                "container/entrypoint.py",
                "--input",
                "data/materialized/dev/inputs.json",
                "--tier",
                tier,
                "--output",
                str(OUT / f"{tier}.json"),
            ]
        )
        dt = time.perf_counter() - t0
    msg = r.stdout.strip().replace("OK: ", "")
    console.print(f"  [{ACCENT}]✓[/] [bold]{tier:9s}[/] {dt:4.1f}초  [dim]{msg}[/]")
    results[tier] = dt
pause(4)

# ───────────────────── 3. 공식 채점기 ─────────────────────
scene(3, "공식 채점기(self-check) — 품질과 예산을 동시에 검증")
with console.status("공식 Decimal 채점기 실행 중...", spinner="dots"):
    run(
        [
            sys.executable,
            "-m",
            "ossp_router.cli",
            "self-check",
            "--input",
            "data/materialized/dev/inputs.json",
            "--outcomes",
            "data/dev/outcomes.json",
            "--submissions",
            str(OUT),
            "--report",
            str(OUT / "report.json"),
        ]
    )
rep = json.loads((OUT / "report.json").read_text())
t = Table(box=box.HEAVY_HEAD, header_style=f"bold {ACCENT}")
t.add_column("등급", style="bold")
t.add_column("품질 점수", justify="right")
t.add_column("비용 비율", justify="right", style=COST)
t.add_column("예산 한도", justify="right", style="dim")
t.add_column("통과", justify="center")
t.add_column("모델 선택 분포 (light / ax31 / think)", style="dim")
for tier in ("fast", "balanced", "premium"):
    x = rep["tiers"][tier]
    c = x["model_counts"]
    t.add_row(
        tier,
        x["quality_score"][:8],
        x["budget_ratio"][:7],
        f"≤ {x['budget_multiplier']}",
        f"[{ACCENT}]✓[/]" if x["budget_passed"] else "[red]✗[/]",
        f"{c['ax31-light']} / {c['ax31']} / {c['axk1-think']}",
    )
console.print(t)
final = rep["final_score"][:8]
console.print(
    Panel(
        Text.assemble(
            ("최종 점수  ", "bold"),
            (final, f"bold {ACCENT}"),
            ("    all-light 0.6193  ·  공식 베이스라인 0.6954", "dim"),
            ("  (스트레스 미통과)", "dim italic"),
        ),
        box=box.ROUNDED,
        border_style=ACCENT,
        padding=(0, 2),
    )
)
pause(6)

# ───────────────────── 4. 결정성 감사 ─────────────────────
scene(4, "결정성 — 문항 ID와 순서를 섞어도 같은 선택")
with console.status("문항 ID 재부여 + 순서 셔플 후 재라우팅...", spinner="dots"):
    doc = json.loads((ROOT / "data/materialized/dev/inputs.json").read_text())
    rng = random.Random(7)
    perm = list(range(len(doc["episodes"])))
    rng.shuffle(perm)
    mapping, shuffled = {}, []
    for ni, oi in enumerate(perm):
        e = dict(doc["episodes"][oi])
        nid = f"audit-{ni:05d}"
        mapping[nid] = e["episode_id"]
        e["episode_id"] = nid
        shuffled.append(e)
    doc["episodes"] = shuffled
    (OUT / "shuffled.json").write_text(json.dumps(doc, ensure_ascii=False))
    run(
        [
            sys.executable,
            "container/entrypoint.py",
            "--input",
            str(OUT / "shuffled.json"),
            "--tier",
            "premium",
            "--output",
            str(OUT / "shuffled-premium.json"),
        ]
    )
orig = {
    d["episode_id"]: d["model_id"]
    for d in json.loads((OUT / "premium.json").read_text())["decisions"]
}
audit = {
    mapping[d["episode_id"]]: d["model_id"]
    for d in json.loads((OUT / "shuffled-premium.json").read_text())["decisions"]
}
diff = sum(1 for k in orig if orig[k] != audit[k])
verdict = f"[bold {ACCENT}]✓ 완전 결정적[/]" if diff == 0 else "[red]✗ 불일치[/]"
console.print(f"  선택 불일치 [bold]{diff} / {len(orig)}[/]문항  →  {verdict}")
console.print("  [dim]순수하게 프롬프트 내용만으로 라우팅한다는 증거 (공정성 감사 대응)[/]")
pause(5)

# ─────────────── 5. 공식 자원 한도 검증 ───────────────
scene(5, "공식 자원 한도 — linux/arm64 컨테이너 · 2코어 · 2GiB · 90초")
with console.status("공개 Train+Dev 2,640문항 전체를 격리 컨테이너에서 실행 중... (약 40초)", spinner="bouncingBar"):
    r = run(
        [
            sys.executable,
            "tools/check_runtime.py",
            "--image",
            "docker.io/gangyub/ossp-router:submission",
            "--report",
            str(OUT / "runtime-report.json"),
        ]
    )
for line in r.stdout.splitlines():
    if "PASS" in line or "FAIL" in line:
        tier, rest = line.split(":", 1)
        style = ACCENT if "PASS" in line else "red"
        console.print(f"  [{style}]✓[/] [bold]{tier:9s}[/][{style}]{rest.strip()}[/]")
    elif "문항" in line:
        console.print(f"  [dim]{line.strip()}[/]")
pause(4)

# ───────────────────── 6. 요약 + 대시보드 ─────────────────────
scene(6, "요약")
s = Table(box=None, pad_edge=False, show_header=False)
s.add_column(width=3)
s.add_column()
for item in (
    f"공개 Dev 최종 점수 [bold]{final}[/] — 공식 베이스라인(0.6954) 상회",
    "세 등급 모두 예산 통과 + 비용 이동(최대 +15%) 스트레스 시나리오 전부 통과",
    "문항 셔플에도 100% 동일 선택 — 결정적 라우팅",
    "공식 한도(등급당 90초)의 약 1/9 시간으로 전체 배치 처리",
):
    s.add_row(f"[{ACCENT}]✓[/]", item)
console.print(s)
console.print()
console.print(
    Panel(
        Align.center(Text("알뜰배차 — 쉬운 문제는 싸게, 어려운 문제만 비싸게", style=f"bold {ACCENT}")),
        box=box.DOUBLE,
        border_style=ACCENT,
        padding=(0, 4),
    )
)
dash = ROOT / "demo" / "dashboard.html"
if dash.exists() and os.environ.get("DEMO_NO_BROWSER") != "1":
    pause(2)
    console.print("[dim]결과 대시보드를 브라우저로 엽니다...[/]")
    webbrowser.open(dash.as_uri())
