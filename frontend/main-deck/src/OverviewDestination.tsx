/**
 * React owns VARIANT-1's Overview: a Summary that carries the Python runtime
 * and one tile per section, then Local LLM inference, Live Performance, API
 * Cost & Limits, and Model Usage.
 */
import {useEffect} from "react";
import {LocalInferenceWidget} from "./overview/LocalInferenceWidget";
import {LivePerformanceWidget} from "./overview/LivePerformanceWidget";
import {ApiCostLimitsWidget} from "./overview/ApiCostLimitsWidget";
import {ModelUsageWidget} from "./overview/ModelUsageWidget";
import {requestTelemetry, useOverviewTelemetry} from "./overviewStore";
import {OverviewSummary} from "./overview/OverviewSummary";
import {selectOverviewTab, useOverviewTab, type OverviewTab} from "./overview/overviewTabStore";

const TABS: ReadonlyArray<OverviewTab> = ["summary", "inference", "system", "cloud", "models"];

export function OverviewDestination() {
  const telemetry = useOverviewTelemetry();
  const tab = useOverviewTab();
  useEffect(() => {
    if (telemetry.active) requestTelemetry({notify: true});
  }, [telemetry.active]);
  return <div className="overview-scroll overview-shell overview-widget-shell deck-destination-scroll deck-theme-void">
    <nav className="utility-tabs" aria-label="Overview details">
      {TABS.map(id => <button type="button" key={id} aria-pressed={tab === id} onClick={() => selectOverviewTab(id)}>{id[0].toUpperCase() + id.slice(1)}</button>)}
    </nav>
    <div className="overview-page deck-destination-page" key={tab} data-tab={tab}>
      {tab === "summary" ? <OverviewSummary telemetry={telemetry} onOpen={selectOverviewTab} onRefresh={() => requestTelemetry({notify: true})}/> : null}
      {tab !== "summary" ? <div className="overview-widget-stack">
        {tab === "inference" ? <LocalInferenceWidget telemetry={telemetry.inference} onRefresh={() => requestTelemetry({notify: true})}/> : null}
        {tab === "system" ? <LivePerformanceWidget telemetry={telemetry.hardware}/> : null}
        {tab === "cloud" ? <ApiCostLimitsWidget telemetry={telemetry.cloudUsage}/> : null}
        {tab === "models" ? <ModelUsageWidget telemetry={telemetry.modelUsage}/> : null}
      </div> : null}
    </div>
  </div>;
}
