export function errorMessage(value: unknown): string | undefined {
  if (value === undefined || value === null || value === '') return undefined;
  if (typeof value === 'string') return value;
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  if (typeof value === 'object') {
    const object = value as Record<string, unknown>;
    for (const key of ['message', 'error', 'error_summary', 'detail']) {
      const message = errorMessage(object[key]);
      if (message) return message;
    }
  }
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}
