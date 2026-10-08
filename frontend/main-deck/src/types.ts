export type ProviderInfo = {
  connection?: {state: "unchecked" | "saved" | "refresh_needed" | "ready" | "service_ready" | "unavailable"; checked_at: number; detail: string};
  name: string;
  display_name: string;
  description: string;
  api_style: string;
  auth_style: string;
  base_url: string;
  default_model: string;
  model: string;
  configured: boolean;
  api_key_configured?: boolean;
  credential_count: number;
  supports_vision: boolean;
  supports_reasoning: boolean;
  reasoning_efforts?: string[];
  signup_url?: string;
  models_url?: string;
  credential_env_vars?: string[];
  auth_methods?: Array<"oauth" | "api_key" | "external" | "custom">;
  base_url_editable?: boolean;
  custom?: boolean;
  origin?: string;
};

export type CredentialInfo = {
  id: string;
  label: string;
  priority: number;
  enabled: boolean;
  base_url: string;
  source: string;
  status: string;
  failures: number;
  last_error: string;
};

export type OAuthStatus = {
  provider?: string;
  connected?: boolean;
  stored?: boolean;
  auth_flow?: string;
  managed_external?: boolean;
  source?: string;
  expires_at?: number;
  expires_in?: number;
  account_id_present?: boolean;
  refresh_managed_by?: string;
  error?: string;
};

export type OAuthFlowState = {
  requestId: string;
  provider: string;
  phase: "idle" | "starting" | "pending" | "complete" | "error";
  verificationUrl: string;
  userCode: string;
  error: string;
};

export type GatewayAdapter = {
  id: string;
  name: string;
  display_name: string;
  description: string;
  docs_url?: string;
  builtin?: boolean;
  running: boolean;
  connected: boolean;
  last_error: string;
  runtime_available: boolean;
  configured: boolean;
  configured_fields: string[];
  values: Record<string, unknown>;
  fields: Array<{
    key: string;
    label: string;
    required: boolean;
    secret: boolean;
    advanced: boolean;
    placeholder?: string;
    config_key?: string;
    value_type?: "string" | "list" | "boolean" | "integer";
  }>;
  credential_required: boolean;
  credential_configured: boolean;
  config: {
    enabled?: boolean;
    allowed_users?: string[];
    allowed_conversations?: string[];
    prefix?: string;
    mention_only?: boolean;
    allow_all?: boolean;
  };
};

export type MessagingPairingUser = {
  request_id?: string;
  platform: string;
  user_id: string;
  user_name?: string;
  conversation_id?: string;
};

export type GatewayState = {
  enabled: boolean;
  adapters: GatewayAdapter[];
  stats: Record<string, number>;
  session_count: number;
  plugin_errors: Array<{plugin: string; error: string}>;
  credential_error?: string;
  lifecycle_error?: string;
  pairing: {pending: MessagingPairingUser[]; approved: MessagingPairingUser[]};
};

export type CloudUsageSnapshot = {
  period?: string;
  today?: string;
  providers?: Record<string, {
    calls?: number;
    total_tokens?: number;
    cost_usd?: number;
    cost_status?: string;
    cached_prompt_tokens?: number;
    uncached_prompt_tokens?: number;
    reasoning_tokens?: number;
    cache_write_prompt_tokens?: number;
    cache_share?: number;
    cache_hit_calls?: number;
  }>;
  total?: {
    calls?: number;
    total_tokens?: number;
    cost_usd?: number;
    cost_status?: string;
    cached_prompt_tokens?: number;
    uncached_prompt_tokens?: number;
    reasoning_tokens?: number;
    cache_write_prompt_tokens?: number;
    cache_share?: number;
  };
};

export type ConfigState = {
  model_ready?: boolean;
  engine_ready?: boolean;
  startup_error?: string;
  mode?: string;
  local_prewarm?: boolean;
  local_engine_wanted?: boolean;
  provider?: string;
  cloud_model?: string;
  oauth?: {
    openai_codex?: boolean;
    openai_codex_detail?: OAuthStatus;
    xai?: boolean;
    xai_detail?: OAuthStatus;
  };
  oauth_by_provider?: Record<string, OAuthStatus>;
  credential_strategy_by_provider?: Record<string, string>;
  credential_revision_by_provider?: Record<string, number>;
  providers?: ProviderInfo[];
  credentials_by_provider?: Record<string, CredentialInfo[]>;
  custom_endpoints?: CustomEndpoint[];
  messaging_gateway?: GatewayState;
  cloud_usage?: CloudUsageSnapshot;
  inference_platform?: Record<string, unknown>;
};

