/**
 * Split/group workbench model adapted from Hermes Agent's MIT-licensed
 * pane-shell tree model. See THIRD_PARTY_NOTICES.md.
 */

export type Orientation = "row" | "column";
export type DropPosition = "center" | "left" | "right" | "top" | "bottom";
export type TabStripMode = "always" | "never";

export type SplitNode = Readonly<{
  type: "split";
  id: string;
  orientation: Orientation;
  children: readonly LayoutNode[];
  weights: readonly number[];
  sizes?: Readonly<Record<string, number>>;
}>;

export type GroupNode = Readonly<{
  type: "group";
  id: string;
  panes: readonly string[];
  active: string;
  minimized?: boolean;
  tabStrip?: TabStripMode;
}>;

export type LayoutNode = SplitNode | GroupNode;

let sequence = 0;
export function layoutNodeId(kind: string): string {
  sequence += 1;
  return `${kind}-${Date.now().toString(36)}-${sequence.toString(36)}`;
}

export function group(
  panes: readonly string[],
  options: Partial<Omit<GroupNode, "type" | "panes">> = {},
): GroupNode {
  return {
    type: "group",
    id: options.id || layoutNodeId("group"),
    panes: [...panes],
    active: options.active || panes[0] || "",
    minimized: options.minimized,
    tabStrip: options.tabStrip,
  };
}

export function split(
  orientation: Orientation,
  children: readonly LayoutNode[],
  weights: readonly number[] = children.map(() => 1),
  id = layoutNodeId("split"),
): SplitNode {
  return {type: "split", id, orientation, children: [...children], weights: [...weights]};
}

export function allPaneIds(node: LayoutNode): string[] {
  return node.type === "group"
    ? [...node.panes]
    : node.children.flatMap(allPaneIds);
}

export function findGroup(node: LayoutNode, groupId: string): GroupNode | null {
  if (node.type === "group") return node.id === groupId ? node : null;
  for (const child of node.children) {
    const match = findGroup(child, groupId);
    if (match) return match;
  }
  return null;
}

export function findGroupOfPane(node: LayoutNode, paneId: string): GroupNode | null {
  if (node.type === "group") return node.panes.includes(paneId) ? node : null;
  for (const child of node.children) {
    const match = findGroupOfPane(child, paneId);
    if (match) return match;
  }
  return null;
}

export function normalize(node: LayoutNode, seen = new Set<string>()): LayoutNode | null {
  if (node.type === "group") {
    const panes = node.panes.filter(id => {if (seen.has(id)) return false; seen.add(id); return true;});
    if (panes.length !== node.panes.length) node = {...node, panes};
    if (!node.panes.length) return null;
    const active = node.panes.includes(node.active) ? node.active : node.panes[0];
    return active === node.active ? node : {...node, active};
  }

  const children: LayoutNode[] = [];
  const weights: number[] = [];
  node.children.forEach((child, index) => {
    const kept = normalize(child, seen);
    if (!kept) return;
    if (kept.type === "split" && kept.orientation === node.orientation) {
      const total = kept.weights.reduce((sum, value) => sum + value, 0) || 1;
      kept.children.forEach((grandchild, childIndex) => {
        children.push(grandchild);
        weights.push((node.weights[index] || 1) * ((kept.weights[childIndex] || 1) / total));
      });
      return;
    }
    children.push(kept);
    weights.push(node.weights[index] || 1);
  });
  if (!children.length) return null;
  if (children.length === 1) return children[0];
  return {...node, children, weights};
}

function mapGroups(node: LayoutNode, mapper: (value: GroupNode) => GroupNode): LayoutNode {
  if (node.type === "group") return mapper(node);
  return {...node, children: node.children.map(child => mapGroups(child, mapper))};
}

export function setActivePane(root: LayoutNode, groupId: string, paneId: string): LayoutNode {
  return mapGroups(root, current => current.id === groupId && current.panes.includes(paneId)
    ? {...current, active: paneId, minimized: false}
    : current);
}

export function setGroupMinimized(root: LayoutNode, groupId: string, minimized: boolean): LayoutNode {
  return mapGroups(root, current => current.id === groupId
    ? {...current, minimized}
    : current);
}

export function setGroupTabStrip(root: LayoutNode, groupId: string, mode?: TabStripMode): LayoutNode {
  return mapGroups(root, current => current.id === groupId
    ? {...current, tabStrip: mode}
    : current);
}

