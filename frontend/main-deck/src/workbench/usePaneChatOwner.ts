import {useChatProjects} from "../state/chatProjectStore";
import {useSessionState} from "../state/sessionStore";

export function usePaneChatOwner(id: string) {
  const session = useSessionState().items.find(item => item.id === id);
  const {projects} = useChatProjects();
  const project = projects[id] !== undefined ? projects[id] : session?.project;
  const title = session?.title || id;
  return {title, label: project ? `${project.name} / ${title}` : title, root: project?.root};
}
