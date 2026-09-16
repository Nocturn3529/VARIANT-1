/** One renderer-wide speech output owner.
 *
 * Chat auto-speak, reply Play, and Settings previews must never create
 * overlapping HTMLAudioElements. Replacing an owner stops the prior element
 * and notifies its state owner so UI cannot remain stuck on "playing".
 */

export type SpeechPlaybackRequest = {
  owner: "chat" | "settings";
  src: string;
  onEnded?: () => void;
  onError?: () => void;
  onStopped?: () => void;
};

let active: {
  owner: SpeechPlaybackRequest["owner"];
  audio: HTMLAudioElement;
  onStopped?: () => void;
} | null = null;

function release(audio: HTMLAudioElement): void {
  try {
    audio.onended = null;
    audio.onerror = null;
    audio.pause();
    audio.removeAttribute("src");
    audio.load();
  } catch {
    /* Playback teardown is best-effort at renderer shutdown. */
  }
}

export function stopSpeechPlayback(
  owner?: SpeechPlaybackRequest["owner"],
): boolean {
  const current = active;
  if (!current || (owner && current.owner !== owner)) return false;
  active = null;
  release(current.audio);
  current.onStopped?.();
  return true;
}

export function playSpeechPlayback(request: SpeechPlaybackRequest): void {
  stopSpeechPlayback();
  const audio = new Audio(request.src);
  active = {owner: request.owner, audio, onStopped: request.onStopped};
  const finish = (kind: "ended" | "error") => {
    if (active?.audio !== audio) return;
    active = null;
    release(audio);
    if (kind === "ended") request.onEnded?.();
    else request.onError?.();
  };
  audio.onended = () => finish("ended");
  audio.onerror = () => finish("error");
  audio.play().catch(() => finish("error"));
}

