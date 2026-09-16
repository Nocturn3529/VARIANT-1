import {Icon, type IconName} from "../ui/Icon";
/**
 * Input, attachment, context, model, and microphone controls for Chat.
 */
import {
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type CSSProperties,
  type RefObject,
} from "react";
import {
  addChatFiles,
  cancelChatTurn,
  notifyChat,
  removeChatAttachment,
  setChatDraft,
  setMutationWriteEnabled,
  useChatState,
  getChatState,
  type ChatAttachment,
} from "../chatStore";
import {cancelMic, toggleMic, useMicState} from "../state/micStore";
import {
  requestSessionContext,
  getContextForSession,
  changeSessionSettings,
  useSessionContextState,
  type SessionContextState,
} from "../sessionContextStore";
import {ClarificationCard} from "./ClarificationCard";
import {getSessionState,useSessionState} from "../state/sessionStore";
import {setChatDelivery, submitUserInput} from "./composer";
import {getComposerRevision,turnApi} from "./stateCore";
import {currentPause,requestChatPause} from "./pause";
import {InputQueuePanel} from "./InputQueuePanel";
import {ComposerGoalPanel} from "./ComposerGoalPanel";
import {AgentTeamPanel} from "./AgentTeamPanel";
import {parseGoalCommand} from "./goalCommand";
import {queueAdmissionPending} from "./inputQueue";
import {invalidatePendingChatAttachments} from "./attachments";
import {AnchoredPopover} from "../ui/AnchoredPopover";
import {formatTokenCount} from "./receipt";
import {
  ModelPickerControl,
  reasoningEffortLabel,
  type ComposerModelSelection,
} from "./ModelPicker";
import {mutationToggleControlState} from "./mutationControl";
import {MicGlyph, MutationSwitch} from "./ComposerMotion";
import {PeerControl} from "../peers/PeerControl";
import type {RuntimeApi} from "../types";

declare global {
  interface Window {
    variant1Deck?: RuntimeApi;
  }
}

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

function chipIcon(item: ChatAttachment): IconName {
  if (item.kind === "image") return "image";
  if (item.kind === "folder") return "folder";
  if (item.kind === "path") return "attach";
  return "file";
}

function ComposerChips({attachments, disabled}: {attachments: ChatAttachment[]; disabled: boolean}) {
  if (!attachments.length) return <div className="composer__chips" id="composer-chips" />;
  return <div className="composer__chips" id="composer-chips">
    {attachments.map(item => (
      <button
        key={item.id}
        type="button"
        className={`context-chip${item.kind === "image" ? " context-chip--image" : ""}`}
        onClick={() => { if (!disabled) removeChatAttachment(item.id); }}
        title={disabled ? (item.path || item.name) : `Remove ${item.path || item.name}`}
        disabled={disabled}
        aria-label={`Remove attachment ${item.name}`}
      >
        {item.kind === "image" && item.previewUrl
          ? <img className="context-chip__thumb" src={item.previewUrl} alt="" />
          : <Icon className="context-chip__icon" name={chipIcon(item)}/>}
        <span className="context-chip__label">{item.name}</span>
        <span className="context-chip__meta">
          {item.size > 0 ? formatBytes(item.size) : (item.path ? "path" : "")}
        </span>
        <span className="context-chip__remove" aria-hidden="true">×</span>
      </button>
    ))}
  </div>;
}

function ComposerCapabilities({
  connected,
  turnActive,
  blockedReason,
}: {
  connected: boolean;
  turnActive: boolean;
  blockedReason?: string;
}) {
  const {
    sessionId,
    runtime,
    mutationTogglePending,
  } = useChatState();
  const mutationControl = mutationToggleControlState({
    runtime,
    pending: mutationTogglePending,
    connected,
    turnActive,
    sessionId,
  });

  return <div className="composer__capabilities" role="group" aria-label="Session capabilities">
    {mutationControl.visible ? <MutationSwitch
      sessionId={sessionId}
      className="composer-mutation-control"
      framed
      checked={mutationControl.checked}
      disabled={mutationControl.disabled || !!blockedReason}
      label="Mutation"
      caption={mutationControl.valueText}
      title={blockedReason || mutationControl.title}
      aria-label={`Tool mutation ${mutationControl.checked ? "on" : "off"}`}
      onChange={next => {
        if (sessionId) setMutationWriteEnabled(next,sessionId);
      }}
    /> : null}
  </div>;
}

