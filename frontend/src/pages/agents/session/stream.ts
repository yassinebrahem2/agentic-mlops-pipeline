export type StreamLike = {
  addEventListener(type: string, listener: (event: Event) => void): void;
  close(): void;
  readyState: number;
  onerror: ((event: Event) => void) | null;
};

export const STREAM_CONNECTING = 0;
export const STREAM_OPEN = 1;
export const STREAM_CLOSED = 2;

export function openStream(url: string): StreamLike {
  return new EventSource(url);
}
