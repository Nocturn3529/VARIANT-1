import {useChatState} from "../chatStore";
import {AgentTeamView} from "./AgentTeamView";
import {refreshAgentTeam,selectTeamAgent,closeTeamAgent} from "./agentTeam";

export function AgentTeamPanel() {
  const state=useChatState(),team=state.agentTeam;
  return <AgentTeamView agents={team.agents} total={team.total} activeCount={team.active} blockedCount={team.blocked} truncated={team.truncated} connected={state.connected} synced={team.synced}
    error={team.error || ""} selectedId={team.selectedId} detail={team.detail} detailError={team.detailError || ""} loadingDetail={!!team.detailReadId}
    onRefresh={()=>refreshAgentTeam(true)} onSelect={selectTeamAgent} onClose={closeTeamAgent}/>;
}