export type ToolsState = Record<string, unknown>;

export type PlatformState = {
  connected: boolean;
  config: ConfigState;
  tools: ToolsState;
  gateway: GatewayState | null;
  oauthFlow: OAuthFlowState;
  credentialReceipt: {
    requestId: string;
    provider: string;
    operation: "set" | "clear" | "add" | "remove" | "enable" | "priority" | "strategy";
    accepted: boolean;
    error: string;
  } | null;
  customEndpointReceipt: {
    requestId: string;
    operation: "validate" | "save" | "activate" | "remove";
    accepted: boolean;
    validation: CustomEndpointValidation | null;
    endpoint: CustomEndpoint | null;
    id: string;
    removed: boolean;
    fallbackMode: string;
    error: string;
  } | null;
};

export type WebSearchProvider =
  | "variant1" | "ddgs" | "brave-free" | "exa" | "firecrawl"
  | "keenable" | "parallel" | "searxng" | "tavily" | "xai";

export type WebSearchProviderInfo = {
  id: WebSearchProvider;
  name: string;
  description: string;
  auth: "none" | "api_key" | "optional" | "endpoint" | "shared";
  signup_url?: string;
  env_vars?: string[];
  keyless?: boolean;
  configured: boolean;
  available: boolean;
  active: boolean;
  config?: Record<string, unknown>;
};

export type AgentToolsState = {
  connected: boolean;
  webSearchProvider: WebSearchProvider;
  webSearchProviders: WebSearchProviderInfo[];
  variant1Engines: string[];
  variant1SearchChecked: boolean;
  variant1HealthyEngines: number;
  variant1EngineCount: number;
  variant1SearchDegraded: boolean;
  variant1SearchLatencyMs: number;
  variant1SearchCacheHit: boolean;
  variant1SearchError: string;
  variant1EngineHealth: Record<string, string>;
  searxngBaseUrl: string;
  searxngAutostart: boolean;
  searxngManaged: boolean;
  searxngReady: boolean;
  searxngOwned: boolean;
  searxngRunning: boolean;
  searxngDockerAvailable: boolean;
  searxngError: string;
  searxngRuntime: string;
  toolsReceipt: {
    requestId: string;
    status: "accepted" | "rejected";
    error: string;
  } | null;
};

export type LocalModelInfo = {
  name: string;
  path: string;
  vision?: boolean;
  mmproj?: string;
  sizeBytes?: number;
};

export type RuntimeTarget = {
  id: string;
  kind: string;
  runtime_id: string;
  label: string;
  detail?: string;
  available: boolean;
  installed: boolean;
  manageable: boolean;
  recommended?: boolean;
  reason?: string;
};

export type InstallJob = {
  id: string;
  runtime_id: string;
  operation: string;
  target_id: string;
  target_label?: string;
  status: string;
  progress?: number;
  step?: string;
  logs?: string[];
  error?: string;
  backend?: string;
  tag?: string;
  done_bytes?: number;
  total_bytes?: number;
};

export type InferencePlatform = {
  targets: RuntimeTarget[];
  install_jobs: InstallJob[];
  local_runtime?: LocalRuntimeStatus;
};

export type LocalRuntimeBuild = {
  tag?: string;
  backend?: string;
  verified_version?: string;
  binary?: string;
};

export type LocalRuntimeStatus = {
  supported: boolean;
  tag: string;
  recommended_backend: string;
  installed: boolean;
  managed_installed: boolean;
  bundled_active: boolean;
  install_source: "managed" | "bundled" | "custom" | "none";
  custom_active?: boolean;
  configured_binary?: string;
  running_binary?: string;
  pending_restart?: boolean;
  backend: string;
  version: string;
  binary: string;
  active_binary: string;
  managed_active: boolean;
  update_available: boolean;
  installed_builds: LocalRuntimeBuild[];
  runtime_root: string;
};

export type CustomEndpoint = {
  id: string;
  name: string;
  base_url: string;
  model: string;
  context_length: number;
  discover_models: boolean;
  models: string[];
  is_current: boolean;
  has_api_key: boolean;
  credential_count: number;
};

export type CustomEndpointValidation = {
  ok: boolean;
  reachable: boolean;
  base_url?: string;
  models: string[];
  latency_ms?: number;
  status_code?: number;
  message: string;
};

