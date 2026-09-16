import {useLayoutEffect, useRef, useState, type ReactNode} from "react";

/** Fixed-height lists keep a bounded DOM even in repositories with many changes. */
export function VirtualList<T>({items, rowHeight, label, render, itemKey}: {
  items: readonly T[]; rowHeight: number; label: string;
  render: (item: T, index: number) => ReactNode; itemKey: (item: T, index: number) => string;
}) {
  const host = useRef<HTMLDivElement>(null);
  const [view, setView] = useState({top:0,height:400});
  useLayoutEffect(() => {
    const element = host.current!;
    const measure = () => setView(value => {
      const next = {top:element.scrollTop,height:element.clientHeight || 400};
      return value.top === next.top && value.height === next.height ? value : next;
    });
    const observer = new ResizeObserver(measure); observer.observe(element); measure();
    element.addEventListener("scroll",measure,{passive:true});
    return () => {observer.disconnect();element.removeEventListener("scroll",measure);};
  }, []);
  const start = Math.max(0,Math.min(Math.floor(view.top / rowHeight) - 6,Math.max(0,items.length - 1)));
  const end = Math.min(items.length,start + Math.ceil(view.height / rowHeight) + 12);
  return <div ref={host} className="deck-virtual-list" role="list" aria-label={label} tabIndex={0}>
    <div style={{height:start * rowHeight}} aria-hidden="true"/>
    {items.slice(start,end).map((item,index) => <div key={itemKey(item,start + index)} role="listitem"
      aria-posinset={start + index + 1} aria-setsize={items.length} style={{height:rowHeight}}>{render(item,start + index)}</div>)}
    <div style={{height:(items.length - end) * rowHeight}} aria-hidden="true"/>
  </div>;
}
