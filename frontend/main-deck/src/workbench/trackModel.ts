/** Fixed sidebar tracks and flexible working space, adapted from Hermes.
 * See docs/HERMES_WORKBENCH_DONOR_MANIFEST.md and THIRD_PARTY_NOTICES.md. */
import type {LayoutNode, Orientation, SplitNode} from "./layoutModel";
import {allPaneIds} from "./layoutModel";

export const COLLAPSED_TRACK = 34;
export const paneKind = (id:string) => id.startsWith("owned:") ? id.split(":")[1] : id;
export const isSidePane = (id: string) => ["history","files","review"].includes(paneKind(id));

export function paneSide(node: LayoutNode, id: string): "left" | "right" {
  if (node.type === "split") {
    const paneIndex = node.children.findIndex(child => allPaneIds(child).includes(id));
    const mainIndex = node.children.findIndex(child => allPaneIds(child).includes("workspace"));
    if (paneIndex >= 0 && mainIndex >= 0 && paneIndex !== mainIndex && node.orientation === "row") return paneIndex < mainIndex ? "left" : "right";
    if (paneIndex >= 0 && paneIndex === mainIndex) return paneSide(node.children[paneIndex], id);
  }
  return id === "history" ? "left" : "right";
}
export type TrackContext = {
  hidden: Readonly<Record<string, boolean>>;
  known: ReadonlySet<string>;
  compact: boolean;
  height: number;
  floatingGroups?: ReadonlySet<string>;
  nativeGroups?: ReadonlySet<string>;
};
export type Track = {preferred: number | null; min: number; max: number; collapsed?: boolean};

export function paneInGrid(id: string, context: TrackContext): boolean {
  return context.known.has(id) && !context.hidden[id] && !(context.compact && isSidePane(id));
}

export function trackFor(node: LayoutNode, axis: Orientation, context: TrackContext): Track | null {
  if (node.type === "group") {
    if (context.nativeGroups?.has(node.id)) return {preferred: 0, min: 0, max: 0};
    // A zero track keeps the existing DOM mounted while its popover floats.
    if (context.floatingGroups?.has(node.id) && !context.compact) return {preferred: 0, min: 0, max: 0};
    const panes = node.panes.filter(id => paneInGrid(id, context));
    if (!panes.length) return node.panes.some(id=>id.startsWith("preview:") && context.known.has(id)) ? {preferred:0,min:0,max:0} : null;
    if (node.minimized) return {preferred: COLLAPSED_TRACK, min: COLLAPSED_TRACK, max: COLLAPSED_TRACK, collapsed: true};
    const main = panes.includes("workspace");
    if (axis === "column") {
      return panes.every(id => paneKind(id) === "terminal")
        ? {preferred: Math.max(120, context.height * .2), min: 90, max: Math.max(120, context.height * .8)}
        : {preferred: null, min: main ? 220 : 100, max: Infinity};
    }
    if (main) return {preferred: null, min: 360, max: Infinity};
    if(panes.some(id=>id.startsWith("chatview:")))return {preferred:null,min:320,max:Infinity};
    if (panes.some(id => id.startsWith("preview:"))) return {preferred: null, min: 280, max: Infinity};
    if (panes.some(id=>paneKind(id)==="terminal")) return {preferred: null, min: 180, max: Infinity};
    return panes.some(id=>paneKind(id)==="review")
      ? {preferred: 360, min: 260, max: Infinity}
      : {preferred: 237, min: 180, max: 360};
  }
  const children = childTracks(node, axis, context).map(item => item.track);
  const tracks = children.filter(track => track.max > 0);
  if (!tracks.length && children.length) return {preferred: 0, min: 0, max: 0};
  if (!tracks.length) return null;
  if (tracks.length === 1) return tracks[0];
  const along = node.orientation === axis;
  const combine = (values: number[]) => along ? values.reduce((a, b) => a + b, tracks.length - 1) : Math.max(...values);
  const fixed = tracks.filter(track => track.preferred !== null && (along || !track.collapsed || tracks.every(item => item.preferred !== null)));
  return {
    preferred: (along ? fixed.length === tracks.length : fixed.length > 0)
      ? combine((along ? tracks : fixed).map(track => track.preferred!)) : null,
    min: combine(tracks.map(track => track.min)),
    max: combine(tracks.map(track => track.max)),
    collapsed: tracks.every(track => track.collapsed),
  };
}

export function childTracks(node: SplitNode, axis: Orientation, context: TrackContext) {
  return node.children.flatMap((child, index) => {
    const track = trackFor(child, axis, context);
    if (!track) return [];
    const override = node.orientation === axis ? node.sizes?.[child.id] : undefined;
    return [{child, index, track: override !== undefined && track.preferred !== null
      ? {...track, preferred: Math.max(track.min, Math.min(track.max, override))} : track}];
  });
}

/** Allocate the measured space without letting an empty/minimized wrapper grow. */
export function allocateTracks(tracks: readonly Track[], available: number, weights: readonly number[]): number[] {
  const room = Math.max(0, available);
  const sizes = tracks.map(track => Math.max(track.min, Math.min(track.max, track.preferred ?? track.min)));
  const sum = () => sizes.reduce((a, b) => a + b, 0);
  let excess = sum() - room;
  if (excess > 0) {
    const slack = sizes.reduce((total, size, i) => total + size - tracks[i].min, 0);
    if (slack > 0) sizes.forEach((size, i) => { sizes[i] -= Math.min(1, excess / slack) * (size - tracks[i].min); });
    excess = sum() - room;
    if (excess > .01) {
      // The split scrolls when its children cannot fit. Scaling below these
      // minima makes controls unusable and produces misleading browser frames.
      return sizes;
    }
  }
  let remaining = room - sum();
  let grow = tracks.flatMap((track, i) => track.preferred === null ? [i] : []);
  if (!grow.length) grow = tracks.flatMap((track, i) => track.max > sizes[i] ? [i] : []);
  while (remaining > .01 && grow.length) {
    const total = grow.reduce((value, i) => value + Math.max(.01, weights[i] || 1), 0);
    const before = remaining;
    for (const i of grow) {
      const add = Math.min(tracks[i].max - sizes[i], before * Math.max(.01, weights[i] || 1) / total);
      sizes[i] += add;
      remaining -= add;
    }
    grow = grow.filter(i => tracks[i].max - sizes[i] > .01);
    if (before - remaining < .01) break;
  }
  return sizes;
}
