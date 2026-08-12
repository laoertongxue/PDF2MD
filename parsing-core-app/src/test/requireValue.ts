export function requireValue<T>(value: T | null | undefined, label = "value"): T {
  if (value === null || value === undefined) throw new Error(`Expected ${label} to exist`);
  return value;
}

export function requireAt<T>(values: readonly T[], index: number, label = "item"): T {
  return requireValue(values[index], `${label} at index ${index}`);
}
