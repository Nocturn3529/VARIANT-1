import {useEffect, useLayoutEffect, useRef, useState, type ReactNode, type RefObject} from "react";
import {createPortal} from "react-dom";

/** One viewport-bounded surface for composer menus in narrow workspaces. */
export function AnchoredPopover({anchor, children, className, id, label, onClose, width = 340, focusSelector}: {
  anchor: RefObject<HTMLButtonElement | null>;
  children: ReactNode;
  className: string;
  id: string;
  label: string;
  onClose: () => void;
  width?: number;
  focusSelector?: string;
}) {
  const panel = useRef<HTMLElement>(null);
  const [position, setPosition] = useState({left:8,top:8,width,maxHeight:400});
  const document = anchor.current?.ownerDocument || window.document;

  useLayoutEffect(() => {
    const trigger = anchor.current, surface = panel.current, view = document.defaultView;
    if (!trigger || !surface || !view) return;
    let frame = 0;
    const place = () => {
      const rect = trigger.getBoundingClientRect(), gap = 8;
      const above = rect.top - gap * 2, below = view.innerHeight - rect.bottom - gap * 2;
      const up = above >= 180 || above >= below;
      const available = Math.max(64, up ? above : below);
      const nextWidth = Math.min(width, view.innerWidth - gap * 2);
      const maxHeight = Math.min(480, available, view.innerHeight - gap * 2);
      const height = Math.min(surface.scrollHeight || maxHeight, maxHeight);
      const next = {
        left:Math.max(gap,Math.min(rect.left,view.innerWidth-nextWidth-gap)),
        top:Math.max(gap,Math.min(up ? rect.top-gap-height : rect.bottom+gap,view.innerHeight-height-gap)),
        width:nextWidth,maxHeight,
      };
      setPosition(previous => Object.keys(next).every(key => previous[key as keyof typeof next] === next[key as keyof typeof next]) ? previous : next);
    };
    const schedule = (event?: Event) => {
      if (event?.type === "scroll" && event.target instanceof Node && surface.contains(event.target)) return;
      view.cancelAnimationFrame(frame);frame=view.requestAnimationFrame(place);
    };
    place();
    const observer = new ResizeObserver(() => schedule());observer.observe(surface);observer.observe(trigger);
    view.addEventListener("resize",schedule);document.addEventListener("scroll",schedule,true);
    return () => {observer.disconnect();view.cancelAnimationFrame(frame);view.removeEventListener("resize",schedule);document.removeEventListener("scroll",schedule,true);};
  },[anchor,document,width]);

  useEffect(() => {
    const view=document.defaultView;
    const frame=view?.requestAnimationFrame(() => {
      const target=focusSelector ? panel.current?.querySelector<HTMLElement>(focusSelector) : panel.current;
      target?.focus({preventScroll:true});
    });
    return () => {if(frame) view?.cancelAnimationFrame(frame);};
  },[document,focusSelector]);

  useEffect(() => {
    const within=(target:EventTarget|null) => target instanceof Node && (panel.current?.contains(target) || anchor.current?.contains(target));
    const outside=(event:PointerEvent) => {if(!within(event.target)) onClose();};
    const focus=(event:FocusEvent) => {if(!within(event.target)) onClose();};
    const escape=(event:KeyboardEvent) => {
      if(event.key!=="Escape" || event.isComposing || event.keyCode===229) return;
      event.preventDefault();event.stopImmediatePropagation();onClose();anchor.current?.focus();
    };
    document.addEventListener("pointerdown",outside);document.addEventListener("focusin",focus);document.addEventListener("keydown",escape,true);
    return () => {document.removeEventListener("pointerdown",outside);document.removeEventListener("focusin",focus);document.removeEventListener("keydown",escape,true);};
  },[anchor,document,onClose]);

  return createPortal(<section ref={panel} id={id} className={`composer-popover ${className}`} role="dialog" aria-label={label} tabIndex={-1} style={position}>{children}</section>,document.body);
}
