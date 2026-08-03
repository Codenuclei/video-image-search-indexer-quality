"use client";

import { useEffect, useEffectEvent, type RefObject } from "react";

/** Close when pointerdown is outside `containerRef`, or Escape is pressed. */
export function useDismissible(
  open: boolean,
  onDismiss: () => void,
  containerRef: RefObject<HTMLElement | null>
) {
  const dismiss = useEffectEvent(onDismiss);

  useEffect(() => {
    if (!open) return;

    const onPointerDown = (event: PointerEvent) => {
      const el = containerRef.current;
      if (!el || el.contains(event.target as Node)) return;
      dismiss();
    };

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") dismiss();
    };

    document.addEventListener("pointerdown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [open, containerRef]);
}
