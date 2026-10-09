import type {ChatMessage,ChatTurnStep} from "./types";

export type IndexedMessage = {message:ChatMessage;index:number};
/** Keep each real input where it reached the run, without changing history. */
export function liveActivitySegments(users:readonly IndexedMessage[],steps:readonly ChatTurnStep[]) {
  const boundaries=users.filter(row=>row.message.inputBoundary)
    .sort((a,b)=>(a.message.ts || 0)-(b.message.ts || 0) || a.index-b.index);
  const prompts=users.filter(row=>!row.message.inputBoundary);
  const segments:Array<{key:string;input?:IndexedMessage;steps:ChatTurnStep[]}>=[];
  let remaining=[...steps],previous:IndexedMessage|undefined;
  for(const input of boundaries) {
    const cutoff=(input.message.ts || 0)*1000;
    const before=remaining.filter(step=>(step.ts<1e12?step.ts*1000:step.ts)<=cutoff);
    remaining=remaining.filter(step=>(step.ts<1e12?step.ts*1000:step.ts)>cutoff);
    segments.push({key:previous?.message.ticketId || previous?.message.localId || "root",input:previous,steps:before});
    previous=input;
  }
  segments.push({key:previous?.message.ticketId || previous?.message.localId || "root",input:previous,steps:remaining});
  return {prompts,segments};
}
