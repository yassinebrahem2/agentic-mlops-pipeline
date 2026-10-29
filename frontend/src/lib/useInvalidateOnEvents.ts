import { useEffect, useRef } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useProjectEvents, type ProjectEventType } from "@/lib/useProjectEvents";

/**
 * Subscribes to a project's event stream and debounces query key invalidations
 * by 150ms when any of the listed event types or a stream resync occurs.
 * Clears any pending timer on unmount.
 */
export function useInvalidateOnEvents(
  projectId: string | undefined,
  queryKey: readonly unknown[],
  types: readonly ProjectEventType[],
): void {
  const queryClient = useQueryClient();
  const debounceRef = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const queryKeyRef = useRef(queryKey);
  queryKeyRef.current = queryKey;
  const typesRef = useRef(types);
  typesRef.current = types;

  const triggerDebouncedRefetch = () => {
    if (debounceRef.current) {
      clearTimeout(debounceRef.current);
    }
    debounceRef.current = setTimeout(() => {
      void queryClient.invalidateQueries({ queryKey: queryKeyRef.current });
    }, 150);
  };

  useProjectEvents(projectId, {
    onEvent: (event) => {
      if (typesRef.current.includes(event.type)) {
        triggerDebouncedRefetch();
      }
    },
    onResync: triggerDebouncedRefetch,
  });

  useEffect(() => {
    return () => {
      if (debounceRef.current) {
        clearTimeout(debounceRef.current);
      }
    };
  }, []);
}