export type VoiceOption = {
  id: string;
  name: string;
  language: string;
};

export type SpeechProviderInfo = {
  id: string;
  name: string;
  kind: "local" | "cloud";
  auth: "none" | "api_key" | "shared";
  description: string;
  signup_url?: string;
  env_vars?: string[];
  default_model?: string;
  default_voice?: string;
  mime_type?: string;
  configured: boolean;
  available: boolean;
  config?: Record<string, unknown>;
  voices?: string[];
};

export type CapabilitiesInfo = {
  tools?: boolean;
  thinking?: boolean;
  vision?: boolean;
  ctx_size?: number;
  source?: string;
};

export type DoctorFinding = {
  level?: string;
  title?: string;
  detail?: string;
  fix?: string;
};

export type AboutState = {
  connected: boolean;
  version: string;
  packaged: boolean;
  updateLabel: string;
  updateChecking: boolean;
  updateCheckComplete: boolean;
  update: UpdateState | null;
  paths: Record<string, string>;
  health: {
    backend: string;
    backendDetail: string;
    model: string;
    modelDetail: string;
    sessionContext: string;
    sessionContextDetail: string;
    scheduler: string;
    schedulerDetail: string;
  };
  findings: DoctorFinding[] | null;
};

export type GeneralState = {
  connected: boolean;
  launchAtLogin: boolean;
  startHidden: boolean;
  mode: string;
  model: string;
  inferenceRuntime: string;
  capabilities: CapabilitiesInfo | null;
  installedModels: LocalModelInfo[];
  modelsFolder: string;
  modelsScanning: boolean;
  modelsSwitching: boolean;
  sttProvider: string;
  ttsProvider: string;
  sttProviders: SpeechProviderInfo[];
  ttsProviders: SpeechProviderInfo[];
  sttLocalAvailable: boolean;
  sttDropPath: string;
  ttsLocalAvailable: boolean;
  ttsDropPath: string;
  voiceEnabled: boolean;
  voiceAvailable: boolean;
  voiceSpeed: number;
  voices: VoiceOption[];
  currentVoice: string;
  voicesLoaded: boolean;
  ttsConfigKey?: string;
  voicesError: string;
  speechReceipt: {requestId: string; accepted: boolean; error: string} | null;
};

export type GoalLoopSummary = {
  id: string;
  title: string;
  status: string;
  created?: number | string | null;
  updated?: number | string | null;
  goal?: string;
  done_count?: number;
  blocked_count?: number;
  next_count?: number;
  session_id?: string;
};

export type GoalLoopDetail = {
  id: string;
  meta?: {
    id?: string;
    title?: string;
    status?: string;
    created?: number;
    updated?: number;
    session_id?: string;
    last_compact_at?: number;
  };
  charter?: {
    goal?: string;
    constraints?: string[];
    success_criteria?: string[];
  };
  progress?: {
    done?: string[];
    blocked?: string[];
    next?: string[];
    notes?: string;
  };
  anchors?: {
    domains?: Record<string, Record<string, unknown>>;
  };
};

export type GoalsState = {
  connected: boolean;
  loops: GoalLoopSummary[];
  loopActiveCount: number;
  selectedLoopId: string;
  selectedLoop: GoalLoopDetail | null;
  draftLoopTitle: string;
  draftLoopGoal: string;
};

