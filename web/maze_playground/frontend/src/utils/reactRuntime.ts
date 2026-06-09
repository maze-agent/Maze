export const DEFAULT_REACT_TASK_TIMEOUT = {
  local: 60,
  online: 120,
} as const;

export function defaultReactTaskTimeout(mode: 'local' | 'online') {
  return DEFAULT_REACT_TASK_TIMEOUT[mode];
}

export function normalizeReactTaskTimeout(value: unknown, mode: 'local' | 'online') {
  const fallback = defaultReactTaskTimeout(mode);
  const numeric = Number(value);
  if (!Number.isFinite(numeric) || numeric <= 0) {
    return fallback;
  }
  return Math.min(Math.max(Math.round(numeric), 10), 600);
}

export function computeReactRunTimeout(maxSteps: unknown, taskTimeout: unknown) {
  const stepsValue = Number(maxSteps);
  const timeoutValue = Number(taskTimeout);
  const steps = Number.isFinite(stepsValue) && stepsValue > 0 ? Math.ceil(stepsValue) : 4;
  const perStep = Number.isFinite(timeoutValue) && timeoutValue > 0 ? Math.ceil(timeoutValue) : 60;
  return Math.max(90, steps * perStep * 2 + 60);
}
