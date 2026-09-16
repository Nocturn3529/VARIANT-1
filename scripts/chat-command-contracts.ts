import {sendChat} from "../frontend/main-deck/src/chat/stateCore";

// These examples are compiled, never executed. Each rejected call must remain
// a compiler error at the real sender boundary, not just at a type alias.
function checkChatContract() {
  sendChat({type:"children:snapshot:get",session_id:"chat",request_id:"agents",limit:100});
  sendChat({type:"children:detail:get",session_id:"chat",request_id:"child",child_id:"owned-child",limit:100});
  sendChat({type:"goal:finish",session_id:"chat",goal_id:"goal",request_id:"end",expected_version:4});
  sendChat({type:"goal:continue",session_id:"chat",goal_id:"goal",request_id:"continue",expected_version:4});
  sendChat({type:"goal:archive",session_id:"chat",goal_id:"goal",request_id:"dismiss",expected_version:4});
  // @ts-expect-error child detail must identify the requested child
  sendChat({type:"children:detail:get",session_id:"chat",request_id:"child"});
  // @ts-expect-error child roster must be session scoped
  sendChat({type:"children:snapshot:get",request_id:"agents"});
  sendChat({type:"goal:submit",session_id:"chat",request_id:"goal-request",objective:"Build and verify"});
  sendChat({type:"goal:current:get",session_id:"chat",request_id:"read",submission_request_id:"goal-request"});
  sendChat({type:"goal:pause",session_id:"chat",goal_id:"goal",request_id:"pause",expected_version:3});
  // @ts-expect-error durable goal admission requires an objective
  sendChat({type:"goal:submit",session_id:"chat",request_id:"goal-request"});
  // @ts-expect-error goal control requires an optimistic version fence
  sendChat({type:"goal:cancel",session_id:"chat",goal_id:"goal",request_id:"cancel"});
  // @ts-expect-error current goal reads belong to an explicit session
  sendChat({type:"goal:current:get",request_id:"read"});
  sendChat({type: "chat", text: "hello", client_id: "client", session_id: "chat"});
  sendChat({type: "cancel", session_id: "chat", admission_id: "admission"});
  sendChat({type:"chat:pause",session_id:"chat",admission_id:"admission",run_id:"run",request_id:"pause-request"});
  sendChat({type:"chat:resume",session_id:"chat",request_id:"resume-request"});
  sendChat({type:"chat:queue:get",session_id:"chat",request_id:"read"});
  sendChat({type:"chat:queue:continue",session_id:"chat",ticket_id:"ticket",expected_revision:4,request_id:"continue"});
  sendChat({type:"chat:queue:remove",session_id:"chat",ticket_id:"ticket",expected_revision:4,request_id:"remove"});
  // @ts-expect-error queue mutations require a revision fence
  sendChat({type:"chat:queue:continue",session_id:"chat",ticket_id:"ticket",request_id:"continue"});
  // @ts-expect-error deletion must identify an individual ticket
  sendChat({type:"chat:queue:remove",session_id:"chat",expected_revision:4,request_id:"remove"});
  // @ts-expect-error Pause needs request correlation
  sendChat({type:"chat:pause",session_id:"chat"});
  // @ts-expect-error Resume must scope its session
  sendChat({type:"chat:resume",request_id:"request"});
  // @ts-expect-error admission must identify the displayed chat explicitly
  sendChat({type: "chat", text: "hello", client_id: "client"});
  // @ts-expect-error Stop must identify its owning chat
  sendChat({type: "cancel"});
  sendChat({type: "chat:runtime:mutation:set", id: "chat", enabled: false, request_id: "request", expected_revision: 3});
  // @ts-expect-error text and client_id are required
  sendChat({type: "chat"});
  // @ts-expect-error mutation requires a boolean
  sendChat({type: "chat:runtime:mutation:set", id: "chat", enabled: "yes", request_id: "request", expected_revision: 3});
  // @ts-expect-error runtime actions require the durable chat id
  sendChat({type: "chat:runtime:action", action: "restart_kernel"});
  // @ts-expect-error unrelated commands belong to their own domain transport
  sendChat({type: "arbitrary-command", payload: 42});
}
void checkChatContract;