export function removePane(root: LayoutNode, paneId: string): LayoutNode | null {
  const walk = (node: LayoutNode): LayoutNode => {
    if (node.type === "group") {
      const index = node.panes.indexOf(paneId);
      if (index < 0) return node;
      const panes = node.panes.filter(value => value !== paneId);
      const active = node.active === paneId
        ? panes[Math.min(index, panes.length - 1)] || ""
        : node.active;
      return {...node, panes, active};
    }
    return {...node, children: node.children.map(walk)};
  };
  return normalize(walk(root));
}

export function insertAtGroup(
  root: LayoutNode,
  targetGroupId: string,
  paneId: string,
  position: DropPosition,
  before: string | null = null,
  activate = true,
  edgeWeights: readonly [number, number] = [1, 1],
): LayoutNode | null {
  const existing = findGroupOfPane(root, paneId);
  if (existing && existing.id !== targetGroupId) return root;
  const walk = (node: LayoutNode): LayoutNode => {
    if (node.type === "group") {
      if (node.id !== targetGroupId) return node;
      if (position === "center") {
        if (node.panes.includes(paneId)) {
          return activate ? {...node, active: paneId, minimized: false} : node;
        }
        const index = before ? node.panes.indexOf(before) : -1;
        const panes = index >= 0
          ? [...node.panes.slice(0, index), paneId, ...node.panes.slice(index)]
          : [...node.panes, paneId];
        return {...node, panes, active: activate || !node.panes.length ? paneId : node.active};
      }
      const orientation: Orientation = position === "left" || position === "right" ? "row" : "column";
      const leading = position === "left" || position === "top";
      const added = group([paneId]);
      const children = leading ? [added, node] : [node, added];
      const [targetWeight, addedWeight] = edgeWeights;
      return split(
        orientation,
        children,
        leading ? [addedWeight, targetWeight] : [targetWeight, addedWeight],
      );
    }
    return {...node, children: node.children.map(walk)};
  };
  return normalize(walk(root));
}

function shape(node: LayoutNode): string {
  if (node.type === "group") return `[${node.panes.join(",")}]`;
  return `${node.orientation}(${node.children.map(shape).join("|")})`;
}

export function movePane(
  root: LayoutNode,
  paneId: string,
  target: {groupId: string; position: DropPosition; before?: string | null},
): LayoutNode {
  const origin = findGroupOfPane(root, paneId);
  if (origin?.id === target.groupId && origin.panes.length === 1 && target.position !== "center") return root;
  const without = removePane(root, paneId);
  if (!without || !findGroup(without, target.groupId)) return root;
  const inserted = insertAtGroup(without, target.groupId, paneId, target.position, target.before) || root;
  return shape(inserted) === shape(root) ? root : inserted;
}

export function reorderPane(root: LayoutNode, groupId: string, paneId: string, before: string | null): LayoutNode {
  return mapGroups(root, current => {
    if (current.id !== groupId || !current.panes.includes(paneId)) return current;
    const panes = current.panes.filter(value => value !== paneId);
    const index = before ? panes.indexOf(before) : -1;
    if (index >= 0) panes.splice(index, 0, paneId);
    else panes.push(paneId);
    return {...current, panes};
  });
}

export function updateSplitWeights(root: LayoutNode, splitId: string, weights: readonly number[], sizes?: Readonly<Record<string, number>>): LayoutNode {
  if (root.type === "group") return root;
  if (root.id === splitId && weights.length === root.children.length) {
    return {...root, weights: weights.map(value => Math.max(0.05, Number(value) || 1)), sizes: sizes || root.sizes};
  }
  return {...root, children: root.children.map(child => updateSplitWeights(child, splitId, weights, sizes))};
}

export function isLayoutNode(value: unknown, panes = new Set<string>(), nodes = new Set<string>()): value is LayoutNode {
  if (!value || typeof value !== "object") return false;
  const row = value as Record<string, unknown>;
  if (typeof row.id !== "string" || nodes.has(row.id)) return false;
  nodes.add(row.id);
  if (row.type === "group") {
    return typeof row.id === "string"
      && Array.isArray(row.panes)
      && row.panes.every(item => {if (typeof item !== "string" || panes.has(item)) return false; panes.add(item); return true;})
      && typeof row.active === "string";
  }
  return row.type === "split"
    && typeof row.id === "string"
    && (row.orientation === "row" || row.orientation === "column")
    && Array.isArray(row.children)
    && row.children.every(child => isLayoutNode(child, panes, nodes))
    && Array.isArray(row.weights)
    && row.weights.length === row.children.length;
}
