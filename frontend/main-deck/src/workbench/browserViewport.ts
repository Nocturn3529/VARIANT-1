export type ViewportSize = Readonly<{width: number; height: number}>;
export const DEFAULT_BROWSER_VIEWPORT: ViewportSize = {width: 800, height: 480};
export const browserWindowSize = (size: ViewportSize) => ({width: size.width + 32, height: size.height + 160});

/** A deliberate mobile size is valid; an automatic narrow dock is not. */
export function parseBrowserViewport(value: Record<string, unknown>): ViewportSize | null {
  if (value.mode === "auto") return null;
  const {width, height} = value;
  if (typeof width !== "number" || typeof height !== "number" || !Number.isInteger(width) || !Number.isInteger(height)
    || width < 320 || width > 3840 || height < 240 || height > 2160) {
    throw new Error("Viewport needs integer width 320–3840 and height 240–2160 in CSS pixels");
  }
  return {width, height};
}

export function browserViewportSize(available: ViewportSize, requested?: ViewportSize | null): ViewportSize {
  return requested || {
    width: Math.min(3840, Math.max(DEFAULT_BROWSER_VIEWPORT.width, Math.floor(available.width))),
    height: Math.min(2160, Math.max(DEFAULT_BROWSER_VIEWPORT.height, Math.floor(available.height))),
  };
}

export function applyBrowserViewport(guest: HTMLElement, requested?: ViewportSize | null): void {
  const host = guest.parentElement;
  const surface = host?.parentElement;
  if (!host || !surface) return;
  // Hidden retained tabs have no layout. Keep their last real viewport rather
  // than treating a zero-size wrapper as a new automatic-size request.
  if (!requested && (!surface.clientWidth || !surface.clientHeight)) return;
  // Automatic geometry follows the native document's CSS layout immediately,
  // even while its owning React renderer is busy in another window.
  const width=requested ? `${requested.width}px` : "clamp(800px, 100%, 3840px)";
  const height=requested ? `${requested.height}px` : "clamp(480px, 100%, 2160px)";
  if(host.style.width!==width)host.style.width=width;
  if(host.style.height!==height)host.style.height=height;
  guest.dataset.viewportMode = requested ? "fixed" : "auto";
}

export function measureBrowserViewport(guest: HTMLElement) {
  const rect = guest.getBoundingClientRect();
  const container = guest.parentElement?.parentElement;
  const surface = container?.getBoundingClientRect() || rect;
  const right = container?.clientWidth ? surface.left + container.clientWidth : surface.right;
  const bottom = container?.clientHeight ? surface.top + container.clientHeight : surface.bottom;
  return {
    width: Math.round(rect.width), height: Math.round(rect.height),
    visible_width: Math.round(Math.max(0, Math.min(rect.right, right) - Math.max(rect.left, surface.left))),
    visible_height: Math.round(Math.max(0, Math.min(rect.bottom, bottom) - Math.max(rect.top, surface.top))),
    device_scale_factor: guest.ownerDocument.defaultView?.devicePixelRatio || 1,
    mode: guest.dataset.viewportMode === "fixed" ? "fixed" : "auto",
  };
}
