# Variant1 CJK SC (Noto Sans SC)

Portable PDF faces for Simplified Chinese documents. ReportLab 4.x/5.x cannot
embed the CFF/PostScript outlines used by Noto CJK TTC/OTC packages
(`postscript outlines are not supported`). These files are TrueType `glyf`
instances of the upstream Simplified Chinese subset.

| Field | Value |
| --- | --- |
| Upstream project | [notofonts/noto-cjk](https://github.com/notofonts/noto-cjk) |
| Upstream file | `Sans/Variable/TTF/Subset/NotoSansSC-VF.ttf` |
| Upstream version | 2.004 (`hotconv 1.0.118; makeotfexe 2.5.65603`) |
| License | SIL Open Font License 1.1 |
| Copyright | © 2014-2021 Adobe, with Reserved Font Name `Source` |
| Regular | `Variant1CJK-Regular.ttf`, instantiated at `wght=400` |
| Bold | `Variant1CJK-Bold.ttf`, instantiated at `wght=700` |
| Packaged names | `Variant1 CJK SC Regular` / `Variant1 CJK SC Bold` |
| Outline table | `glyf` (TrueType). No `CFF `/`CFF2`. |
| PDF embedding | ReportLab `TTFont` subsets used glyphs into `/FontFile2` |

Coverage is the upstream Noto Sans SC language subset: Basic Latin, Latin-1,
common punctuation, and the Simplified Chinese repertoire in that face
(about 30,890 cmap entries). It is the portable face for user documents, not
a handful of regression-test characters. Japanese and Korean are not claimed.

The OFL text is at `assets/licenses/NotoSansSC-OFL-1.1.txt` and `OFL.txt`.
