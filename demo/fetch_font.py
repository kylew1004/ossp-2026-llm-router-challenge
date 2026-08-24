# SPDX-FileCopyrightText: Copyright 2026 SmartDispatch contributors
# SPDX-License-Identifier: Apache-2.0

"""D2Coding 폰트(OFL-1.1)를 고정 출처에서 받아 SHA-256 검증 후 build/fonts에 둔다.

한글과 박스 문자 폭이 정확히 2:1로 맞는 코딩 폰트로, 시연 영상 렌더링에만
사용한다. 저장소에는 바이너리를 커밋하지 않는 정책에 따라 스크립트로 받는다.
"""

from __future__ import annotations

import hashlib
import io
import urllib.request
import zipfile
from pathlib import Path

URL = (
    "https://github.com/naver/d2codingfont/releases/download/"
    "VER1.3.2/D2Coding-Ver1.3.2-20180524.zip"
)
SHA256 = "0f1c9192eac7d56329dddc620f9f1666b707e9c8ed38fe1f988d0ae3e30b24e6"
MEMBERS = {
    "D2Coding/D2Coding-Ver1.3.2-20180524.ttf": "D2Coding.ttf",
    "D2Coding/D2CodingBold-Ver1.3.2-20180524.ttf": "D2CodingBold.ttf",
}

def main() -> None:
    out = Path(__file__).resolve().parents[1] / "build" / "fonts"
    out.mkdir(parents=True, exist_ok=True)
    if all((out / name).exists() for name in MEMBERS.values()):
        print(f"OK: 폰트가 이미 있습니다 ({out})")
        return
    print(f"다운로드: {URL}")
    data = urllib.request.urlopen(URL, timeout=60).read()
    digest = hashlib.sha256(data).hexdigest()
    if digest != SHA256:
        raise SystemExit(f"SHA-256 불일치: {digest}")
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for member, name in MEMBERS.items():
            (out / name).write_bytes(z.read(member))
            print(f"  {name} 저장")
    print(f"OK: {out}")

if __name__ == "__main__":
    main()
