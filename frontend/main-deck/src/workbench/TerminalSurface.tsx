import {focusMainComposer} from "../ui/SurfaceDocument";
import {useEffect, useRef, useState} from "react";
import {createPortal} from "react-dom";
import {Terminal} from "@xterm/xterm";
import {FitAddon} from "@xterm/addon-fit";
import {SerializeAddon} from "@xterm/addon-serialize";
import {Unicode11Addon} from "@xterm/addon-unicode11";
import {WebLinksAddon} from "@xterm/addon-web-links";
import {
  openNewTerminal,
  writeTerminalInputFor,
  resizeTerminalFor,
  useTerminalState,
} from "../context/terminalStore";
import {useTerminalSlot} from "./terminalSlot";
import {retainChatDraft} from "../chat/stateCore";

function terminalTheme() {
  return {
    background: "#090a0a",
    foreground: "#d8dde6",
    cursor: "#e9e7df",
    cursorAccent: "#090a0a",
    selectionBackground: "#46647a88",
    black: "#11151a",
    red: "#ef6b73",
    green: "#65d6ad",
    yellow: "#e6c36a",
    blue: "#7aa2f7",
    magenta: "#bb9af7",
    cyan: "#74d5de",
    white: "#d8dde6",
    brightBlack: "#596273",
    brightRed: "#ff7a83",
    brightGreen: "#7be0bb",
    brightYellow: "#f0cf7d",
    brightBlue: "#8db1ff",
    brightMagenta: "#c9a8ff",
    brightCyan: "#88e2e9",
    brightWhite: "#f4f7fb",
  };
}

type TerminalSnapshot={text:string;end:number;cols:number;rows:number};
const snapshots=new Map<string,TerminalSnapshot>();

