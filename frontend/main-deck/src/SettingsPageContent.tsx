import {MemoryDestination} from "./MemoryDestination";
import type {SettingsCategory} from "./state/appStore";
import {AboutSettings} from "./AboutSettings";
import {AgentToolsSettings} from "./AgentToolsSettings";
import {GeneralSettings} from "./GeneralSettings";
import {PlatformSettings} from "./PlatformSettings";
import {PluginsSettings} from "./PluginsSettings";
import {SettingsPage} from "./ui/Settings";
import {VoiceSettings} from "./VoiceSettings";
import {ToolsAndKeysSettings} from "./ToolsAndKeysSettings";
import {BrowserSettings} from "./BrowserSettings";

export function SettingsPageContent({category}: {category: SettingsCategory}) {
  const content = {
    general: <GeneralSettings page="general"/>,
    providers: <PlatformSettings page="providers"/>,
    "provider-keys": <PlatformSettings page="provider-keys"/>,
    "custom-endpoints": <PlatformSettings page="custom-endpoints"/>,
    "local-models": <GeneralSettings page="local-models"/>,
    "tools-keys": <ToolsAndKeysSettings/>,
    search: <AgentToolsSettings/>,
    browser: <BrowserSettings/>,
    voice: <VoiceSettings/>,
    messaging: <PlatformSettings page="messaging"/>,
    plugins: <PluginsSettings/>,
    about: <AboutSettings/>,
    memory: <MemoryDestination/>,
  };
  return <SettingsPage>{content[category]}</SettingsPage>;
}
