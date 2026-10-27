import "@testing-library/jest-dom/vitest";

// Node's Request rejects the relative URLs the app's client sends (a browser resolves them against the page),
// so tests resolve them against the jsdom origin.
try {
  new Request("/");
} catch {
  const OriginalRequest = globalThis.Request;
  globalThis.Request = class extends OriginalRequest {
    constructor(input: RequestInfo | URL, init?: RequestInit) {
      if (typeof input === "string" && input.startsWith("/")) {
        const origin =
          typeof window !== "undefined" && window.location.origin
            ? window.location.origin
            : "http://localhost";
        input = `${origin}${input}`;
      }
      super(input, init);
    }
  };
}

if (typeof Element !== "undefined") {
  Element.prototype.scrollIntoView = () => {};
  Element.prototype.hasPointerCapture = () => false;
  Element.prototype.setPointerCapture = () => {};
  Element.prototype.releasePointerCapture = () => {};
}

// jsdom has no ResizeObserver; Radix primitives that size a form control (Switch) construct one.
globalThis.ResizeObserver = class implements ResizeObserver {
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
};
