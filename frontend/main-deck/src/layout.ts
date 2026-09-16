/**
 * Main Deck layout constants shared by React chrome and CSS media queries.
 *
 * CSS may only use these max-widths (plus prefers-reduced-motion). Keep
 * `scripts/test-deck-design-system.js` in lockstep when a value changes.
 */
export const DECK_BREAKPOINT = {
  /** History drawer, compact destination chrome, stacked forms. */
  narrow: 830,
  /** Destination and Overview grids collapse from multi-column. */
  medium: 980,
  /** Context inspector overlays the transcript instead of sitting in-flow. */
  wide: 1180,
} as const;

export type DeckBreakpoint = typeof DECK_BREAKPOINT[keyof typeof DECK_BREAKPOINT];
