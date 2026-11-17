import { request } from './api.ts';

type Observer<T> = {
  begin: () => void;
  data: (value: T) => void;
  error: (error: unknown) => void;
  settled: () => void;
};

// Each effect owns its GET signal and returns this reader's cleanup directly.
export function observeResource<T>(
  path: string,
  observer: Observer<T>,
  polling: boolean | number = false,
  stopPolling?: (data: T) => boolean,
) {
  const controller = new AbortController();
  let alive = true;
  let inFlight = false;
  let finished = false;
  let timer: number | undefined;
  const load = async () => {
    if (inFlight || finished) return;
    inFlight = true;
    try {
      const next = await request<T>(path, { signal: controller.signal });
      if (alive) {
        observer.data(next);
        if (stopPolling?.(next)) {
          finished = true;
          window.clearInterval(timer);
        }
      }
    } catch (error) {
      if (alive) observer.error(error);
    } finally {
      inFlight = false;
      if (alive) observer.settled();
    }
  };
  observer.begin();
  void load();
  timer = polling
    ? window.setInterval(
        () => {
          if (!document.hidden) void load();
        },
        typeof polling === 'number' ? Math.max(500, polling) : 4000,
      )
    : undefined;
  return () => {
    alive = false;
    window.clearInterval(timer);
    controller.abort();
  };
}