export type RuntimeApi = {
  supportsNativeWindows?: boolean;
  controlNativeWindow?: (id: string, action: "focus" | "minimize" | "maximize" | "pin" | "close" | "ready" | "bounds" | "resize", size?: {width: number; height: number}) => Promise<{ok?: boolean; pinned?: boolean; bounds?: {x: number; y: number; width: number; height: number}}>;
  onNativeWindowClosed?: (listener: (id: string) => void) => (() => void);
  openMonitor?: () => Promise<{ok?: boolean; reason?: string}>;
  getBackendInfo?: () => Promise<{
    port?: number;
    token?: string;
  } | null | undefined>;
  onBackendStatus?: (
    listener: (status: {status?: string}) => void,
  ) => unknown;
  log?: (message: string) => void;
  readWorkbenchDirectory?: (path: string) => Promise<{
    ok?: boolean;
    error?: string;
    path?: string;
    entries?: Array<{name: string; path: string; directory: boolean; symlink?: boolean; size?: number; mtimeMs?: number}>;
  }>;
  readWorkbenchFile?: (path: string) => Promise<{
    ok?: boolean;
    error?: string;
    path?: string;
    text?: string;
    dataUrl?: string;
    binary?: boolean;
    editable?: boolean;
    truncated?: boolean;
    size?: number;
    mtimeMs?: number;
    mediaType?: string;
  }>;
  writeWorkbenchFile?: (path: string, text: string, expectedMtimeMs?: number) => Promise<{
    ok?: boolean;
    conflict?: boolean;
    error?: string;
    mtimeMs?: number;
  }>;
  renameWorkbenchPath?: (path: string, name: string) => Promise<{ok?: boolean; error?: string; path?: string}>;
  trashWorkbenchPath?: (path: string) => Promise<{ok?: boolean; error?: string}>;
  revealWorkbenchPath?: (path: string) => Promise<{ok?: boolean; error?: string}>;
  watchWorkbenchPath?: (path: string, options?: {scope?: "workspace" | "directory"}) => Promise<{ok?: boolean; id?: string; error?: string}>;
  stopWorkbenchWatch?: (id: string) => Promise<{ok?: boolean}>;
  onWorkbenchPathChanged?: (listener: (event: {id?: string; path?: string; filename?: string; event?: string; error?: string}) => void) => (() => void) | void;
  listChatWindows?:()=>Promise<unknown>;
  onChatWindowsChanged?:(callback:(value:unknown)=>void)=>()=>void;
  manageChatWindow?:(id:string,action:"focus"|"close")=>Promise<{ok:boolean}>;
  openChatWindow?: (id:string,title:string)=>Promise<{ok:boolean;error?:string}>;
  getWorkbenchRoot?: () => Promise<{ok?: boolean; path?: string; error?: string}>;
  getWorkbenchGitStatus?: (path: string) => Promise<WorkbenchGitStatus>;
  getWorkbenchGitBranches?: (path:string, options?:{limit?:number}) => Promise<{ok?:boolean;error?:string;branches?:WorkbenchGitBranch[];truncated?:boolean;defaultBaseRef?:string;defaultBaseSource?:string;baseError?:string}>;
  getWorkbenchGitHistory?: (path:string, options:{ref?:string;baseRef?:string;allBranches?:boolean;historyOids?:string[];excludeOid?:string;limit?:number;offset?:number}) => Promise<{ok?:boolean;error?:string;resolvedOid?:string;historyOids?:string[];excludeOid?:string;commits?:WorkbenchGitCommit[];nextOffset?:number|null;truncated?:boolean}>;
  getWorkbenchReviewFiles?: (path:string, options:WorkbenchReviewOptions) => Promise<WorkbenchGitStatus & {truncated?:boolean;baseOid?:string;headOid?:string;comparison?:{baseRef:string;baseSource:string;baseOid:string;headOid:string};countBasis?:string;aggregate?:WorkbenchReviewAggregate}>;
  getWorkbenchReviewDiff?: (path:string,file:string,options:WorkbenchReviewOptions & {context?:number}) => Promise<WorkbenchReviewDiff>;
  runWorkbenchGit?: (action: string, path: string, options?: Record<string, unknown>) => Promise<Record<string, unknown>>;
  captureWorkbenchPreview?: (webContentsId: number) => Promise<{ok?: boolean; image?: string; image_width?: number; image_height?: number; error?: string}>;
    bindWorkbenchBrowser?: (tabId: string, guestId: number, operationId?: string) => Promise<{ok?: boolean}>;
    workbenchBrowser?: (command:Record<string,unknown>)=>Promise<{ok?:boolean;state?:Record<string,unknown>;value?:unknown;error?:string}>;
    onWorkbenchBrowserEvent?: (callback:(event:{tabId:string;guestId?:number;event:string;details?:Record<string,unknown>;state?:Record<string,unknown>})=>void)=>()=>void;
  workbenchDownloads?: (command: Record<string, unknown>) => Promise<{ok?: boolean; error?: string; downloads?: unknown[]; [key: string]: unknown}>;
  onWorkbenchDownloads?: (callback: (rows: unknown) => void) => () => void;
  onNavigate?: (listener: (view: string) => void) => unknown;
  onVoiceToggle?: (listener: () => void) => (() => void) | void;
  minimize?: () => void;
  toggleMaximize?: () => void;
  close?: () => void;
  pickFolder?: () => Promise<string | null | undefined>;
  chooseContextExportPath?: (format:"jsonl"|"markdown") => Promise<{ok?:boolean;cancelled:boolean;path?:string;overwrite?:boolean;error?:string}>;
  getPathForFile?: (file: File) => string;
  openAppPath?: (key: string) => Promise<{ok?: boolean; reason?: string} | null | undefined>;
  openLocalPath?: (path: string) => Promise<{ok?: boolean; reason?: string} | null | undefined>;
  openExternal?: (url: string) => Promise<{ok?: boolean; reason?: string} | null | undefined> | void;
  getAppInfo?: () => Promise<{
    version?: string;
    packaged?: boolean;
    paths?: Record<string, string>;
    update?: UpdateState;
  } | null | undefined>;
  getLaunchAtLogin?: () => Promise<boolean>;
  setLaunchAtLogin?: (on: boolean) => Promise<boolean | {
    ok: boolean;
    value?: boolean;
    reason?: string;
  }>;
  getSettings?: () => Promise<{
    general?: {startHidden?: boolean};
  } | null | undefined>;
  setStartHidden?: (on: boolean) => Promise<boolean | {
    ok: boolean;
    value?: boolean;
    reason?: string;
  }>;
  checkForUpdates?: () => Promise<{
    ok?: boolean;
    available?: boolean;
    version?: string;
    reason?: string;
    state?: UpdateState;
  } | null | undefined>;
  getUpdateState?: () => Promise<UpdateState | null | undefined>;
  downloadUpdate?: () => Promise<{ok?: boolean; reason?: string} | null | undefined>;
  cancelUpdateDownload?: () => Promise<{ok?: boolean} | null | undefined>;
  installUpdate?: () => Promise<{ok?: boolean; reason?: string} | null | undefined>;
  openUpdateRelease?: () => Promise<{ok?: boolean} | null | undefined>;
  onUpdateState?: (callback: (state: UpdateState) => void) => () => void;
};

