/**
 * React Memory destination island.
 *
 * Owns core facts, approved memories, durable loops,
 * and explicit portable export.
 */
import { KeyboardEvent, useMemo, useState } from "react";
import {TextInputDialog} from "./ui/TextInputDialog";
import {Button} from "./ui/Button";
import {EmptyState} from "./ui/EmptyState";
import {
  addCoreFact,
  approveMemoryProposal,
  controlLoop,
  createLoop,
  deleteArchive,
  deleteCoreFact,
  editCoreFact,
  exportMemory,
  filteredArchive,
  openExportFolder,
  refreshMemory,
  rejectMemoryProposal,
  relativeTimeMemory,
  selectLoop,
  setArchiveQuery,
  setDraftFact,
  setDraftLoopGoal,
  setDraftLoopTitle,
  tidyMemory,
  toggleShownArchive,
  toggleShownFacts,
  useMemoryState,
} from "./memoryStore";
export function MemoryDestination() {
  const state = useMemoryState();
  const [tab, setTab] = useState("core");
  const [editingFact, setEditingFact] = useState<string | null>(null);
  const {
    core,
    proposals,
    coreCount,
    coreCap,
    shownFacts,
    archiveQuery,
    shownArchive,
    loops,
    selectedLoopId,
    selectedLoop,
    draftLoopTitle,
    draftLoopGoal,
    exportState,
    draftFact,
  } = state;

  // Data is requested by the runtime enter/connection hooks so we do not
  // fire memory:list while the WebSocket is still offline.

  const archiveItems = useMemo(() => filteredArchive(state), [state]);
  const visibleCore = core.slice(0, shownFacts);
  const visibleArchive = archiveItems.slice(0, shownArchive);
  const showMoreFactsLabel = shownFacts >= core.length
    ? "Show fewer facts"
    : `Show ${core.length - shownFacts} more facts`;
  const showMoreArchiveLabel = shownArchive >= archiveItems.length
    ? "Show fewer memories"
    : `Load ${Math.min(25, archiveItems.length - shownArchive)} more memories`;

  function onFactKeyDown(event: KeyboardEvent<HTMLInputElement>) {
    if (event.key === "Enter") {
      event.preventDefault();
      addCoreFact();
    }
  }

  return <>
    <div className="memory-scroll deck-destination-scroll">
      <div className="memory-page deck-destination-page">
        <div className="memory-page__utilities" role="toolbar" aria-label="Memory actions">
          <Button tone="quiet" id="memory-refresh" onClick={refreshMemory}>
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20 11a8 8 0 1 0-2.3 5.7M20 4v7h-7" /></svg>
            Refresh
          </Button>
          <Button tone="quiet" id="memory-tidy" onClick={tidyMemory}>
            <svg viewBox="0 0 24 24" aria-hidden="true">
              <path d="m12 3 1.5 4.5L18 9l-4.5 1.5L12 15l-1.5-4.5L6 9l4.5-1.5zM18 15l.8 2.2L21 18l-2.2.8L18 21l-.8-2.2L15 18l2.2-.8z" />
            </svg>
            Tidy memory
          </Button>
        </div>

        <nav className="memory-tabs utility-tabs" aria-label="Memory collections">
          {[{id: "core", label: "Core facts", count: coreCount}, {id: "archive", label: "Archive", count: state.archival.length}, {id: "proposals", label: "Proposals", count: proposals.length}, {id: "loops", label: "Goals", count: loops.length}, {id: "export", label: "Export"}].map(item => <button type="button" key={item.id} aria-pressed={tab === item.id} onClick={() => setTab(item.id)}>{item.label}{item.count !== undefined ? <small>{item.count}</small> : null}</button>)}
        </nav>
        {tab === "proposals" && !proposals.length ? <EmptyState title="No proposals to review" description="Proposed memories appear here before they enter recall."/> : null}
        {proposals.length ? <section className="memory-proposals deck-instrument" hidden={tab !== "proposals"}>
          <div className="memory-section-heading deck-instrument__header">
            <div className="deck-instrument__heading">
              <span className="eyebrow deck-instrument__eyebrow">Needs your approval</span>
              <h2 className="deck-instrument__title">Proposed memories</h2>
              <p className="deck-instrument__description">
                Inferred details stay out of recall until you approve them here.
              </p>
            </div>
          </div>
          <div className="deck-data-list">
            {proposals.map(item => <article className="deck-data-row" key={item.proposalId}>
              <div>
                <strong>{item.content}</strong>
                <small>
                  {item.kind === "update" ? "Update" : "New memory"}
                  {item.createdAt ? ` · ${relativeTimeMemory(item.createdAt)}` : ""}
                </small>
              </div>
              <div className="memory-proposal-actions">
                <Button tone="quiet" onClick={() => rejectMemoryProposal(item.proposalId)}>
                  Reject
                </Button>
                <Button tone="primary" onClick={() => approveMemoryProposal(item.proposalId)}>
                  Approve
                </Button>
              </div>
            </article>)}
          </div>
        </section> : null}

        <div className="memory-workspace-grid">
        <section className="memory-loops-section deck-instrument" hidden={tab !== "loops"}>
          <div className="memory-section-heading deck-instrument__header">
            <div className="deck-instrument__heading">
              <span className="eyebrow deck-instrument__eyebrow">Goal projection</span>
              <h2 className="deck-instrument__title">Long-running goals</h2>
              <p className="deck-instrument__description">
                Multi-hour or multi-day work uses the same Goal records shown in the Detail panel.
              </p>
            </div>
          </div>
          <div className="loop-create-row">
            <input
              type="text"
              aria-label="Project run title"
              placeholder="Title (optional)"
              maxLength={120}
              autoComplete="off"
              value={draftLoopTitle}
              onChange={event => setDraftLoopTitle(event.target.value)}
            />
            <input
              type="text"
              aria-label="Project run goal"
              placeholder="Goal for this project run"
              maxLength={500}
              autoComplete="off"
              value={draftLoopGoal}
              onChange={event => setDraftLoopGoal(event.target.value)}
              onKeyDown={event => {
                if (event.key === "Enter") {
                  event.preventDefault();
                  createLoop();
                }
              }}
            />
            <Button tone="primary" onClick={createLoop}>Start run</Button>
          </div>
          <div className="loop-layout">
            <div
              className="loop-list deck-data-list"
              id="memory-loop-list"
              role="listbox"
              aria-label="Project runs"
            >
              {!loops.length && (
                <EmptyState
                  title="No project runs yet"
                  description="Start one when a goal should outlive a single chat window."
                />
              )}
              {loops.map((item, index) => (
                <article
                  key={item.id}
                  className={`loop-row deck-data-row${selectedLoopId === item.id ? " is-selected" : ""}`}
                  data-status={item.status}
                  role="option"
                  aria-selected={selectedLoopId === item.id}
                  tabIndex={selectedLoopId === item.id || (!selectedLoopId && index === 0) ? 0 : -1}
                  onClick={() => selectLoop(item.id)}
                  onKeyDown={event => {
                    if (event.key === "Enter" || event.key === " ") {
                      event.preventDefault();
                      selectLoop(item.id);
                      return;
                    }
                    let next = index;
                    if (event.key === "ArrowDown") next = Math.min(loops.length - 1, index + 1);
                    else if (event.key === "ArrowUp") next = Math.max(0, index - 1);
                    else if (event.key === "Home") next = 0;
                    else if (event.key === "End") next = loops.length - 1;
                    else return;
                    event.preventDefault();
                    const nextItem = loops[next];
                    if (!nextItem) return;
                    const list = event.currentTarget.parentElement;
                    selectLoop(nextItem.id);
                    requestAnimationFrame(() => {
                      list?.querySelectorAll<HTMLElement>('[role="option"]')[next]?.focus();
                    });
                  }}
                >
                  <div className="loop-row__main">
                    <strong>{item.title}</strong>
                    <span className={`loop-status loop-status--${item.status}`}>{item.status}</span>
                  </div>
                  {item.goal ? <p>{item.goal}</p> : null}
                  <small>
                    done {item.done_count || 0} · blocked {item.blocked_count || 0} · next {item.next_count || 0}
                    {item.updated ? ` · ${relativeTimeMemory(item.updated)}` : ""}
                  </small>
                </article>
              ))}
            </div>
            <div className="loop-detail deck-section" id="memory-loop-detail">
              {!selectedLoop && (
                <EmptyState
                  tone="panel"
                  title="Select a project run"
                  description="Inspect charter, progress, and anchors."
                />
              )}
              {selectedLoop && (
                <>
                  <div className="loop-detail__head">
                    <h3>{selectedLoop.meta?.title || selectedLoop.id}</h3>
                    <span className={`loop-status loop-status--${selectedLoop.meta?.status || "active"}`}>
                      {selectedLoop.meta?.status || "active"}
                    </span>
                  </div>
                  <div className="loop-detail__actions">
                    {(selectedLoop.meta?.status === "active") && (
                      <Button tone="quiet" onClick={() => controlLoop(selectedLoop.id, "pause")}>Pause</Button>
                    )}
                    {(selectedLoop.meta?.status === "paused") && (
                      <Button tone="quiet" onClick={() => controlLoop(selectedLoop.id, "resume")}>Resume</Button>
                    )}
                    {(selectedLoop.meta?.status === "active" || selectedLoop.meta?.status === "paused") && (
                      <Button tone="quiet" onClick={() => controlLoop(selectedLoop.id, "complete")}>Complete</Button>
                    )}
                    {selectedLoop.meta?.status !== "archived" && (
                      <Button tone="quiet" onClick={() => controlLoop(selectedLoop.id, "archive")}>Archive</Button>
                    )}
                    <button
                      type="button"
                      className="loop-detail__danger"
                      onClick={() => {
                        if (window.confirm("Delete this project run from local memory?")) {
                          controlLoop(selectedLoop.id, "delete");
                        }
                      }}
                    >
                      Delete
                    </button>
                  </div>
                  {selectedLoop.meta?.session_id ? (
                    <p className="loop-notes">
                      Bound chat session: <code>{selectedLoop.meta.session_id}</code>
                    </p>
                  ) : (
                    <p className="loop-notes">This Goal is not owned by a chat session.</p>
                  )}
                  <div className="loop-detail__block">
                    <span className="eyebrow">Charter</span>
                    <p><strong>Goal:</strong> {selectedLoop.charter?.goal || "—"}</p>
                    {(selectedLoop.charter?.constraints || []).length > 0 && (
                      <ul>
                        {(selectedLoop.charter?.constraints || []).map((c, i) => (
                          <li key={`c-${i}`}>{c}</li>
                        ))}
                      </ul>
                    )}
                    {(selectedLoop.charter?.success_criteria || []).length > 0 && (
                      <>
                        <span className="eyebrow">Success</span>
                        <ul>
                          {(selectedLoop.charter?.success_criteria || []).map((c, i) => (
                            <li key={`s-${i}`}>{c}</li>
                          ))}
                        </ul>
                      </>
                    )}
                  </div>
                  <div className="loop-detail__block">
                    <span className="eyebrow">Progress</span>
                    <ul className="loop-progress-list">
                      {(selectedLoop.progress?.done || []).map((c, i) => (
                        <li key={`d-${i}`} data-kind="done">[done] {c}</li>
                      ))}
                      {(selectedLoop.progress?.blocked || []).map((c, i) => (
                        <li key={`b-${i}`} data-kind="blocked">[blocked] {c}</li>
                      ))}
                      {(selectedLoop.progress?.next || []).map((c, i) => (
                        <li key={`n-${i}`} data-kind="next">[next] {c}</li>
                      ))}
                    </ul>
                    {selectedLoop.progress?.notes ? (
                      <p className="loop-notes">{selectedLoop.progress.notes}</p>
                    ) : null}
                    {!selectedLoop.progress?.done?.length
                      && !selectedLoop.progress?.blocked?.length
                      && !selectedLoop.progress?.next?.length
                      && !selectedLoop.progress?.notes && (
                      <p className="loop-notes">No progress notes yet.</p>
                    )}
                  </div>
                  <div className="loop-detail__block">
                    <span className="eyebrow">Domain anchors</span>
                    {selectedLoop.anchors?.domains
                      && Object.keys(selectedLoop.anchors.domains).length > 0 ? (
                      <ul>
                        {Object.entries(selectedLoop.anchors.domains).map(([domain, info]) => {
                          const bits = Object.entries(info || {})
                            .filter(([k]) => !["refreshed_at", "ttl_sec", "stale"].includes(k))
                            .map(([k, v]) => `${k}=${String(v)}`);
                          const stale = !!(info as {stale?: boolean})?.stale;
                          return (
                            <li key={domain}>
                              <strong>{domain}</strong>
                              {stale ? " (stale)" : ""}: {bits.join(", ") || "—"}
                            </li>
                          );
                        })}
                      </ul>
                    ) : (
                      <p className="loop-notes">No anchors yet. The agent can set coding/desktop/browser re-entry points.</p>
                    )}
                  </div>
                </>
              )}
            </div>
          </div>
        </section>

          <section className="core-profile-card deck-instrument" hidden={tab !== "core"}>
            <div className="memory-section-heading deck-instrument__header">
              <div className="deck-instrument__heading">
                <span className="eyebrow deck-instrument__eyebrow">Always included</span>
                <h2 className="deck-instrument__title">What VARIANT-1 knows about you</h2>
                <p className="deck-instrument__description">Core facts are editable and travel with every local conversation.</p>
              </div>
              <span className="core-capacity deck-instrument__utility">
                <strong id="core-capacity-label">{coreCount}</strong> of {coreCap}
              </span>
            </div>
            <div className="core-fact-add">
              <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14M5 12h14" /></svg>
              <input
                id="core-fact-input"
                type="text"
                aria-label="New core fact"
                placeholder="Add something VARIANT-1 should always remember"
                maxLength={240}
                autoComplete="off"
                value={draftFact}
                onChange={event => setDraftFact(event.target.value)}
                onKeyDown={onFactKeyDown}
              />
              <Button id="core-fact-add" onClick={addCoreFact}>Remember</Button>
            </div>
            <div className="core-fact-list deck-data-list" id="core-fact-list">
              {visibleCore.map(item => (
                <article
                  key={item.text}
                  className="core-fact-row deck-data-row"
                  data-fact-text={item.text}
                >
                  <span className="fact-category-icon" aria-hidden="true">Aa</span>
                  <div>
                    <strong>{item.text}</strong>
                    <small>
                      {item.ts
                        ? `Core profile · ${relativeTimeMemory(item.ts)}`
                        : "Core profile"}
                    </small>
                  </div>
                  <button
                    type="button"
                    className="fact-edit-button"
                    data-edit-fact=""
                    aria-label="Edit fact"
                    onClick={() => setEditingFact(item.text)}
                  >
                    Edit
                  </button>
                  <button
                    type="button"
                    className="fact-forget-button"
                    data-delete-fact=""
                    aria-label="Forget fact"
                    onClick={() => deleteCoreFact(item.text)}
                  >
                    ×
                  </button>
                </article>
              ))}
              {!core.length && (
                <EmptyState
                  title="No core facts yet"
                  description="Add one above when something should always travel with a prompt."
                />
              )}
            </div>
            <button
              className="show-all-facts"
              type="button"
              id="show-all-facts"
              hidden={core.length <= 5}
              onClick={toggleShownFacts}
            >
              <span>{showMoreFactsLabel}</span>
              <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m8 10 4 4 4-4" /></svg>
            </button>
          </section>

        <section className="archival-memory-section deck-instrument" hidden={tab !== "archive"}>
          <div className="memory-section-heading archival-heading deck-instrument__header">
            <div className="deck-instrument__heading">
              <span className="eyebrow deck-instrument__eyebrow">Long-term recall</span>
              <h2 className="deck-instrument__title">Archival memories</h2>
              <p className="deck-instrument__description">Semantic memories retrieved only when they are relevant to the current conversation.</p>
            </div>
            <label className="archive-search" htmlFor="archive-search-input">
              <svg viewBox="0 0 24 24" aria-hidden="true">
                <circle cx="11" cy="11" r="7" />
                <path d="m20 20-4-4" />
              </svg>
              <input
                id="archive-search-input"
                type="search"
                placeholder="Search by meaning"
                autoComplete="off"
                value={archiveQuery}
                onChange={event => setArchiveQuery(event.target.value)}
              />
            </label>
          </div>
          <div className="archive-list deck-data-list" id="archive-list">
            {visibleArchive.map(item => (
              <article
                key={item.id || item.text}
                className="archive-memory-row deck-data-row"
                data-archive-id={item.id || ""}
              >
                <span className="archive-memory-icon" aria-hidden="true">◇</span>
                <div>
                  <strong>{item.text || ""}</strong>
                  <small>
                    {item.type || "Memory"}
                    {item.created ? ` · ${relativeTimeMemory(item.created)}` : ""}
                  </small>
                </div>
                <span className="archive-memory-type">{item.type || "Memory"}</span>
                <button
                  type="button"
                  className="archive-delete"
                  data-delete-archive={item.id || ""}
                  aria-label="Delete archival memory"
                  onClick={() => deleteArchive(item.id)}
                >
                  ×
                </button>
              </article>
            ))}
            {!archiveItems.length && (
              <EmptyState
                tone="panel"
                title="No matching archival memories"
                description={
                  state.archival.length
                    ? "Try another search."
                    : "Long-term memories will appear here as VARIANT-1 learns."
                }
              />
            )}
          </div>
          <button
            className="archive-load-more"
            type="button"
            id="archive-load-more"
            hidden={archiveItems.length <= 25}
            onClick={toggleShownArchive}
          >
            {showMoreArchiveLabel}
          </button>
        </section>
        </div>

        <div className="memory-management-grid" hidden={tab !== "export"}>
          <section className="memory-management-card memory-export-card deck-instrument">
            <div className="management-card-icon">
              <svg viewBox="0 0 24 24" aria-hidden="true">
                <path d="M12 3v12m0 0 4-4m-4 4-4-4M4 17v3h16v-3" />
              </svg>
            </div>
            <div className="management-card-copy deck-instrument__heading">
              <div className="management-card-title deck-instrument__title-row">
                <strong className="deck-instrument__title">Portable memory export</strong>
                <span className="deck-instrument__status" id="memory-export-state">{exportState}</span>
              </div>
              <p className="deck-instrument__description">
                Write a structured retrieval corpus with lifecycle and provenance.
                Memory facts are not fabricated into chat training pairs.
              </p>
            </div>
            <div className="management-card-actions deck-instrument__controls">
              <Button id="memory-export-run" onClick={exportMemory}>
                Export JSONL
              </Button>
              <Button
                id="memory-export-open"
                onClick={() => { void openExportFolder(); }}
              >
                Open folder
              </Button>
            </div>
          </section>
        </div>
      </div>
    </div>
    {editingFact !== null ? <TextInputDialog title="Edit core fact" label="Fact" initialValue={editingFact} maxLength={2000}
      onSubmit={next => next === editingFact || editCoreFact(editingFact, next)} onClose={() => setEditingFact(null)}/> : null}
  </>;
}
