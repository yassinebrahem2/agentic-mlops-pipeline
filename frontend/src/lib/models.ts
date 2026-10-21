export function isUsableModel(m: {
  availability: string;
  tool_calling: boolean;
  streaming: boolean;
  context_window: number | null;
}): boolean {
  return (
    m.availability === "verified" &&
    m.tool_calling &&
    m.streaming &&
    m.context_window !== null &&
    m.context_window > 0
  );
}

export function hostedModelWarning(provider: string): string {
  return `Files the agent reads are sent to ${provider}. A secret inside a committed file whose name does not look secret can reach it.`;
}