const COMPOSER_COMMANDS = [
  {
    name: "/remember",
    description: "Save an explicit durable memory",
    insert: "/remember ",
  },
  {
    name: "/system-status",
    description: "Show current device resource status",
    insert: "/system-status",
  },
  {
    name: "/goal",
    description: "Start an explicit durable goal",
    insert: "/goal ",
  },
] as const;

const CONTEXT_METER_SEGMENTS = 30;
type ComposerCommand = (typeof COMPOSER_COMMANDS)[number];

function formatContextTokens(value: number | null): string {
  if (value === null) return "-";
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}m`;
  if (value >= 1000) return `${(value / 1000).toFixed(value >= 10_000 ? 0 : 1)}k`;
  return String(Math.round(value));
}

function ComposerCommandMenu({
  commands,
  onChoose,
  activeIndex,
  onHighlight,
}: {
  commands: readonly ComposerCommand[];
  onChoose: (command: ComposerCommand) => void;
  activeIndex: number;
  onHighlight: (index:number) => void;
}) {
  if (!commands.length) return null;

  return <div
    className="composer-command-menu"
    id="composer-command-menu"
    role="listbox"
    aria-label="Commands"
  >
    {commands.map((command,index) => (
      <button
        type="button"
        role="option"
        id={`composer-command-${index}`}
        aria-selected={index===activeIndex}
        tabIndex={-1}
        key={command.name}
        onMouseDown={event => event.preventDefault()}
        onClick={() => onChoose(command)}
        onMouseEnter={() => onHighlight(index)}
      >
        <strong>{command.name}</strong>
        <span>{command.description}</span>
      </button>
    ))}
  </div>;
}

function ContextMeter({
  context,
  sessionId,
  open,
  buttonRef,
  onToggle,
  onClose,
}: {
  context: SessionContextState;
  sessionId: string | null;
  open: boolean;
  buttonRef: RefObject<HTMLButtonElement | null>;
  onToggle: () => void;
  onClose: () => void;
}) {
  const measured=context.status==="ready";
  const filledSegments = context.percentUsed > 0
    ? Math.max(1, Math.ceil(context.percentUsed * CONTEXT_METER_SEGMENTS / 100))
    : 0;
  const categorizedTokens = context.categories.reduce(
    (total, category) => total + category.tokens,
    0,
  );
  const dominantCategory = context.categories.reduce(
    (dominant, category) => category.tokens > dominant.tokens ? category : dominant,
    context.categories[0],
  )?.id || "other";
  const segments = Array.from({length: CONTEXT_METER_SEGMENTS}, (_, index) => {
    if (index >= filledSegments) return "";
    if (categorizedTokens <= 0) return dominantCategory;
    const sampledToken = (index + .5) / filledSegments * categorizedTokens;
    let accumulatedTokens = 0;
    for (const category of context.categories) {
      accumulatedTokens += category.tokens;
      if (sampledToken <= accumulatedTokens) return category.id;
    }
    return dominantCategory;
  });

  return <div className="composer__context">
    <button
      type="button"
      className="context-meter-button"
      id="context-meter-button"
      ref={buttonRef}
      aria-haspopup="dialog"
      aria-expanded={open}
      aria-controls="context-meter-menu"
      aria-label={measured ? `Session context: ${context.percentUsed.toFixed(0)}% used` : "Session context: awaiting measurement"}
      title="Session context"
      disabled={!sessionId}
      onClick={onToggle}
    >
      <span
        className={`context-meter-ring${context.percentUsed >= 85 ? " is-tight" : ""}`}
        data-context-category={dominantCategory}
        aria-hidden="true"
        style={{
          "--context-fill": `${context.percentUsed * 3.6}deg`,
        } as CSSProperties}
      ><i /></span>
      <span>{measured ? `${context.percentUsed.toFixed(0)}%` : "Context"}</span>
    </button>
    {open ? <AnchoredPopover anchor={buttonRef} className="context-meter-menu" id="context-meter-menu" label="Session context details" onClose={onClose} width={380}>
      <header className="context-meter-menu__header deck-section__header">
        <span>
          <small>Session context</small>
          <strong>{measured ? `${context.percentUsed.toFixed(1)}% used` : "Awaiting measurement"}</strong>
        </span>
        <b>{formatContextTokens(context.usedTokens)} / {formatContextTokens(context.contextLimitTokens)}</b>
      </header>
      <div className="context-meter-total" aria-hidden="true">
        {segments.map((category, index) => <i
          className={category ? "is-active" : undefined}
          data-context-category={category || undefined}
          key={index}
        />)}
      </div>
      <div className="context-meter-summary">
        <span>{context.status === "ready"
          ? `${formatContextTokens(context.availableTokens)} tokens available`
          : "Waiting for the first model request"}</span>
        <span>{context.cachedInputTokens
          ? `${formatTokenCount(context.cachedInputTokens)} cached`
          : context.measurement.replaceAll("_", " ")}</span>
      </div>
      <div className="context-meter-breakdown" aria-label="Context breakdown">
        {context.categories.map(category => <div
          className="context-meter-row"
          data-context-category={category.id}
          key={category.id}
        >
          <span>{category.label}</span>
          <i aria-hidden="true"><b style={{
            width: `${context.percentUsed > 0
              ? Math.min(100, category.percent * 100 / context.percentUsed)
              : 0}%`,
          }} /></i>
          <em>{formatContextTokens(category.tokens)}</em>
          <strong>{category.percent.toFixed(1)}%</strong>
        </div>)}
      </div>
      <footer className="context-meter-menu__footer"><span>{context.connected ? "Latest model request" : "Last known measurement"}</span><button type="button" disabled={!context.connected} onClick={()=>requestSessionContext(sessionId)}><Icon name="refresh"/>Refresh</button></footer>
    </AnchoredPopover> : null}
  </div>;
}

export function ChatComposer() {
  const navigating = !!useSessionState().pendingAction;
  const {
    draft, turnActive, connected, attachments, attachmentsPreparing, deliveryMode, stopPending, queuedFollowUps, sessionId, messages, pendingActiveInputs, runtime, inputQueue,
  } = useChatState();
  const composerRevision = getComposerRevision(sessionId || "");
  const {phase: micPhase, sessionTitle: micChat, error: micError} = useMicState();
  const pendingIds = new Set(pendingActiveInputs.map(input => input.localId));
  const queued = turnActive ? messages.filter(message => message.role === "user" && (message.activeInputState === "queued" || (message.localId && pendingIds.has(message.localId)))) : [];
  const sessionContext = useSessionContextState();
  const composerContext=getContextForSession(sessionId || "");
  const pendingSetting=composerContext.settingsPending;
  const pauseControl=currentPause();
  const paused=pauseControl?.state==="paused" && pauseControl.synced;
  const pauseUnknown=pauseControl?.synced===false;
  const pauseBusy=!!pauseControl?.pending || pauseControl?.state==="pausing";
  const activeOwner=turnApi().snapshot();
  const pauseIdentity=!!(activeOwner.admissionId || activeOwner.runId || runtime?.activeAdmissionId || runtime?.activeRunId);
  const pauseStatus=pauseUnknown ? "Checking task pause state…" : pauseControl?.pending
    ? pauseControl.pending.action==="resume" ? "Resuming task…" : "Requesting pause…"
    : pauseControl?.state==="pausing" ? "Pausing after current step…"
    : paused ? "Paused · Messages stay queued until you resume" : "";
  const composerStatus=!connected ? "Offline · Your draft is kept" : navigating ? "Opening conversation…" : stopPending ? "Stopping task…" : pauseStatus || (pendingSetting ? "Waiting for model confirmation" : "");
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const zoneRef = useRef<HTMLElement | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const filePickerOwner = useRef<string | null>(null);
  const modelButtonRef = useRef<HTMLButtonElement | null>(null);
  const contextButtonRef = useRef<HTMLButtonElement | null>(null);
  const [dragOver, setDragOver] = useState(false);
  const [modelMenuOpen, setModelMenuOpen] = useState(false);
  const [contextMenuOpen, setContextMenuOpen] = useState(false);
  const [commandIndex,setCommandIndex]=useState(0);
  const [dismissedCommand,setDismissedCommand]=useState("");
  const composing=useRef(false);
  // Active-turn input is admitted immediately as steering or a follow-up.
  const goalCommand=parseGoalCommand(draft);
  const readyToSend=connected && !navigating && !!sessionId && !attachmentsPreparing && !stopPending && !pendingSetting && !queueAdmissionPending()
    && !(goalCommand && (turnActive || !!getChatState().goal.pending));
  const canActiveInput = readyToSend && turnActive && Boolean(draft.trim()) && !attachments.length;
  const canSend = readyToSend && Boolean(draft.trim() || attachments.length) && !turnActive;
  const slashQuery = (
    draft.startsWith("/") && !draft.slice(1).includes(" ")
      ? draft.toLowerCase()
      : ""
  );
  const commandMatches = slashQuery && dismissedCommand!==draft && !modelMenuOpen && !contextMenuOpen
    ? COMPOSER_COMMANDS.filter(command => command.name.startsWith(slashQuery))
    : [];

  useLayoutEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    const resize=()=>{el.style.height="0px";el.style.height=`${Math.min(el.scrollHeight,180,window.innerHeight*.24)}px`;};
    let width=el.clientWidth,frame=0;resize();
    const observer=new ResizeObserver(()=>{if(el.clientWidth!==width){width=el.clientWidth;cancelAnimationFrame(frame);frame=requestAnimationFrame(resize);}});observer.observe(el);
    return ()=>{observer.disconnect();cancelAnimationFrame(frame);};
  }, [draft,sessionId]);

  useEffect(()=>{setCommandIndex(0);},[slashQuery]);
  useEffect(()=>{setModelMenuOpen(false);setContextMenuOpen(false);setDismissedCommand("");setDragOver(false);composing.current=false;},[sessionId,navigating,turnActive,connected]);

  useEffect(() => {
    requestSessionContext(sessionId);
  }, [sessionId, sessionContext.connected]);

  // Drag-and-drop files/folders onto the composer zone.
  useEffect(() => {
    const zone = zoneRef.current;
    if (!zone) return;
    const onDragOver = (event: DragEvent) => {
      if (!event.dataTransfer?.types.includes("Files")) return;
      event.preventDefault();
      event.dataTransfer.dropEffect = "copy";
      setDragOver(true);
    };
    const onDragLeave = (event: DragEvent) => {
      if (event.target === zone || !zone.contains(event.relatedTarget as Node)) {
        setDragOver(false);
      }
    };
    const onDrop = (event: DragEvent) => {
      if (!event.dataTransfer?.types.includes("Files")) return;
      event.preventDefault();
      setDragOver(false);
      if (turnActive || navigating || !sessionId) {
        notifyChat(turnActive ? "Wait for VARIANT-1 to finish before attaching files" : "Wait for this chat to finish loading.");
        return;
      }
      const dt = event.dataTransfer;
      if (!dt) return;
      const files = dt.files;
      if (files && files.length) {
        void addChatFiles(files,sessionId);
        return;
      }
    };
    zone.addEventListener("dragover", onDragOver);
    zone.addEventListener("dragleave", onDragLeave);
    zone.addEventListener("drop", onDrop);
    return () => {
      zone.removeEventListener("dragover", onDragOver);
      zone.removeEventListener("dragleave", onDragLeave);
      zone.removeEventListener("drop", onDrop);
    };
  }, [turnActive,navigating,sessionId]);

  const send = (delivery: "steer" | "follow_up" = "steer") => {
    if(!(turnActive ? canActiveInput : canSend)) return;
    if(submitUserInput({source:"composer",text:draft,sessionId,revision:composerRevision,attachments},delivery)) textareaRef.current?.focus();
  };

  const chooseCommand = (command: ComposerCommand) => {
    setChatDraft(command.insert,sessionId);
    setDismissedCommand(command.insert);
    requestAnimationFrame(() => textareaRef.current?.focus());
  };

  const attachFiles = () => {
    // A Web File gives VARIANT-1 both Electron's absolute-path fast path and a
    // bounded byte fallback. The native path-only dialog cannot provide that
    // retry contract.
    filePickerOwner.current=sessionId;
    fileInputRef.current?.click();
  };

  const chooseModel = (selection: ComposerModelSelection) => {
    if(getChatState().sessionId!==sessionId || getSessionState().pendingAction || getChatState().turnActive) return;
    if (turnActive) {
      notifyChat("Wait for the active turn to finish before switching models");
      return;
    }
    if (!sessionId) {
      notifyChat("Open a chat session before switching models");
      return;
    }
    const payload = {
      type: "mode:set" as const,
      scope: "session" as const,
      id: sessionId,
      mode: selection.mode,
      provider: selection.mode === "cloud" ? selection.provider : "local",
      model: selection.model,
      ...(selection.reasoningEffort
        ? {reasoning_effort: selection.reasoningEffort}
        : {}),
    };
    if (navigating || !connected || !changeSessionSettings(sessionId,payload,selection.label)) {
      notifyChat("Could not update model route — still connecting");
      return;
    }
    setModelMenuOpen(false);
  };

  const chooseReasoningEffort = (effort: string) => {
    if(getChatState().sessionId!==sessionId || getSessionState().pendingAction || getChatState().turnActive) return;
    if (turnActive || !sessionId || navigating || !connected) return;
    if (!changeSessionSettings(sessionId,{
      type: "reasoning:effort:set",
      id: sessionId,
      effort,
    },`${reasoningEffortLabel(effort)} reasoning`)) {
      notifyChat("Could not update reasoning effort — still connecting");
      return;
    }
  };

  return <section
    className={`composer-zone${dragOver ? " composer-zone--drag" : ""}${queuedFollowUps ? " composer-zone--queued" : ""}`}
    aria-label="Message composer"
    ref={zoneRef}
  >
    {dragOver ? <div className="composer-drop-hint" aria-hidden="true">Drop files or folders to attach</div> : null}
    <ClarificationCard />
    <AgentTeamPanel/>
    <div className="composer" id="composer" data-state={!connected ? "offline" : stopPending ? "stopping" : turnActive ? "working" : "ready"}>
      <div className="composer__supplements">
        {composerStatus ? <p className="composer-status" id="composer-status" role="status">{composerStatus}</p> : null}
        <ComposerChips attachments={attachments} disabled={turnActive} />
        {attachmentsPreparing>0 ? <div className="composer-preparation" role="status"><i className="composer-spinner" aria-hidden="true"/><span>Preparing {attachmentsPreparing} {attachmentsPreparing===1 ? "attachment" : "attachments"}…</span><button type="button" onClick={invalidatePendingChatAttachments}>Cancel</button></div> : null}
        <InputQueuePanel/>
        <ComposerGoalPanel/>
        {!inputQueue.snapshot && (queued.length || queuedFollowUps) && turnActive ? <details className="composer-input-queue">
          <summary><Icon name="queue"/>Inputs for this task <span>{Math.max(queued.length, queuedFollowUps)}</span><Icon name="down"/></summary>
          {queued.map(message => <div className="composer-input-queue__item" key={message.localId || message.ticketId}>
            <span>{message.text}</span><small>{message.activeInputState !== "queued" ? "Sending…" : message.delivery === "follow_up" ? "Queued next" : "Steering queued"}</small>
          </div>)}
        </details> : null}
        {!["idle", "error"].includes(micPhase) ? <div className="composer-mic-status" role="status"><Icon name="mic"/><strong>{micPhase === "recording" ? "Recording" : micPhase === "requesting" ? "Opening microphone" : micPhase === "transcribing" ? "Transcribing" : "Preparing audio"}</strong><span>{micChat || "Current chat"}</span><div className="composer-mic-status__actions">{micPhase==="recording" ? <button type="button" onClick={toggleMic}>Finish recording</button> : null}<button type="button" onClick={cancelMic} disabled={micPhase==="encoding"}>{micPhase==="recording" ? "Discard" : "Cancel"}</button></div></div> : null}
        {micPhase === "error" && micError ? <p className="composer-mic-error" role="alert">{micError}</p> : null}
        {composerContext.settingsError ? <p className="composer-setting-error" role="alert">{composerContext.settingsError}</p> : null}
      </div>
      {turnActive ? <div className="composer-delivery-choice" role="group" aria-label="Active task delivery">
        <button type="button" aria-pressed={deliveryMode === "steer"} title="Wait for the current model or tool step to finish; does not interrupt Python" onClick={() => setChatDelivery("steer",sessionId)}><Icon name="send"/>Steer current task</button>
        <button type="button" aria-pressed={deliveryMode === "follow_up"} onClick={() => setChatDelivery("follow_up",sessionId)}><Icon name="queue"/>Queue next message</button>
      </div> : null}
      <div className="composer__body">
        <ComposerCommandMenu commands={commandMatches} activeIndex={commandIndex} onHighlight={setCommandIndex} onChoose={chooseCommand} />
        <textarea
          id="composer-input"
          ref={textareaRef}
          rows={1}
          maxLength={8000}
          placeholder={
            turnActive
              ? deliveryMode === "steer" ? "Guide the current task at its next safe step…" : "What should happen after this task?"
            : (attachments.length
              ? "Add a message (optional)…"
              : "Ask VARIANT-1 anything…")
          }
          aria-label="Message VARIANT-1"
          aria-describedby={composerStatus ? "composer-status" : undefined}
          aria-controls={commandMatches.length ? "composer-command-menu" : undefined}
          aria-expanded={Boolean(commandMatches.length)}
          aria-activedescendant={commandMatches.length ? `composer-command-${commandIndex}` : undefined}
          readOnly={navigating}
          value={draft}
          onChange={event => {setDismissedCommand("");setChatDraft(event.target.value,sessionId);}}
          onCompositionStart={()=>{composing.current=true;}}
          onCompositionEnd={()=>{composing.current=false;}}
          onPaste={event => {
            const clipboard = event.clipboardData;
            let files = Array.from(clipboard.files || []);
            if (!files.length) {
              files = Array.from(clipboard.items || []).flatMap(item => {
                if (item.kind !== "file") return [];
                const file = item.getAsFile();
                return file ? [file] : [];
              });
            }
            if (!files.length) return;
            event.preventDefault();
            if(navigating || !sessionId){notifyChat("Wait for this chat to finish loading.");return;}
            void addChatFiles(files,sessionId);
          }}
          onKeyDown={event => {
            if(composing.current || event.nativeEvent.isComposing || event.keyCode===229) return;
            if(commandMatches.length && ["ArrowDown","ArrowUp"].includes(event.key)){
              event.preventDefault();setCommandIndex(index=>(index+(event.key==="ArrowDown" ? 1 : -1)+commandMatches.length)%commandMatches.length);return;
            }
            if(commandMatches.length && event.key==="Escape"){
              event.preventDefault();event.stopPropagation();setDismissedCommand(draft);return;
            }
            if ((event.key === "Tab" && !event.shiftKey || event.key==="Enter" && !event.shiftKey) && commandMatches.length) {
              event.preventDefault();
              chooseCommand(commandMatches[commandIndex] || commandMatches[0]);
              return;
            }
            if (event.key === "Enter" && !event.shiftKey) {
              event.preventDefault();
              if (turnActive && !draft.trim()) return;
              if (!turnActive && !canSend) return;
              if (turnActive && !canActiveInput) return;
              send(turnActive ? deliveryMode : "steer");
            }
          }}
        />
        {draft.length>=7200 ? <div className="composer-length" title="Message character limit">{draft.length.toLocaleString()} / 8,000</div> : null}
      </div>
      <div className="composer__toolbar">
          <div className="composer__primary-tools" role="group" aria-label="Input tools">
            <button
              type="button"
              className="composer-icon-button"
              id="attach-button"
              aria-label="Attach file"
              title="Attach an image or file. You can drag folders onto the composer."
              disabled={turnActive || navigating || !sessionId}
              onClick={attachFiles}
            >
              <Icon name="attach"/>
            </button>
            <button
              type="button"
              className={`voice-button${micPhase === "recording" ? " recording" : ""}${micPhase === "encoding" || micPhase === "transcribing" || micPhase === "requesting" ? " processing" : ""}`}
              id="composer-voice"
              aria-label={micPhase==="recording" ? "Finish recording" : micPhase==="transcribing" ? "Cancel transcription" : micPhase==="requesting" ? "Cancel microphone request" : "Use voice"}
              aria-pressed={micPhase === "recording"}
              title={micPhase === "recording"
                ? "Listening — click to stop"
                : micPhase === "transcribing"
                  ? "Transcribing — click to cancel"
                  : micPhase === "encoding"
                    ? "Encoding audio…"
                  : micPhase === "requesting"
                    ? "Requesting microphone — click to cancel"
                    : "Use voice"}
              disabled={
                !connected || navigating || !sessionId
                || !["idle", "error", "recording", "requesting", "transcribing"].includes(micPhase)
              }
              onClick={toggleMic}
            >
              <MicGlyph phase={micPhase}/>
            </button>
            <PeerControl chatId={sessionId}/>
            <input
              ref={fileInputRef}
              type="file"
              hidden
              multiple
              onChange={event => {
                if (event.currentTarget.files?.length) {
                  void addChatFiles(event.currentTarget.files,filePickerOwner.current);
                }
                event.currentTarget.value = "";
              }}
            />
          </div>
          <div className="composer__session-controls">
            <ModelPickerControl
              sessionId={sessionId}
              open={modelMenuOpen}
              disabled={turnActive || !connected || !sessionId || navigating || !!pendingSetting}
              pendingLabel={pendingSetting ? `${pendingSetting.checking ? "Checking" : "Applying"} ${pendingSetting.label}…` : undefined}
              buttonRef={modelButtonRef}
              onToggle={() => {
                setContextMenuOpen(false);
                setModelMenuOpen(open => !open);
              }}
              onClose={() => setModelMenuOpen(false)}
              onChoose={chooseModel}
              onChooseReasoning={chooseReasoningEffort}
            />
            <ComposerCapabilities connected={connected} turnActive={turnActive} blockedReason={navigating ? "Opening conversation…" : pendingSetting ? "Wait for the model change to finish" : undefined}/>
          </div>
        <div className="composer__send-group" role="group" aria-label="Message delivery">
          <ContextMeter
            context={composerContext}
            sessionId={sessionId}
            open={contextMenuOpen}
            buttonRef={contextButtonRef}
            onClose={()=>setContextMenuOpen(false)}
            onToggle={() => {
              setModelMenuOpen(false);
              setContextMenuOpen(open => !open);
            }}
          />
          {turnActive ? (
            <>
              <button className="composer-delivery-send" type="button" disabled={!canActiveInput}
                aria-label={deliveryMode === "steer" ? "Send steering message" : "Queue next message"}
                title={deliveryMode === "steer" ? "Wait for the current model or tool step to finish; does not interrupt Python" : "Queue after the current task"}
                onClick={() => send(deliveryMode)}><Icon name={deliveryMode === "steer" ? "send" : "queue"}/>{deliveryMode === "steer" ? "Steer" : "Queue"}</button>
              {paused ? <button
                className="composer-delivery-send composer-stop"
                type="button"
                aria-label="Stop response"
                aria-busy={stopPending}
                disabled={!connected || navigating || stopPending}
                onClick={() => cancelChatTurn(sessionId, activeOwner.admissionId)}
                title={stopPending ? "Waiting for the task to stop" : "Stop the current task"}
              >
                {stopPending ? <i className="composer-spinner" aria-hidden="true"/> : <Icon name="stop"/>}<span>Stop</span>
              </button> : null}
              <button
                className="send-button composer-pause"
                type="button"
                aria-label={paused ? "Resume task" : "Pause task"}
                aria-busy={pauseBusy}
                disabled={!connected || navigating || stopPending || pauseBusy || pauseUnknown || !pauseIdentity || pauseControl?.state==="idle"}
                onClick={() => requestChatPause(paused ? "resume" : "pause",sessionId,activeOwner.admissionId)}
                title={pauseUnknown ? "Waiting for the current task state" : pauseBusy ? pauseStatus : paused ? "Resume the same task and its queued messages" : "Pause after the current model or tool step; keeps this task and queued messages"}
              >
                {pauseBusy ? <i className="composer-spinner" aria-hidden="true"/> : <Icon name={paused ? "play" : "pause"}/>}<span>{paused ? "Resume" : "Pause"}</span>
              </button>
            </>
          ) : (
            <button
              className="send-button"
              id="send-button"
              type="button"
              aria-label={goalCommand ? "Start goal" : "Send message"}
              disabled={!canSend}
              onClick={() => send()}
            >
              <Icon name="send"/><span>{goalCommand ? "Start goal" : "Send"}</span>
            </button>
          )}
        </div>
      </div>
    </div>
  </section>;
}
