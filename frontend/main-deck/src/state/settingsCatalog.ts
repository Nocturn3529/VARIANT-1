import type {SettingsCategory} from "./appStore";

type SettingsPageDefinition = Readonly<{
  id: SettingsCategory;
  label: string;
  description: string;
  path: string;
  nested?: boolean;
}>;

export const SETTINGS_PAGES: readonly SettingsPageDefinition[] = [
  {
    id: "general",
    label: "General",
    description: "Density, motion, startup, and local-engine availability.",
    path: "M4 7h16M7 12h10M10 17h4",
  },
  {
    id: "providers",
    label: "Providers",
    description: "Connect subscription and desktop inference accounts.",
    path: "M7 7h10v10H7Zm-4 5h4m10 0h4M12 3v4m0 10v4",
  },
  {
    id: "provider-keys",
    label: "API keys",
    description: "Add or replace provider credentials.",
    path: "M8 11a4 4 0 1 1 7.5 2H21v4h-2v2h-3v-3h-.5A4 4 0 0 1 8 11Z",
    nested: true,
  },
  {
    id: "custom-endpoints",
    label: "Custom endpoints",
    description: "Add, test, discover, and activate compatible inference servers.",
    path: "M4 6h16v12H4Zm4 4h8m-8 4h5",
    nested: true,
  },
  {
    id: "local-models",
    label: "Local models",
    description: "Manage llama.cpp, discover GGUF models, and choose what runs locally.",
    path: "M5 6h14v12H5Zm3 3h8m-8 3h5m-5 3h3",
    nested: true,
  },
  {
    id: "provider-routing",
    label: "Model routing",
    description: "Configure provider recovery and auxiliary model chains.",
    path: "M4 5h6v6H4Zm10 8h6v6h-6M7 11v5h7M10 8h7v5",
    nested: true,
  },
  {
    id: "tools-keys",
    label: "Tools & Keys",
    description: "Manage credentials for search, speech, browsers, and model downloads.",
    path: "M14 4a6 6 0 1 1-4 10l-6 6H2v-3l6-6a6 6 0 0 1 6-7Z",
  },
  {
    id: "search",
    label: "Search",
    description: "Choose and configure web-search providers.",
    path: "M11 5a6 6 0 1 0 0 12 6 6 0 0 0 0-12Zm5 11 4 4",
  },
  {
    id: "browser",
    label: "Browser",
    description: "Browser profiles, the default for new sessions, and this chat’s connection.",
    path: "M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0ZM3 12h18M12 3c5 5 5 13 0 18-5-5-5-13 0-18Z",
  },
  {
    id: "desktop",
    label: "Desktop control",
    description: "CuaDriver status and the permissions this computer needs.",
    path: "M3 5h18v11H3Zm6 15h6m-3-4v4",
  },
  {
    id: "voice",
    label: "Voice",
    description: "Speech input, output, and playback.",
    path: "M12 4a3 3 0 0 0-3 3v5a3 3 0 0 0 6 0V7a3 3 0 0 0-3-3Zm-7 8a7 7 0 0 0 14 0m-7 7v3m-4 0h8",
  },
  {
    id: "messaging",
    label: "Messaging",
    description: "Remote conversations and messaging adapters.",
    path: "M4 5h16v11H8l-4 4Z",
  },
  {
    id: "plugins",
    label: "Plugins",
    description: "Install and enable capability packages.",
    path: "M8 3h8v5h5v8h-5v5H8v-5H3V8h5Z",
  },
  {
    id: "session-context",
    label: "Session context",
    description: "Browse, search, and export frozen views of retained session evidence.",
    path: "M5 3h10l4 4v14H5Zm10 0v5h4M8 12h8m-8 4h8",
  },
  {
    id: "goals",
    label: "Goals",
    description: "Create and manage long-running goals.",
    path: "M5 5c0 2 14 2 14 0v12c0 2-14 2-14 0zM5 11c0 2 14 2 14 0",
  },
  {
    id: "about",
    label: "About VARIANT-1",
    description: "Version, health, updates, and storage.",
    path: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18Zm0-10v6m0-10h.01",
  },
] as const;

