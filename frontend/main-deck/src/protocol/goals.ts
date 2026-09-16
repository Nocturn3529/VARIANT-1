/** Durable goal data is backend-owned; a submitted command is not a goal. */
export const GOAL_STATUSES = ["draft", "queued", "running", "waiting_user", "waiting_external", "blocked", "paused", "succeeded", "failed", "cancelled", "archived"] as const;
export type GoalStatus = typeof GOAL_STATUSES[number];
export type ComposerGoal = Readonly<{
  goal_id: string;
  owner_chat_id: string;
  title: string;
  objective: string;
  status: GoalStatus;
  version: number;
  pause_reason: string;
}>;

export function parseComposerGoal(value: unknown): ComposerGoal | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const row = value as Record<string, unknown>;
  if (row.schema !== "variant1.goal.v1" || typeof row.goal_id !== "string" || !row.goal_id
    || typeof row.owner_chat_id !== "string" || !row.owner_chat_id
    || typeof row.objective !== "string" || typeof row.title !== "string"
    || !GOAL_STATUSES.includes(row.status as GoalStatus)
    || !Number.isSafeInteger(row.version) || Number(row.version) < 1) return null;
  return {goal_id:row.goal_id,owner_chat_id:row.owner_chat_id,title:row.title,objective:row.objective,
    status:row.status as GoalStatus,version:Number(row.version),pause_reason:typeof row.pause_reason === "string" ? row.pause_reason : ""};
}

export function goalIsTerminal(goal: ComposerGoal): boolean {
  return ["succeeded", "failed", "cancelled", "archived"].includes(goal.status);
}

export type ComposerGoalState = {
  snapshot: ComposerGoalSnapshot|null;
  synced: boolean;
  error?: string;
  refreshRequestId?: string;
  refreshAgain?: boolean;
  guidance?: {goalId:string;text:string;revision:number};
  pending?: {requestId:string;operation:"submit"|GoalControl;goalId?:string;draft?:string;draftRevision?:number;uncertain?:boolean;guidanceRevision?:number;guidanceText?:string};
};
export type GoalControl="pause"|"resume"|"cancel"|"continue"|"finish"|"archive";
export type ObjectiveOutcome=Readonly<{status:"completed"|"blocked"|"continuing"|"unreported";summary:string;basis:string|null;executionStatus:string}>;
export function parseObjectiveOutcome(value:unknown):ObjectiveOutcome {
  const row=(value && typeof value==="object"?value:{}) as Record<string,unknown>;
  return {status:["completed","blocked","continuing","unreported"].includes(String(row.status))?row.status as ObjectiveOutcome["status"]:"unreported",
    summary:typeof row.summary==="string"?row.summary:"",basis:typeof row.basis==="string"?row.basis:null,
    executionStatus:typeof row.execution_status==="string"?row.execution_status:""};
}

export type ComposerGoalSnapshot = Readonly<{
  goal: ComposerGoal;
  submission_request_id:string;
  completion_basis:string;
  admittedContinuation:Readonly<{requestId:string;message:string;stepId:string;previousAttempt:number;jobId:string}>|null;
  capabilities: Readonly<{pause_scheduling:boolean;pause_active_work:boolean;resume:boolean;cancel:boolean;continue:boolean;retry_cleanup:boolean;finish:boolean;archive:boolean}>;
  objectiveOutcome:ObjectiveOutcome;
  cleanup:Readonly<{status:"not_requested"|"pending"|"complete"|"failed"|"unknown";complete:boolean}>;
  terminationKind:string;
  steps:readonly {id:string;title:string;status:string}[];
  reports:readonly {childId:string;stepId:string;status:string;text:string;truncated:boolean;artifactRef:string;outcome:ObjectiveOutcome}[];
}>;

/** Only the exact backend Work-job receipt can turn stored intent into admission evidence. */
function admittedContinuation(row:Record<string,unknown>):ComposerGoalSnapshot["admittedContinuation"] {
  const record=(value:unknown):Record<string,unknown>=>value && typeof value==="object" && !Array.isArray(value)?value as Record<string,unknown>:{};
  const admission=record(row.continuation_admission),request=record(record(row.state).continuation_request);
  const nonempty=(value:unknown):value is string=>typeof value==="string" && !!value.trim();
  if(admission.status!=="admitted" || admission.basis!=="work_job" || !nonempty(admission.job_id)
    || !nonempty(admission.request_id) || !nonempty(admission.step_id)
    || !Number.isSafeInteger(admission.previous_attempt) || Number(admission.previous_attempt)<0
    || request.request_id!==admission.request_id || request.step_id!==admission.step_id
    || request.previous_attempt!==admission.previous_attempt || typeof request.message!=="string")return null;
  return {requestId:admission.request_id,message:request.message,stepId:admission.step_id,previousAttempt:Number(admission.previous_attempt),jobId:admission.job_id};
}