function TerminalInstance({id,active,live,output,outputStart,cols,rows,chatId}:{chatId:string;id:string;active:boolean;live:boolean;output:string;outputStart:number;cols:number;rows:number}) {
  const hostRef = useRef<HTMLDivElement | null>(null);
  const terminalRef = useRef<Terminal | null>(null);
  const fitRef = useRef<FitAddon | null>(null);
  const serializeRef = useRef<SerializeAddon | null>(null);
  const queuedEnd=useRef(0),appliedEnd=useRef(0),readyRef=useRef(false);
  const snapshotTimer=useRef<ReturnType<typeof setTimeout>|null>(null);
  const snapshotKey=JSON.stringify([chatId,id]);
  const saveSnapshot=()=>{
    const terminal=terminalRef.current,serialize=serializeRef.current;
    if(!terminal || !serialize || !readyRef.current)return;
    snapshots.delete(snapshotKey);
    snapshots.set(snapshotKey,{text:serialize.serialize({scrollback:300}),end:appliedEnd.current,cols:terminal.cols,rows:terminal.rows});
    while(snapshots.size>12)snapshots.delete(snapshots.keys().next().value!);
  };
  const queueSnapshot=()=>{
    if(snapshotTimer.current)clearTimeout(snapshotTimer.current);
    snapshotTimer.current=setTimeout(()=>{snapshotTimer.current=null;saveSnapshot();},1500);
  };

  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    const snapshot=snapshots.get(snapshotKey);
    const terminal = new Terminal({
      cols:snapshot?.cols || cols || 120,rows:snapshot?.rows || rows || 30,
      // Unicode11 is an xterm proposed API. Hermes enables this explicitly;
      // without it the add-on throws during mount and can take down the whole
      // workbench when a durable terminal is restored.
      allowProposedApi: true,
      cursorBlink: active && live,
      disableStdin: !live,
      cursorStyle: "bar",
      convertEol: false,
      allowTransparency: true,
      scrollback: 5000,
      fontFamily: "'Cascadia Code', 'SFMono-Regular', Consolas, monospace",
      fontSize: 12,
      lineHeight: 1.18,
      theme: terminalTheme(),
    });
    const fit = new FitAddon();
    const serialize = new SerializeAddon();
    const unicode = new Unicode11Addon();
    terminal.loadAddon(fit);
    terminal.loadAddon(serialize);
    terminal.loadAddon(unicode);
    terminal.unicode.activeVersion = "11";
    terminal.loadAddon(new WebLinksAddon((_event, uri) => {
      void window.variant1Deck?.openExternal?.(uri);
    }));
    terminal.open(host);
    // Electron defaults to software graphics. The DOM renderer avoids an extra
    // software WebGL context per terminal and remains stable across documents.
    terminalRef.current=terminal;fitRef.current=fit;serializeRef.current=serialize;
    const retainedEnd=snapshot?.end ?? outputStart;
    const initialEnd=Math.max(retainedEnd,outputStart+output.length);
    queuedEnd.current=initialEnd;appliedEnd.current=retainedEnd;
    readyRef.current=false;
    const initial=(snapshot?.text || "")+output.slice(Math.max(0,retainedEnd-outputStart));
    const ready=()=>{appliedEnd.current=initialEnd;readyRef.current=true;host.dispatchEvent(new Event("terminal-ready"));};
    if(initial)terminal.write(initial,ready);else ready();
    const input = terminal.onData(data => {
      writeTerminalInputFor(id, data,chatId);
    });
    const clipboard = (host.ownerDocument.defaultView || window).navigator.clipboard;
    terminal.attachCustomKeyEventHandler(event => {
      if (event.type !== "keydown") return true;
      const key = event.key.toLowerCase();
      if ((event.ctrlKey || event.metaKey) && event.shiftKey && key === "c") {
        if (terminal.hasSelection()) void clipboard.writeText(terminal.getSelection());
        return false;
      }
      if ((event.ctrlKey || event.metaKey) && event.shiftKey && key === "v") {
        void clipboard.readText().then(value => writeTerminalInputFor(id, value,chatId));
        return false;
      }
      if (event.ctrlKey && !event.shiftKey && key === "c" && terminal.hasSelection()) {
        void clipboard.writeText(terminal.getSelection());
        return false;
      }
      if ((event.ctrlKey || event.metaKey) && key === "l" && terminal.hasSelection()) {
        retainChatDraft(chatId,terminal.getSelection().slice(-12_000));
        focusMainComposer();
        return false;
      }
      return true;
    });
    const clearScrollback=(event:Event)=>{const detail=(event as CustomEvent).detail;if(detail?.id===id && detail?.chatId===chatId)terminal.write("\x1b[3J");};
    window.addEventListener("variant1:terminal-clear-scrollback",clearScrollback);
    terminalRef.current = terminal;
    fitRef.current = fit;
    serializeRef.current = serialize;
    return () => {
      window.removeEventListener("variant1:terminal-clear-scrollback",clearScrollback);
      if(snapshotTimer.current)clearTimeout(snapshotTimer.current);snapshotTimer.current=null;
      saveSnapshot();input.dispose();
      terminal.dispose();
      terminalRef.current = null;
      fitRef.current = null;
      serializeRef.current = null;
    };
  }, [id]);

  useEffect(() => {
    if (terminalRef.current) {
      terminalRef.current.options.disableStdin = !live;
      terminalRef.current.options.cursorBlink = active && live;
    }
  }, [active, live]);

  useEffect(() => {
    if (!active) return;
    const terminal = terminalRef.current;
    if (!terminal) return;
    const end=outputStart+output.length;
    if(end<=queuedEnd.current)return;
    const from=Math.max(0,queuedEnd.current-outputStart);
    const delta=output.slice(from);
    queuedEnd.current=end;
    if(delta)terminal.write(delta,()=>{appliedEnd.current=end;queueSnapshot();});
  },[active,output,outputStart,id]);

  useEffect(()=>{
    if(!active)return;
    const host=hostRef.current;if(!host)return;
    const ownerWindow=host.ownerDocument.defaultView || window;
    let timer:ReturnType<typeof setTimeout>|null=null,frame=0;
    const fit=()=>{
      timer=null;
      if(!readyRef.current || !host.isConnected || host.getBoundingClientRect().width<2 || host.getBoundingClientRect().height<2)return;
      try {
        const size=fitRef.current?.proposeDimensions(),terminal=terminalRef.current;
        if(size && terminal && size.cols>1 && size.rows>1){
          if(terminal.cols!==size.cols || terminal.rows!==size.rows)terminal.resize(size.cols,size.rows);
          resizeTerminalFor(id,size.cols,size.rows,chatId);
        }
      }catch{/* hidden or detached during layout */}
    };
    const schedule=()=>{if(!timer)timer=setTimeout(()=>{frame=ownerWindow.requestAnimationFrame(fit);},50);};
    const observer=new (ownerWindow as Window & typeof globalThis).ResizeObserver(schedule);
    observer.observe(host);host.addEventListener("terminal-ready",schedule);schedule();
    terminalRef.current?.focus();
    return()=>{if(timer)clearTimeout(timer);ownerWindow.cancelAnimationFrame(frame);observer.disconnect();host.removeEventListener("terminal-ready",schedule);};
  },[active,id,chatId]);

  return <div
    ref={hostRef}
    className={`workbench-terminal-instance${active ? " is-active" : ""}`}
    aria-hidden={!active}
    onMouseDown={() => terminalRef.current?.focus()}
    onContextMenu={event => {
      event.preventDefault();
      const terminal = terminalRef.current;
      if (!terminal) return;
      const clipboard = (event.currentTarget.ownerDocument.defaultView || window).navigator.clipboard;
      if (terminal.hasSelection()) void clipboard.writeText(terminal.getSelection());
      else void clipboard.readText().then(value => writeTerminalInputFor(id, value,chatId));
    }}
  />;
}

