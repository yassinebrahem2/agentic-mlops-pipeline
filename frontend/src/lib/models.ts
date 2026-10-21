export function hostedModelWarning(provider: string): string {
  return `Files the agent reads are sent to ${provider}. A secret inside a committed file whose name does not look secret can reach it.`;
}
