import { useEffect, useRef, useState } from 'react';
import { ApiError } from './api';
import type { AutonomousSession, AutonomousStatus, autonomousResearchApi } from './autonomous-research-api';

export function researchIsRunning(status?: AutonomousStatus) {
  return status === 'RUNNING' || status === 'PAUSE_REQUESTED' || status === 'CANCEL_REQUESTED';
}

function interval(status?: AutonomousStatus) {
  if (researchIsRunning(status)) return 2500;
  // Both states are picked up by the durable scheduler, even without an active attempt.
  return status === 'READY' || status === 'SEALED' ? 7500 : 0;
}

export function useAutonomousSession(
  api: ReturnType<typeof autonomousResearchApi>,
  sessionId: string,
  refresh: number,
  controlInFlight: boolean,
) {
  const [value, setValue] = useState<AutonomousSession | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [pageRevision, setPageRevision] = useState(0);
  const snapshot = useRef<{ value: AutonomousSession; serialized: string } | null>(null);

  function publish(session: AutonomousSession) {
    const previous = snapshot.current;
    if (previous && session.revision < previous.value.revision) return;
    const serialized = JSON.stringify(session);
    if (serialized !== previous?.serialized) {
      snapshot.current = { value: session, serialized };
      setValue(session);
    }
  }

  useEffect(() => {
    if (controlInFlight) return;
    let timer: number | undefined;
    let request: AbortController | undefined;
    let reconcile = true;
    let lastReconciliation = 0;

    async function read() {
      if (document.hidden || request) return;
      const active = new AbortController();
      request = active;
      let nextDelay = interval(snapshot.current?.value.status);
      try {
        const session = await api.read(sessionId, active.signal);
        if (active.signal.aborted) return;
        const previous = snapshot.current?.value;
        if (previous && session.revision < previous.revision) return;
        publish(session);
        setError(null);
        const now = performance.now();
        // Stage events and partial artifacts can be appended without changing revision.
        if (reconcile || session.revision !== previous?.revision ||
            researchIsRunning(session.status) && now - lastReconciliation >= 5000) {
          reconcile = false;
          lastReconciliation = now;
          setPageRevision(n => n + 1);
        }
        nextDelay = interval(session.status);
      } catch (error) {
        if (!active.signal.aborted) {
          setError(error);
          if (error instanceof ApiError && [401, 403, 404].includes(error.status)) nextDelay = 0;
        }
      } finally {
        if (request === active) {
          request = undefined;
          if (!active.signal.aborted && nextDelay && !document.hidden) {
            timer = window.setTimeout(() => void read(), nextDelay);
          }
        }
      }
    }

    function onVisibility() {
      if (document.hidden) {
        window.clearTimeout(timer);
        request?.abort();
        request = undefined;
        reconcile = true;
      } else {
        void read();
      }
    }

    document.addEventListener('visibilitychange', onVisibility);
    void read();
    return () => {
      document.removeEventListener('visibilitychange', onVisibility);
      window.clearTimeout(timer);
      request?.abort();
    };
  }, [api, sessionId, refresh, controlInFlight]);

  return { value, error, setError, publish, pageRevision };
}
