# Third-party notices

Original VARIANT-1 code is licensed under the root MIT license. Third-party
components retain their own licenses. The Live2D cat and its floating overlay
have been removed, including vendor libraries and model assets.

## Prime Agent — persistent Python execution inspiration

VARIANT-1's persistent Python execution approach was inspired by
[Prime Agent](https://github.com/PrimeIntellect-ai/prime-agent) from Prime Intellect.
We thank the Prime Agent contributors for sharing their work.

This acknowledgement credits the design inspiration and does not imply
endorsement by Prime Intellect.

Prime Agent's upstream MIT license notice is reproduced below for reference,
from [LICENSE at commit `66abc2a`](https://github.com/PrimeIntellect-ai/prime-agent/blob/66abc2a604fc42a220292a1ca4cf33ee60cb5733/LICENSE).

MIT License

Copyright (c) 2025 Mario Zechner
Copyright (c) 2026 Prime Intellect

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

## Noto Sans CJK subsets (PDF)

The packaged faces under `backend/artifacts/fonts/` are TrueType-outline
instances of Noto Sans VF 2.004 at Regular (`wght=400`) and Bold (`wght=700`):

- `Variant1CJK-Regular.ttf` and `Variant1CJK-Bold.ttf` from Noto Sans SC
- `Variant1CJKKR-Regular.ttf` and `Variant1CJKKR-Bold.ttf` from Noto Sans KR

Ordinary Japanese is covered by the SC subset (kana and the shared BMP
ideographs). Hangul uses the KR pair. They are distributed under the SIL Open
Font License 1.1. The full upstream notice/license is included in
`assets/licenses/NotoSansSC-OFL-1.1.txt` and `backend/artifacts/fonts/OFL.txt`.
Project: https://github.com/notofonts/noto-cjk

## Geist fonts

The bundled Geist and Geist Mono font files are distributed under the SIL Open
Font License 1.1. The full upstream notice/license is included in
`assets/licenses/Geist-OFL-1.1.txt`. Project: https://github.com/vercel/geist-font

## Hermes Agent workbench

Portions of VARIANT-1's workbench layout, file browser, review, terminal, and browser-preview design are adapted from Hermes Agent:

https://github.com/NousResearch/hermes-agent

MIT License

Copyright (c) 2025 Nous Research

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.


## Oh My Pi — Google AI subscription protocol reference

VARIANT-1's Google AI subscription OAuth and Cloud Code Assist transport were
implemented with reference to Oh My Pi's Google Antigravity provider:

https://github.com/can1357/oh-my-pi

MIT License

Copyright (c) 2025 Mario Zechner
Copyright (c) 2025-2026 Can Bölük
Copyright (c) 2026 Stencil Labs, Inc.

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

## p5.js

p5.js 2.3.2 is used for the code-generated kernel symbol and is bundled without modification. Licensed under LGPL-2.1. The complete license is in assets/licenses/p5-LGPL-2.1.txt. Source: https://github.com/processing/p5.js/tree/v2.3.2


## Native and generated runtime notices

The installer retains per-component licenses; the application MIT declaration
is not a license grant for other third-party software. The only bundled native
program is the pinned cua-driver desktop driver (trycua/cua, MIT); on macOS it
ships as trycua's signed CuaDriver.app, unmodified.

llama.cpp is not bundled. When a user installs the local engine from the app,
VARIANT-1 downloads the official llama.cpp release archive for that platform,
checks it against a pinned SHA-256 digest, and keeps it in user data with the
licenses and notices the archive itself carries (including NVIDIA CUDA
redistribution terms for the CUDA build).

The renderer bundle carries THIRD_PARTY_LICENSES.txt for its compiled modules.
The frozen backend's _internal/THIRD_PARTY_LICENSES.txt records locked Python and
CPython notices. Electron includes LICENSE and LICENSES.chromium.html. The
baseline excludes offline speech engines and weights; independently installed
speech services remain subject to their own licenses.
