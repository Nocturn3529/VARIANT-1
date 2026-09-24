# Variant1 CJK faces (Noto Sans)

Portable PDF faces. ReportLab 4.x/5.x cannot embed the CFF/PostScript outlines
used by Noto CJK TTC/OTC packages (`postscript outlines are not supported`).
These files are TrueType `glyf` instances of the upstream language subsets.

| Field | Value |
| --- | --- |
| Upstream project | [notofonts/noto-cjk](https://github.com/notofonts/noto-cjk) |
| Upstream files | `Sans/Variable/TTF/Subset/NotoSansSC-VF.ttf`, `NotoSansKR-VF.ttf` |
| Upstream version | 2.004 (`hotconv 1.0.118; makeotfexe 2.5.65603`) |
| License | SIL Open Font License 1.1 |
| Copyright | © 2014-2021 Adobe, with Reserved Font Name `Source` |
| Simplified Chinese | `Variant1CJK-Regular.ttf` / `Variant1CJK-Bold.ttf` at `wght` 400 / 700 |
| Korean | `Variant1CJKKR-Regular.ttf` / `Variant1CJKKR-Bold.ttf` at `wght` 400 / 700 |
| Packaged names | `Variant1 CJK SC` and `Variant1 CJK KR` (the reserved name `Source` is not used) |
| Outline table | `glyf` (TrueType). No `CFF `/`CFF2`. |
| PDF embedding | ReportLab `TTFont` subsets used glyphs into `/FontFile2` |

The SC face is the first registered pair, so Latin, Simplified Chinese, and
ordinary Japanese (kana plus the shared BMP ideographs, including
`こんにちは`) stay on it. Hangul is absent from that subset and is drawn with
the KR pair (`안녕하세요`). Supplementary-plane characters are still refused
by the PDF builder. These are language subsets, not a full CJK super-set.

The OFL text is at `assets/licenses/NotoSansSC-OFL-1.1.txt` and `OFL.txt`.
