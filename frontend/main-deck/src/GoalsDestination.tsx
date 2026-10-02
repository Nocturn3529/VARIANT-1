/** Durable goal controls, independent of session context retrieval. */
import {Button} from "./ui/Button";
import {EmptyState} from "./ui/EmptyState";
import {controlLoop, createLoop, refreshGoals, relativeTimeGoals, selectLoop,
  setDraftLoopTitle, setDraftLoopGoal, useGoalsState} from "./goalsStore";
export function GoalsDestination() {
  const state = useGoalsState();
  const {loops, selectedLoopId, selectedLoop, draftLoopTitle, draftLoopGoal} = state;
  return <div className="memory-scroll deck-destination-scroll">
    <div className="memory-page deck-destination-page">
      <div className="memory-page__utilities" role="toolbar" aria-label="Goal actions">
        <Button tone="quiet" id="goals-refresh" onClick={refreshGoals}>Refresh</Button>
      </div>
        <section className="memory-loops-section deck-instrument">
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
            <Button tone="primary" onClick={createLoop}>Create goal</Button>
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
                    {item.updated ? ` · ${relativeTimeGoals(item.updated)}` : ""}
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
                        if (window.confirm("Delete this project run from Goals?")) {
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

      </div>
  </div>;
}