/** The main process's update state; downloads and installs happen only on request. */
export type UpdateState = {
  status: "unavailable" | "idle" | "checking" | "up-to-date" | "available" | "downloading" | "downloaded" | "installing" | "error";
  reason: string;
  currentVersion: string;
  platform: string;
  version: string;
  releaseName: string;
  releaseUrl: string;
  installMode: "in-app" | "release-page";
  percent: number;
  transferred: number;
  total: number;
  error: string;
  checkedAt: number;
};

export type WorkbenchGitFile = {
  path: string;
  originalPath?: string;
  status: string;
  staged?: boolean;
  added?: number|null;
  removed?: number|null;
  binary?:boolean;untracked?:boolean;
};

export type WorkbenchReviewOptions = {scope:"uncommitted"|"staged"|"unstaged"|"commit"|"branch";commits?:string[];ref?:string;baseRef?:string};
export type WorkbenchReviewAggregate={added:number;removed:number;fileCount:number;binaryFiles:number;complete:boolean};
export type WorkbenchReviewDiff = {ok?:boolean;error?:string;diff?:string;fullContents?:boolean;truncated?:boolean;binary?:boolean;context?:number;omittedSections?:number;sections?:Array<{oid:string;baseOid?:string;diff:string;fullContents?:boolean;truncated?:boolean;binary?:boolean}>};
export type WorkbenchGitBranch = {ref:string;name:string;oid:string;current?:boolean;remote?:boolean};
export type WorkbenchGitCommit = {oid:string;parents:string[];subject:string;authorName:string;committedAt:number;added?:number|null;removed?:number|null;fileCount?:number|null;binaryFiles?:number|null;statsComplete?:boolean};

export type WorkbenchGitStatus = {
  ok?: boolean;
  error?: string;
  root?: string;
  branch?: string;
  ahead?: number;
  behind?: number;
  files?: WorkbenchGitFile[];
};

export type RuntimeContext = {
  send(payload: WsCommand): boolean;
  notify(message: string, surface?: string): void;
  /** True when the Main Deck WebSocket is open to the local backend. */
  isOpen?: () => boolean;
  /** Active primary view id (chat, memory, settings, …). */
  view?: string;
  /** Active Settings category when view === "settings". */
  settingsCategory?: string;
  api?: RuntimeApi | null;
  relativeTime?: (ts: number | string | undefined | null) => string;
  formatTime?: (ts: number | string | undefined | null) => string;
  navigate?: (view: string) => void;
};
import type { WsCommand } from "./protocol";
