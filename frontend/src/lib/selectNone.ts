export const SELECT_NONE = "__none__";

export function toSelectValue(value: string | null | undefined): string {
  return value ?? SELECT_NONE;
}

export function fromSelectValue(value: string): string | null {
  return value === SELECT_NONE ? null : value;
}
