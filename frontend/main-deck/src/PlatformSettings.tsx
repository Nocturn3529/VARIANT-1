/** Provider, endpoint, and messaging settings surfaces. */
import {
  CustomEndpointsPanel,
  ProviderAccounts,
  ProviderKeys,
} from "./ProviderCenter";
import {MessagingSettings} from "./MessagingSettings";

export type PlatformSettingsPage = "providers" | "provider-keys" | "custom-endpoints" | "messaging";

export function PlatformSettings({page = "providers"}: {page?: PlatformSettingsPage}) {
  return <div className={`platform-shell platform-shell--${page}`}>
    {page === "providers" ? <ProviderAccounts/> : null}
    {page === "provider-keys" ? <ProviderKeys/> : null}
    {page === "custom-endpoints" ? <CustomEndpointsPanel/> : null}
    {page === "messaging" ? <MessagingSettings/> : null}
  </div>;
}