export function parseComposerGoalSnapshot(value:unknown):ComposerGoalSnapshot|null {
  if(!value || typeof value!=="object" || Array.isArray(value))return null;
  const row=value as Record<string,unknown>,goal=parseComposerGoal(row.goal);
  if(!goal || row.schema!=="variant1.goal-snapshot.v1" || typeof row.submission_request_id!=="string")return null;
  const capabilities=(row.capabilities && typeof row.capabilities==="object" ? row.capabilities : {}) as Record<string,unknown>;
  const cleanup=(row.cleanup && typeof row.cleanup==="object"?row.cleanup:{}) as Record<string,unknown>;
  const termination=(row.termination && typeof row.termination==="object"?row.termination:{}) as Record<string,unknown>;
  return {goal,submission_request_id:row.submission_request_id,completion_basis:typeof row.completion_basis==="string"?row.completion_basis:"",
    admittedContinuation:admittedContinuation(row),
    capabilities:{pause_scheduling:capabilities.pause_scheduling===true,pause_active_work:capabilities.pause_active_work===true,
      resume:capabilities.resume===true,cancel:capabilities.cancel===true,continue:capabilities.continue===true,retry_cleanup:capabilities.retry_cleanup===true,finish:capabilities.finish===true,archive:capabilities.archive===true},
    objectiveOutcome:parseObjectiveOutcome(row.objective_outcome),terminationKind:typeof termination.kind==="string"?termination.kind:"",
    cleanup:{status:["not_requested","pending","complete","failed"].includes(String(cleanup.status))?cleanup.status as ComposerGoalSnapshot["cleanup"]["status"]:"unknown",complete:cleanup.status==="complete" && cleanup.complete===true},
    reports:(Array.isArray(row.reports)?row.reports:[]).slice(0,20).flatMap(value=>{
      if(!value || typeof value!=="object")return [];
      const report=value as Record<string,unknown>;
      if(typeof report.child_id!=="string" || typeof report.text!=="string" || report.completion_basis!=="agent_report")return [];
      return [{childId:report.child_id,stepId:typeof report.step_id==="string"?report.step_id:"",status:typeof report.status==="string"?report.status:"",
        text:report.text.slice(0,16000),truncated:report.truncated===true || report.text.length>16000,artifactRef:typeof report.artifact_ref==="string"?report.artifact_ref:"",outcome:parseObjectiveOutcome(report.outcome)}];
    }),
    steps:(Array.isArray(row.steps)?row.steps:[]).slice(0,50).flatMap(value=>{
      if(!value || typeof value!=="object")return [];
      const step=value as Record<string,unknown>;
      return typeof step.step_id==="string" && typeof step.status==="string" ? [{id:step.step_id,title:typeof step.title==="string"?step.title:step.step_id,status:step.status}] : [];
    })};
}

export type GoalCommand =
  | {type:"goal:submit";session_id:string;request_id:string;objective:string}
  | {type:"goal:current:get";session_id:string;request_id:string;submission_request_id?:string}
  | {type:`goal:${GoalControl}`;session_id:string;goal_id:string;request_id:string;expected_version:number;message?:string};

export type GoalMessage = Readonly<{
  type:"goal:accepted"|"goal:rejected"|"goal:current";
  session_id:string;
  request_id:string;
  operation:string;
  result:unknown;
  error:string;
}>;

export function parseGoalMessage(row:Record<string,unknown>):GoalMessage|null {
  if (!["goal:accepted","goal:rejected","goal:current"].includes(String(row.type))
    || typeof row.session_id!=="string" || !row.session_id || typeof row.request_id!=="string") return null;
  return {type:row.type as GoalMessage["type"],session_id:row.session_id,request_id:row.request_id,
    operation:typeof row.operation==="string"?row.operation:"",result:row.result,
    error:typeof row.error==="string"?row.error:""};
}

export type GoalWorkEvent = Readonly<{type:"work:event";session_id:string;goal_id:string;version:number;aggregateKind:string;aggregateId:string;jobKind?:string}>;
export function parseGoalWorkEvent(row:Record<string,unknown>):GoalWorkEvent|null {
  const event=row.event as Record<string,unknown>|undefined;
  const aggregate=event?.aggregate as Record<string,unknown>|undefined;
  const scope=event?.scope as Record<string,unknown>|undefined;
  const payload=event?.payload as Record<string,unknown>|undefined;
  if(!["goal","job","operation"].includes(String(aggregate?.kind)) || typeof aggregate?.id!=="string" || typeof scope?.chat_id!=="string" || !scope.chat_id)return null;
  return {type:"work:event",session_id:scope.chat_id,goal_id:aggregate.kind==="goal"?aggregate.id:"",version:Number(aggregate.version)||0,aggregateKind:String(aggregate.kind),aggregateId:aggregate.id,
    ...(aggregate.kind==="job" && typeof payload?.kind==="string"?{jobKind:payload.kind}:{})};
}
