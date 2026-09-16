/** Collapse background triggers, including those arriving during a slow read. */
export function createRefreshQueue(read: () => Promise<void>, report: (error: unknown) => void, delay = 200) {
  let disposed = false;
  let running = false;
  let dirty = false;
  let timer: ReturnType<typeof setTimeout> | undefined;
  const request = () => {
    if (disposed) return;
    dirty = true;
    if (running || timer) return;
    timer = setTimeout(async () => {
      timer = undefined;
      if (disposed) return;
      running = true; dirty = false;
      try { await read(); } catch (error) { if (!disposed) report(error); }
      finally { running = false; if (dirty && !disposed) request(); }
    }, delay);
  };
  return {request, dispose() { disposed = true; dirty = false; clearTimeout(timer); }};
}
