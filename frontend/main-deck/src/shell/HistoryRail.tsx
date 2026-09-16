import {openChatView,registerChatView} from "../workbench/chatViewStore";
import {chooseChatProject,useChatProjects} from "../state/chatProjectStore";
import {Icon} from "../ui/Icon";
import {KernelGlyph} from "../motion/KernelGlyph";
import {useChatState} from "../chatStore";
import {focusMainComposer, useSurfaceDocument} from "../ui/SurfaceDocument";
import {createPortal} from "react-dom";
import {useEffect, useLayoutEffect, useMemo, useRef, useState} from "react";
import {
  deleteSession,
  renameSession,
  requestNewSession,
  setOpenSessionMenu,
  setSessionArchived,
  setSessionPinned,
  setSessionSearchQuery,
  switchSession,
  useSessionState,
  type SessionSummary,
} from "../state/sessionStore";
import {closeMobileHistory, navigateTo} from "../state/appStore";
import {EmptyState} from "../ui/EmptyState";
import {TextInputDialog} from "../ui/TextInputDialog";
import {useQuestionChatIds} from "../state/clarificationStore";

function groupFor(session: SessionSummary): string {
  if (session.archived) return "Archived";
  if (session.pinned) return "Pinned";
  const raw = Number(session.updatedAt || 0);
  const time = raw > 1e12 ? raw : raw * 1000;
  const age = Date.now() - time;
  if (!time || age < 86400_000) return "Today";
  if (age < 7 * 86400_000) return "Previous 7 days";
  return "Older";
}

function SessionMenu({
  session,
  position,
  returnFocus,
  onRename,
}: {
  session: SessionSummary;
  position: {left: number; top: number};
  returnFocus: HTMLButtonElement | null;
  onRename: () => void;
}) {
  const ownerDocument = useSurfaceDocument();
  const ownerWindow = ownerDocument.defaultView || window;
  const menuRef = useRef<HTMLDivElement>(null);
  const restoreFocusRef = useRef(false);
  const [measuredPosition, setMeasuredPosition] = useState(position);

  useLayoutEffect(() => {
    const menu = menuRef.current;
    if (!menu) return;
    const place = () => {
      const rect = menu.getBoundingClientRect();
      const anchor = returnFocus?.getBoundingClientRect();
      const next = {
        left: Math.max(8, Math.min((anchor?.right ?? position.left) - rect.width, ownerWindow.innerWidth - rect.width - 8)),
        top: Math.max(8, Math.min(anchor ? anchor.bottom + 4 : position.top, ownerWindow.innerHeight - rect.height - 8)),
      };
      setMeasuredPosition(previous => previous.left === next.left && previous.top === next.top ? previous : next);
    };
    const resize = new ResizeObserver(place);
    resize.observe(menu); ownerWindow.addEventListener("resize", place); ownerWindow.addEventListener("scroll", place, true);
    place();
    return () => {resize.disconnect();ownerWindow.removeEventListener("resize", place);ownerWindow.removeEventListener("scroll", place, true);};
  }, [returnFocus, position.left, position.top, ownerWindow]);

  useEffect(() => {
    const closeFromPointer = () => setOpenSessionMenu(null);
    ownerDocument.addEventListener("pointerdown", closeFromPointer);
    requestAnimationFrame(() => {
      menuRef.current?.querySelector<HTMLButtonElement>('[role="menuitem"]')?.focus();
    });
    return () => {
      ownerDocument.removeEventListener("pointerdown", closeFromPointer);
      if (restoreFocusRef.current && returnFocus?.isConnected) {
        requestAnimationFrame(() => returnFocus.focus());
      }
    };
  }, [returnFocus, ownerDocument]);

  const close = (restoreFocus = true) => {
    restoreFocusRef.current = restoreFocus;
    setOpenSessionMenu(null);
  };

  const activate = (action: () => void) => {
    action();
    close();
  };

  function rename() {
    onRename();
    close(false);
  }

  return createPortal(
    <div
      ref={menuRef}
      id="runtime-session-menu"
      className="runtime-session-menu"
      role="menu"
      style={measuredPosition}
      onPointerDown={event => event.stopPropagation()}
      onKeyDown={event => {
        const items = [...event.currentTarget.querySelectorAll<HTMLButtonElement>('[role="menuitem"]')];
        const current = items.indexOf(ownerDocument.activeElement as HTMLButtonElement);
        let next = current;
        if (event.key === "ArrowDown") next = (current + 1 + items.length) % items.length;
        else if (event.key === "ArrowUp") next = (current - 1 + items.length) % items.length;
        else if (event.key === "Home") next = 0;
        else if (event.key === "End") next = items.length - 1;
        else if (event.key === "Escape") {
          event.preventDefault();
          event.stopPropagation();
          close();
          return;
        } else if (event.key === "Tab") {
          event.preventDefault();
          event.stopPropagation();
          close();
          return;
        } else return;
        event.preventDefault();
        items[next]?.focus();
      }}
    >
      <button className="runtime-session-menu__item" role="menuitem" onClick={()=>activate(()=>openChatView(session.id,session.title,"center"))}>Open as tab</button>
      <button className="runtime-session-menu__item" role="menuitem" onClick={()=>activate(()=>openChatView(session.id,session.title,"right"))}>Open beside</button>
      <button className="runtime-session-menu__item" role="menuitem" onClick={()=>activate(()=>openChatView(session.id,session.title,"bottom"))}>Open below</button>
      {window.variant1Deck?.openChatWindow ? <button className="runtime-session-menu__item" role="menuitem" onClick={()=>activate(()=>{void window.variant1Deck?.openChatWindow?.(session.id,session.title);})}>Detach chat</button>:null}
      <button className="runtime-session-menu__item" role="menuitem" type="button" onClick={rename}>
        Rename
      </button>
      <button
        className="runtime-session-menu__item"
        role="menuitem"
        type="button"
        onClick={() => activate(() => setSessionPinned(session.id, !session.pinned))}
      >
        {session.pinned ? "Unpin" : "Pin"}
      </button>
      <button
        className="runtime-session-menu__item"
        role="menuitem"
        type="button"
        onClick={() => activate(() => setSessionArchived(session.id, !session.archived))}
      >
        {session.archived ? "Unarchive" : "Archive"}
      </button>
      <button
        className="runtime-session-menu__item runtime-session-menu__item--danger"
        role="menuitem"
        type="button"
        onClick={() => {
          if (ownerWindow.confirm("Delete this conversation permanently?")) {
            deleteSession(session.id);
            close();
          }
        }}
      >
        Delete
      </button>
    </div>,
    returnFocus?.closest(".workbench-group") || ownerDocument.body,
  );
}

