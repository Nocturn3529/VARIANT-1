import {BrowserCommandError} from "./browserLifecycle";

// Preserve Electron's existing accelerator vocabulary (v42 keyboard_util.cc).
// Browser/Playwright arrow names are translated only at this native boundary.
const namedKeys = new Map([
  "Alt", "AltGr", "Backspace", "CapsLock", "Cmd", "CmdOrCtrl", "Command", "CommandOrControl",
  "Control", "Ctrl", "Delete", "Down", "End", "Enter", "Esc", "Escape", "Home", "Insert", "Left",
  "MediaNextTrack", "MediaPlayPause", "MediaPreviousTrack", "MediaStop", "Meta",
  ...Array.from({length:24}, (_, i) => `F${i+1}`),
  ...Array.from({length:10}, (_, i) => `num${i}`),
  "numadd", "numdec", "numdiv", "numlock", "nummult", "numsub", "Option", "PageDown", "PageUp",
  "Plus", "PrintScreen", "Return", "Right", "ScrollLock", "Shift", "Space", "Super", "Tab", "Up",
  "VolumeDown", "VolumeMute", "VolumeUp",
].map(key => [key.toLowerCase(), key]));
for (const direction of ["Right", "Left", "Up", "Down"]) namedKeys.set(`arrow${direction.toLowerCase()}`, direction);

const modifiers: Record<string, string> = {
  ctrl:"control", control:"control", cmd:"meta", command:"meta", meta:"meta", alt:"alt", shift:"shift",
  iskeypad:"iskeypad", isautorepeat:"isautorepeat", leftbuttondown:"leftbuttondown",
  middlebuttondown:"middlebuttondown", rightbuttondown:"rightbuttondown", capslock:"capslock",
  numlock:"numlock", left:"left", right:"right",
};

/** Validate before any host reveal, element focus, operation bind, or input. */
export function browserKeyInput(value: unknown): {keyCode: string; modifiers: string[]} {
  const invalid = (detail: string): never => {
    throw new BrowserCommandError("UNSUPPORTED_BROWSER_KEY", `${detail}. Use a printable ASCII character, ArrowRight/Left/Up/Down, Home, End, Enter, Tab, or an Electron accelerator key; chords use modifiers such as Control+ArrowRight. Use fill for Unicode text.`, "keys");
  };
  if (typeof value !== "string" || !value || value.length > 500) return invalid("Keys must be a nonempty string of at most 500 characters");
  const parts = value.split("+");
  let key: string;
  // A terminal '+' is itself a printable key: '+' or 'Control++'.
  if (value.endsWith("+") && (value === "+" || value.endsWith("++"))) {
    parts.pop(); parts.pop(); key = "+";
  } else {
    const last = parts.pop()!;
    key = last.length === 1 ? last : last.trim();
  }
  const resolvedModifiers = parts.map(part => {
    const token = part.trim().toLowerCase();
    if (token === "controlormeta") return /Mac/i.test(navigator.platform) ? "meta" : "control";
    return Object.hasOwn(modifiers, token) ? modifiers[token] : invalid(`Unsupported modifier ${JSON.stringify(part)}`);
  });
  const keyCode = /^[\x20-\x7e]$/.test(key) ? key : namedKeys.get(key.toLowerCase());
  if (!keyCode) return invalid(`Unsupported key ${JSON.stringify(key)}`);
  return {keyCode, modifiers: resolvedModifiers};
}