export function PersistentTerminalSurface({chatId = ""}: {chatId?:string}) {
  const slot = useTerminalSlot(chatId);
  const ownerDocument = slot?.ownerDocument || document;
  const ownerWindow = ownerDocument.defaultView || window;
  const state = useTerminalState(chatId);
  const [rect, setRect] = useState<DOMRect | null>(null);

  useEffect(() => {
    if (!slot) { setRect(null); return; }
    const sync = () => {
      const next = slot.getBoundingClientRect();
      const style = getComputedStyle(slot);
      const visible = slot.isConnected && style.visibility !== "hidden" && style.display !== "none"
        && next.width > 2 && next.height > 2;
      setRect(previous => {
        if (!visible) return previous === null ? previous : null;
        if (previous && previous.left === next.left && previous.top === next.top && previous.width === next.width && previous.height === next.height) return previous;
        return next;
      });
    };
    sync();
    let frame=0;
    const schedule=()=>{if(!frame)frame=ownerWindow.requestAnimationFrame(()=>{frame=0;sync();});};
    const observer = new ResizeObserver(schedule);
    observer.observe(slot);
    ownerWindow.addEventListener("resize", schedule);
    ownerWindow.addEventListener("scroll", schedule, true);
    const mutation = new MutationObserver(records=>{
      if(records.some(record=>!(record.target as Element).closest?.(".workbench-terminal-surface")))schedule();
    });
    mutation.observe(ownerDocument.body, {attributes: true, subtree: true, attributeFilter: ["class", "style", "hidden"]});
    return () => {
      ownerWindow.cancelAnimationFrame(frame);observer.disconnect();
      mutation.disconnect();
      ownerWindow.removeEventListener("resize", schedule);
      ownerWindow.removeEventListener("scroll", schedule, true);
    };
  }, [slot, ownerDocument]);

  if (!ownerDocument.body) return null;
  const selected = state.terminals.find(item => item.id === state.activeId);
  return createPortal(<div
    className={`workbench-terminal-surface${rect ? " is-visible" : ""}`}
    style={rect ? {left: rect.left, top: rect.top, width: rect.width, height: rect.height} : undefined}
  >
    {selected ? <TerminalInstance
      chatId={chatId} id={selected.id} active={!!rect} live={selected.state === "running" || selected.state === "starting"}
      output={state.output} outputStart={state.outputStart} cols={selected.cols} rows={selected.rows} key={selected.id}
    /> : <div className="workbench-terminal-empty">
      <button type="button" disabled={!state.connected || state.opening} onClick={() => void openNewTerminal(undefined,chatId)}>{state.opening ? "Opening terminal…" : "Open terminal"}</button>
    </div>}
  </div>, ownerDocument.body);
}