function SessionButton({
  session,
  active,
  working,
  mutation = false,
  hasQuestion = false,
  menuOpen = false,
  onMenu,
}: {
  session: SessionSummary;
  active: boolean;
  working: boolean;
  mutation?: boolean;
  hasQuestion?: boolean;
  menuOpen?: boolean;
  onMenu: (button: HTMLButtonElement) => void;
}) {
  const title = session.title || "New chat";
  return <div
    className={`history-item${active ? " active" : ""}${working ? " history-item--working" : ""}`}
    draggable
    onDragStart={event=>{event.dataTransfer.effectAllowed="move";event.dataTransfer.setData("application/x-variant1-pane",registerChatView(session.id,title));}}
    data-session-id={session.id}
    data-working={working ? "1" : undefined}
    data-needs-answer={hasQuestion ? "1" : undefined}
    title={title}
  >
    <button
      className="history-item__select"
      type="button"
      aria-current={active ? "true" : undefined}
      onClick={() => {
        switchSession(session.id);
        focusMainComposer();
        closeMobileHistory();
      }}
    >
      {working ? <KernelGlyph seed={session.id} size={16} phase="running" mutation={mutation}/> : null}
      <span className="history-item__title">{title}</span>
      {hasQuestion ? <span className="history-item__question" aria-label="Answer needed" title="Answer needed">?</span> : null}
    </button>
    <button
      type="button"
      className="history-item__menu"
      aria-label={`Actions for ${title}`}
      aria-haspopup="menu"
      aria-expanded={menuOpen}
      title={`Actions for ${title}`}
      onClick={event => onMenu(event.currentTarget)}
    >
      •••
    </button>
  </div>;
}

