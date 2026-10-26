export type StreamConnectionState = "live" | "reconnecting" | "closed";

export type StreamLike = {
  addEventListener(type: string, listener: (event: Event) => void): void;
  removeEventListener(type: string, listener: (event: Event) => void): void;
  close(): void;
  readyState: number;
  onerror: ((event: Event) => void) | null;
};

export const STREAM_CONNECTING = 0;
export const STREAM_OPEN = 1;
export const STREAM_CLOSED = 2;

export const RETRY_DELAYS = [1000, 2000, 5000] as const;
export const MAX_RETRY_DELAY = 10000;

export function getRetryDelay(retryCount: number): number {
  return (
    (retryCount < RETRY_DELAYS.length ? RETRY_DELAYS[retryCount] : undefined) ?? MAX_RETRY_DELAY
  );
}

export function openStream(url: string): StreamLike {
  return new EventSource(url);
}

export class StreamBackoff {
  private retry = 0;
  private timer: ReturnType<typeof setTimeout> | undefined;

  reset(): void {
    this.retry = 0;
    this.clear();
  }

  clear(): void {
    if (this.timer !== undefined) {
      clearTimeout(this.timer);
      this.timer = undefined;
    }
  }

  schedule(action: () => void): number {
    this.clear();
    const delay = getRetryDelay(this.retry);
    this.retry += 1;
    this.timer = setTimeout(action, delay);
    return delay;
  }
}

export function isConnecting(stream: StreamLike): boolean {
  return stream.readyState === STREAM_CONNECTING;
}
