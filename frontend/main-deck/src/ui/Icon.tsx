import type {SVGProps} from "react";

const paths = {
  thought: "M9 18h6m-5 3h4M8 14a6 6 0 1 1 8 0c-1 1-1 2-1 2H9s0-1-1-2Z",
  peers: "M3 4h12v9H8l-5 4V4Zm15 4h3v13l-5-4h-5",
  tree: "M12 3v6M5 15v-6h14v6M3 15h4v5H3Zm7 0h4v5h-4Zm7 0h4v5h-4M12 9v6",
  search: "M10.5 17a6.5 6.5 0 1 0 0-13 6.5 6.5 0 0 0 0 13Zm5-1 4.5 4.5",
  plus: "M12 5v14M5 12h14",
  close: "m6 6 12 12M6 18 18 6",
  chevron: "m9 5 7 7-7 7",
  back: "M20 12H4m6-6-6 6 6 6",
  forward: "M4 12h16m-6-6 6 6-6 6",
  collapse: "M5 3v18m15-9H9m5-5-5 5 5 5",
  code: "m8 6-6 6 6 6m8-12 6 6-6 6m-3-15-2 18",
  down: "m5 9 7 7 7-7",
  up: "m5 15 7-7 7 7",
  history: "M3 4h18v16H3ZM8 4v16",
  panels: "M3 4h18v16H3Zm13 0v16",
  layout: "M3 4h18v16H3Zm9 0v16M3 12h18",
  flip: "M4 7h16m-4-4 4 4-4 4M20 17H4m4-4-4 4 4 4",
  terminal: "m4 6 6 6-6 6m9 0h7",
  review: "M7 3v5m0 8v5m-3-9h6m7-9v18m-3-6h6",
  browser: "M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0ZM3 12h18M12 3c5 5 5 13 0 18-5-5-5-13 0-18Z",
  file: "M14 3H5v18h14V8Zm0 0v5h5M8 12h8m-8 4h6",
  folder: "M3 6h7l2 2h9v12H3Z",
  image: "M3 4h18v16H3Zm0 12 5-5 5 5 3-3 5 5M15 8h.01",
  attach: "m9 13 6-6a3 3 0 0 1 4 4l-9 9a5 5 0 0 1-7-7l9-9",
  mic: "M9 6a3 3 0 0 1 6 0v6a3 3 0 0 1-6 0Zm-4 5a7 7 0 0 0 14 0m-7 7v3m-4 0h8",
  send: "M12 20V4m-6 6 6-6 6 6",
  stop: "M6 6h12v12H6Z",
  pause: "M8 5v14M16 5v14",
  play: "m7 4 13 8-13 8V4Z",
  queue: "M4 5h16M4 11h10M4 17h6m7-4 4 4-4 4m-4-4h8",
  check: "m4 12 5 5L20 6",
  error: "M12 4 2 21h20ZM12 10v5m0 3h.01",
  settings: "M9 3h6l1 4 4 1v7l-4 1-1 5H9l-1-5-4-1V8l4-1Zm6 9a3 3 0 1 0-6 0 3 3 0 0 0 6 0Z",
  overview: "M4 20V10m5 10V4m5 16v-7m5 7V7",
  windows: "M4 8h12v12H4Zm4-4h12v12",
  popout: "M13 3h8v8M21 3 11 13M9 5H3v16h16v-6",
  dock: "M21 14v7H3V3h7m5 0v12m-5-5 5 5 5-5",
  pin: "m9 3 6 0-1 6 4 4H6l4-4Zm3 10v8",
  minimize: "M5 12h14",
  maximize: "M5 5h14v14H5Z",
  kernel: "M7 7h10v10H7ZM8 3v4m8-4v4M8 17v4m8-4v4M3 8h4m-4 8h4m10-8h4m-4 8h4",
  clock: "M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0ZM12 6v6l4 2",
  refresh: "M20 10a8 8 0 1 0-2 8M20 3v7h-7",
  command: "M8 8h8v8H8Zm0 0H5a3 3 0 1 1 3-3Zm8 0V5a3 3 0 1 1 3 3Zm0 8h3a3 3 0 1 1-3 3Zm-8 0v3a3 3 0 1 1-3-3Z",
} as const;
export type IconName = keyof typeof paths;

export function Icon({name, className = "", ...props}: SVGProps<SVGSVGElement> & {name: IconName}) {
  return <svg {...props} className={`deck-icon ${className}`} viewBox="0 0 24 24" fill="none"
    stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false"><path d={paths[name]}/></svg>;
}
