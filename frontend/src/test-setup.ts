import "@testing-library/jest-dom/vitest";

// jsdom doesn't implement ResizeObserver -- useMeasuredWidth (used by
// FermentationChart) needs one to exist at all; components that measure
// their own size fall back to width 0 without this, which is fine for
// jsdom's non-visual layout anyway.
class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}

globalThis.ResizeObserver ??= ResizeObserverStub as unknown as typeof ResizeObserver;
