# Variant1 CJK SC (Noto Sans SC subset)

Portable PDF CJK qualification font. ReportLab 4.x/5.x cannot embed the
CFF/PostScript outlines used by Noto CJK TTC/OTC packages (`postscript
outlines are not supported`). This file is a TrueType `glyf` subset.

| Field | Value |
| --- | --- |
| Upstream project | [notofonts/noto-cjk](https://github.com/notofonts/noto-cjk) |
| Upstream file | `Sans/Variable/TTF/Subset/NotoSansSC-VF.ttf` |
| Upstream version | 2.004 (`hotconv 1.0.118; makeotfexe 2.5.65603`) |
| License | SIL Open Font License 1.1 |
| Copyright | © 2014-2021 Adobe, with Reserved Font Name `Source` |
| Instantiation | `wght=400` (Regular) |
| Packaged name | `Variant1 CJK SC Regular` (modified version; does not use the reserved name) |
| Outline table | `glyf` (TrueType). No `CFF `/`CFF2`. |
| PDF embedding | ReportLab `TTFont` → `/FontFile2` |

Glyph coverage is Basic Latin, selected punctuation, and the Simplified
Chinese characters used by the portable PDF tests (`Hello 中文` /
`再次检查 中文`). Japanese and Korean support is not claimed.

The OFL text is at `assets/licenses/NotoSansSC-OFL-1.1.txt`.
