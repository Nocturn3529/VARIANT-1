import {createExternalStore} from "./createModuleStore";

type ToastState = Readonly<{
  message: string;
  visible: boolean;
  surface: string;
}>;

const store = createExternalStore<ToastState>({message: "", visible: false, surface: "main"});
export const getToastState = store.getState;
let timer: ReturnType<typeof setTimeout> | null = null;

export function notifyToast(message: string, surface = "main"): void {
  if (timer) clearTimeout(timer);
  store.replaceState({message: String(message || ""), visible: true, surface});
  timer = setTimeout(() => {
    timer = null;
    store.setState({visible: false});
  }, 2200);
}

export function useToastState(): ToastState {
  return store.useStore();
}
