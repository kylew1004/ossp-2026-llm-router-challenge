# SPDX-FileCopyrightText: Copyright 2026 SmartDispatch contributors
# SPDX-License-Identifier: Apache-2.0

# scenes.json + dashboard.html → player.html (자체 완결 재생 페이지)
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE.parent / 'build' / 'demo-video'
OUT.mkdir(parents=True, exist_ok=True)
scenes = json.loads((OUT / "scenes.json").read_text())
DASH_DUR = 18

TEMPLATE = """<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<title>알뜰배차 시연</title>

<style>
  @font-face { font-family: 'D2Coding'; src: url('D2Coding.ttf'); font-weight: normal; }
  @font-face { font-family: 'D2Coding'; src: url('D2CodingBold.ttf'); font-weight: bold; }
  * { box-sizing: border-box; margin: 0; }
  html, body { width: 1280px; height: 800px; overflow: hidden; }
  body { background: #111411; font-family: -apple-system, "Apple SD Gothic Neo", sans-serif; }

  #term {
    position: absolute; inset: 18px 24px 96px 24px;
    background: #171b17; border: 1px solid #2c332c; border-radius: 12px;
    display: flex; flex-direction: column; overflow: hidden;
    box-shadow: 0 12px 40px rgba(0,0,0,.5);
  }
  #bar {
    height: 36px; background: #1f241f; display: flex; align-items: center;
    padding: 0 14px; gap: 7px; border-bottom: 1px solid #2c332c; flex: none;
  }
  #bar i { width: 12px; height: 12px; border-radius: 50%; }
  #bar .t { margin-left: 10px; color: #9aa79a; font-size: 12.5px; }
  #screen {
    flex: 1; overflow: hidden; padding: 16px 20px;
    display: flex; flex-direction: column; justify-content: flex-end;
  }
  #screen pre {
    font-family: 'D2Coding', Menlo, monospace !important;
    font-size: 15px;
    line-height: 1.45; white-space: pre;
    background: transparent !important;
  }
  .scene { animation: rise .45s ease-out both; }
  @keyframes rise { from { opacity: 0; transform: translateY(14px); } to { opacity: 1; } }

  #dash {
    position: absolute; inset: 18px 24px 96px 24px; border: none; border-radius: 12px;
    width: 1232px; height: 686px; display: none; background: #1a1a19;
    box-shadow: 0 12px 40px rgba(0,0,0,.5);
  }
  /* 대시보드(1280x800 설계)를 1232x686 영역에 축소 */
  #dashwrap {
    position: absolute; inset: 18px 24px 96px 24px; display: none;
    border-radius: 12px; overflow: hidden; box-shadow: 0 12px 40px rgba(0,0,0,.5);
    background: #1a1a19;
  }
  #dashwrap iframe {
    width: 1280px; height: 800px; border: none;
    transform: scale(0.9625); transform-origin: 0 0;
  }

  #cap {
    position: absolute; left: 24px; right: 24px; bottom: 18px; height: 64px;
    background: #182018; border: 1px solid #2e5a41; border-left: 4px solid #27b57f;
    border-radius: 10px; display: flex; align-items: center; padding: 0 22px;
    color: #e8f4ec; font-size: 19px; font-weight: 500; letter-spacing: -.01em;
  }
  #cap b { color: #3ecf95; margin-right: 12px; font-size: 15px; white-space: nowrap; }
</style>
</head>
<body>
  <div id="term">
    <div id="bar">
      <i style="background:#ff5f57"></i><i style="background:#febc2e"></i><i style="background:#28c840"></i>
      <span class="t">알뜰배차 시연 — 공개 Dev 880문항 실측</span>
    </div>
    <div id="screen"></div>
  </div>
  <div id="dashwrap"><iframe src="dashboard.html"></iframe></div>
  <div id="cap"><b>알뜰배차</b><span id="captext"></span></div>

<script>
const SCENES = __SCENES__;
const DASH_DUR = __DASH_DUR__;
const screen = document.getElementById('screen');
const captext = document.getElementById('captext');

let i = 0;
function next() {
  if (i < SCENES.length) {
    const s = SCENES[i];
    const d = document.createElement('div');
    d.className = 'scene';
    d.innerHTML = s.html;
    screen.appendChild(d);
    // 이전 장면은 위로 밀려나며 흐려짐
    [...screen.children].slice(0, -1).forEach(el => el.style.opacity = 0.25);
    captext.textContent = s.caption;
    i += 1;
    setTimeout(next, s.dur * 1000);
  } else if (i === SCENES.length) {
    document.getElementById('term').style.display = 'none';
    document.getElementById('dashwrap').style.display = 'block';
    captext.textContent = '결과 대시보드 — 점수 비교 · 예산 여유 · 등급별 모델 배분을 한눈에';
    i += 1;
    setTimeout(next, DASH_DUR * 1000);
  } else {
    document.title = 'DONE';
  }
}
window.addEventListener('load', () => document.fonts.ready.then(() => setTimeout(next, 500)));
</script>
</body>
</html>
"""

# rich가 넣은 pre 배경·폰트는 CSS로 덮으므로 그대로 두되, 각 장면 pre의
# style에서 background-color만 제거해 터미널 창 배경이 비치게 한다.
BOX_FIX = str.maketrans({
    '═': '━', '║': '┃', '╔': '┏', '╗': '┓', '╚': '┗', '╝': '┛',
    '╭': '┌', '╮': '┐', '╰': '└', '╯': '┘',
})
for s in scenes:
    s["html"] = s["html"].replace('background-color:#000000', 'background-color:transparent')
    s["html"] = s["html"].translate(BOX_FIX)

html = TEMPLATE.replace("__SCENES__", json.dumps(scenes, ensure_ascii=False)).replace(
    "__DASH_DUR__", str(DASH_DUR)
)
(OUT / "player.html").write_text(html, encoding="utf-8")
import shutil
shutil.copy(HERE / 'dashboard.html', OUT / 'dashboard.html')
for f in ('D2Coding.ttf', 'D2CodingBold.ttf'):
    if (HERE.parent / 'build' / 'fonts' / f).exists():
        shutil.copy(HERE.parent / 'build' / 'fonts' / f, OUT / f)
    else:
        raise SystemExit('폰트가 없습니다. 먼저 python3 demo/fetch_font.py 를 실행하세요.')
total = sum(s["dur"] for s in scenes) + DASH_DUR
print(f"player.html written — 예상 길이 {total}s")