export function HistoryRail() {
  const sessionState = useSessionState();
  const chat = useChatState();
  const questionChats = useQuestionChatIds();
  const [menuPosition, setMenuPosition] = useState({left: 8, top: 8});
  const [menuTrigger, setMenuTrigger] = useState<HTMLButtonElement | null>(null);
  const [renaming, setRenaming] = useState<SessionSummary | null>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  const projectState=useChatProjects();
  const order = ["Pinned", "Today", "Previous 7 days", "Older", "Archived"];
  const groups = useMemo(() => {
    const projects=new Map<string,{label:string;items:SessionSummary[]}>();
    const unbound:SessionSummary[]=[];
    for(const item of sessionState.items) {
      const project=projectState.projects[item.id] !== undefined ? projectState.projects[item.id] : item.project;
      if(!project){unbound.push(item);continue;}
      const group=projects.get(project.root) || {label:project.name,items:[]};group.items.push(item);projects.set(project.root,group);
    }
    return [...projects.entries()].map(([root,g])=>({...g,key:root})).concat(order.map(label=>({key:label,label,items:unbound.filter(item=>groupFor(item)===label)})).filter(g=>g.items.length));
  }, [sessionState.items,projectState.projects]);
  const openSession = sessionState.items.find(
    item => item.id === sessionState.openMenuId,
  );

  function openMenu(session: SessionSummary, button: HTMLButtonElement) {
    const rect = button.getBoundingClientRect();
    setMenuPosition({left: rect.right, top: rect.bottom + 4});
    setMenuTrigger(button);
    setOpenSessionMenu(session.id);
  }

  return <>
    <div className="history-panel__header">
      <div>
        <span className="eyebrow">History</span>
        <h1>Chats</h1>
      </div>

    </div>

    <button
      className="new-chat-button"
      type="button"
      id="new-chat"
      onClick={() => {
        if (requestNewSession()) {
          focusMainComposer();
          closeMobileHistory();
        }
      }}
    >
      <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14M5 12h14"/></svg>
      <span>New chat</span>
      <kbd>Ctrl N</kbd>
    </button>

    <button type="button" className="history-utility-button" aria-haspopup="dialog" onClick={() => navigateTo("automations")}>
      <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="8"/><path d="M12 7v5l3 2"/></svg><span>Scheduled jobs</span>
    </button>

    <button className="history-utility-button" disabled={!sessionState.displayedSessionId || !!projectState.pending[sessionState.displayedSessionId]} onClick={()=>void chooseChatProject(sessionState.displayedSessionId || "")}><Icon name="folder"/><span>Choose project folder</span></button>
    {sessionState.displayedSessionId && projectState.errors[sessionState.displayedSessionId] ? <p role="alert">{projectState.errors[sessionState.displayedSessionId]}</p>:null}

    <label className="history-search" htmlFor="history-search-input">
      <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="7"/><path d="m20 20-4-4"/></svg>
      <input
        ref={searchRef}
        id="history-search-input"
        type="search"
        placeholder="Search conversations"
        autoComplete="off"
        value={sessionState.searchQuery}
        onChange={event => setSessionSearchQuery(event.target.value)}
      />
    </label>

    <div className="history-scroll" id="history-list" data-fixture-root="history">
      {sessionState.loading ? <EmptyState title="Loading conversations…" /> : null}
      {!sessionState.loading && sessionState.error
        ? <EmptyState title={sessionState.error} />
        : null}
      {!sessionState.loading && !sessionState.error && sessionState.searchQuery.trim()
        ? <section className="history-group" data-group="Search results">
            <h2>Search results</h2>
            {(sessionState.searchResults || []).map(hit => <SessionButton
              key={`${hit.id}-${hit.ts}`}
              session={hit}
              active={hit.id === sessionState.displayedSessionId}
              working={sessionState.workingSessionIds.includes(hit.id)}
              mutation={hit.id === chat.sessionId && !!chat.runtime?.mutationEffectiveEnabled}
              hasQuestion={questionChats.includes(hit.id)}
              menuOpen={hit.id === sessionState.openMenuId}
              onMenu={button => openMenu(hit, button)}
            />)}
            {sessionState.searchResults?.length === 0
              ? <EmptyState title="No matching conversations" />
              : null}
          </section>
        : <>
            {groups.map(group => <section className="history-group" data-group={group.label} key={group.key}>
              <h2 title={group.key}>{group.label}</h2>
              {group.items.map(session => <SessionButton
                key={session.id}
                session={session}
                active={session.id === sessionState.displayedSessionId}
                working={sessionState.workingSessionIds.includes(session.id)}
                mutation={session.id === chat.sessionId && !!chat.runtime?.mutationEffectiveEnabled}
                hasQuestion={questionChats.includes(session.id)}
                menuOpen={session.id === sessionState.openMenuId}
                onMenu={button => openMenu(session, button)}
              />)}
            </section>)}
          </>}
      {!sessionState.loading
        && !sessionState.error
        && !sessionState.searchQuery.trim()
        && !sessionState.items.length
        ? <EmptyState title="No conversations yet" />
        : null}
    </div>

    {openSession
      ? <SessionMenu session={openSession} position={menuPosition} returnFocus={menuTrigger} onRename={() => setRenaming(openSession)}/>
      : null}
    {renaming ? <TextInputDialog title="Rename conversation" label="Title" initialValue={renaming.title || "New chat"}
      returnFocus={menuTrigger} onSubmit={title => renameSession(renaming.id, title)} onClose={() => setRenaming(null)}/> : null}
  </>;
}
