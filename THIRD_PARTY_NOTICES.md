# Third-party notices

Original VARIANT-1 code is licensed under the root MIT license. Third-party
components retain their own licenses. The Live2D cat and its floating overlay
have been removed, including vendor libraries and model assets.

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
is not a license grant for NVIDIA SDK libraries or other third-party software.
The build manifest pins llama.cpp b10289 (f9e832c10e9444cb168ddcb579cc62c154f3068b)
and its CPU/CUDA DLLs. NVIDIA CUDA 13.3 redistribution terms and the llama.cpp,
cpp-httplib and json.hpp MIT notices are in assets/licenses/native.

The loader filename libomp140.x86_64.dll contains the official LLVM20.1.8
libomp.dll bytes (SHA256 a12116ba72d1d6820407cf30be23da04ce79d6bb8a71a5ee71759c5a1faa6f1c),
under Apache-2.0 WITH LLVM-exception. It is not the Microsoft debug_nonredist
binary from the original upstream Windows package. Its imported/exported ABI
and CPU graph execution are tested before release. Source and archive provenance
are recorded in config/native-runtime.json and the included LLVM notice.

The renderer bundle carries THIRD_PARTY_LICENSES.txt for its compiled modules.
The frozen backend's _internal/THIRD_PARTY_LICENSES.txt records locked Python and
CPython notices. Electron includes LICENSE and LICENSES.chromium.html. The
baseline excludes offline speech engines and weights; independently installed
speech services remain subject to their own licenses.
