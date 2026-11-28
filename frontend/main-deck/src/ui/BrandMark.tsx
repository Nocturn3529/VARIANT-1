import type {CSSProperties} from "react";
import type {KernelPhase} from "../motion/kernelField";

const MONOGRAM_V = "44,230 162,230 278,595 394,230 512,230 338,770 218,770";
const MONOGRAM_ONE = "716,296 802,230 894,230 894,670 956,670 956,770 716,770 716,670 782,670 782,356 716,408";
const WORDMARK_V = "120,230 236,230 344,588 452,230 568,230 402,770 286,770";
const WORDMARK_A1 = "M 770 230 H 886 L 1052 770 H 936 L 903 662 H 753 L 720 770 H 604 Z M 828 372 L 768 566 H 888 Z";
const WORDMARK_R = "M 1112 230 H 1364 C 1462 230 1516 286 1516 384 C 1516 466 1474 518 1404 536 L 1524 770 H 1398 L 1292 548 H 1224 V 770 H 1112 Z M 1224 330 V 452 H 1350 C 1392 452 1412 428 1412 391 C 1412 354 1392 330 1350 330 Z";
const WORDMARK_A2 = "M 1922 230 H 2038 L 2204 770 H 2088 L 2055 662 H 1905 L 1872 770 H 1756 Z M 1980 372 L 1920 566 H 2040 Z";
const WORDMARK_N = "2264,230 2374,230 2580,584 2580,230 2688,230 2688,770 2578,770 2372,416 2372,770 2264,770";
const WORDMARK_T = "2748,230 3148,230 3148,332 3004,332 3004,770 2892,770 2892,332 2748,332";
const WORDMARK_ONE = "3426,296 3512,230 3604,230 3604,670 3666,670 3666,770 3426,770 3426,670 3492,670 3492,356 3426,408";

/**
 * The canonical VARIANT-1 brand mark: geometric V-1 monogram or full VARIANT-1
 * wordmark with the signature lime hyphen accent. Replaces circular orb glyphs
 * across boot, empty chat, top rail, About, and Updates.
 */
export function BrandMark({
  variant = "monogram",
  size = 28,
  height,
  showWordmark = false,
  phase = "idle",
  mutation = false,
  className = "",
  label,
}: {
  variant?: "monogram" | "wordmark";
  size?: number;
  height?: number;
  showWordmark?: boolean;
  seed?: string;
  phase?: KernelPhase;
  mutation?: boolean;
  className?: string;
  /** Accessible name when the mark stands alone; decorative otherwise. */
  label?: string;
}) {
  const mode = showWordmark ? "wordmark" : variant;
  const wordmarkHeight = height ?? Math.max(12, Math.round(size * 0.65));
  const cls = [
    "brand-mark",
    `brand-mark--${mode}`,
    mutation ? "is-mutation" : "",
    className,
  ].filter(Boolean).join(" ");

  if (mode === "wordmark") {
    return <span
      className={cls}
      data-phase={phase}
      role={label ? "img" : undefined}
      aria-label={label}
      style={{"--brand-height": `${wordmarkHeight}px`} as CSSProperties}
    >
      <svg className="brand-mark__wordmark-svg" viewBox="112 222 3562 556" aria-hidden="true" focusable="false">
        <polygon className="brand-mark__fg" points={WORDMARK_V}/>
        <path className="brand-mark__fg" fillRule="evenodd" d={WORDMARK_A1}/>
        <path className="brand-mark__fg" fillRule="evenodd" d={WORDMARK_R}/>
        <rect className="brand-mark__fg" x="1588" y="230" width="112" height="540"/>
        <path className="brand-mark__fg" fillRule="evenodd" d={WORDMARK_A2}/>
        <polygon className="brand-mark__fg" points={WORDMARK_N}/>
        <polygon className="brand-mark__fg" points={WORDMARK_T}/>
        <rect className="brand-mark__accent" x="3196" y="458" width="182" height="92"/>
        <polygon className="brand-mark__fg" points={WORDMARK_ONE}/>
      </svg>
    </span>;
  }

  return <span
    className={cls}
    data-phase={phase}
    role={label ? "img" : undefined}
    aria-label={label}
    style={{"--brand-size": `${size}px`} as CSSProperties}
  >
    <span className="brand-mark__tile" aria-hidden="true">
      <svg className="brand-mark__monogram-svg" viewBox="0 0 1000 1000" aria-hidden="true" focusable="false">
        <polygon className="brand-mark__fg" points={MONOGRAM_V}/>
        <rect className="brand-mark__accent" x="498" y="458" width="182" height="92"/>
        <polygon className="brand-mark__fg" points={MONOGRAM_ONE}/>
      </svg>
    </span>
  </span>;
}
