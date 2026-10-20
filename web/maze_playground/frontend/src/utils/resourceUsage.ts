import type { ResourceUsage, UnifiedRunTaskSnapshot } from '../types/workflow';

function measurement(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null;
}

export function formatMeasuredBytes(value: unknown): string {
  const bytes = measurement(value);
  if (bytes === null) return 'Not recorded';
  if (bytes === 0) return '0 B';
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
  const index = Math.max(0, Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1));
  return `${Number((bytes / 1024 ** index).toFixed(2))} ${units[index]}`;
}

function formatNumber(value: unknown, unit: string): string {
  const number = measurement(value);
  if (number === null) return 'Not recorded';
  if (number > 0 && number < 0.001) return `<0.001 ${unit}`;
  return `${Number(number.toFixed(3))} ${unit}`;
}

export function resolveResourceUsage(task?: Pick<UnifiedRunTaskSnapshot, 'resource_usage' | 'metrics' | 'duration_seconds'> | null): ResourceUsage {
  if (task?.resource_usage) return task.resource_usage;
  if (task?.metrics?.resource_usage) return task.metrics.resource_usage;
  // Historical runs keep their original metrics; expose the same compact view.
  const metrics = task?.metrics || {};
  const model = metrics.model_instance;
  const source = model?.measurement_phase === 'task_execution' ? model : metrics;
  const duration = measurement(task?.duration_seconds);
  const time = measurement(source.cpu_time_seconds);
  let gpuPeak = measurement(source.gpu_memory_peak_observed_bytes);
  if (gpuPeak === null && source === metrics && metrics.memory_scope !== 'model_instance') {
    const peaks = [metrics.gpu_memory_peak_allocated_bytes, metrics.gpu_memory_peak_reserved_bytes,
      metrics.peak_cuda_allocated_bytes, metrics.peak_cuda_reserved_bytes]
      .map(measurement).filter((value): value is number => value !== null);
    gpuPeak = peaks.length ? Math.max(...peaks) : null;
  }
  return {
    duration_seconds: duration,
    cpu: { time, average_cores: time !== null && duration ? time / duration : measurement(source.cpu_average_cores),
      peak_cores: measurement(source.cpu_peak_cores) },
    memory: { process_rss_peak_bytes: measurement(source.process_rss_peak_bytes) },
    gpu: { memory_peak: gpuPeak, utilization_peak: measurement(source.gpu_utilization_peak_percent) },
    disk: { read_bytes: measurement(source.process_io_read_bytes), write_bytes: measurement(source.process_io_write_bytes) },
  };
}

export function resourceUsageRows(usage: ResourceUsage): Array<[string, string]> {
  const busy = measurement(usage.gpu.utilization_peak);
  return [
    ['Execution time', formatNumber(usage.duration_seconds, 's')],
    ['CPU time', formatNumber(usage.cpu.time, 's')],
    ['CPU average cores', formatNumber(usage.cpu.average_cores, 'cores')],
    ['CPU peak cores', formatNumber(usage.cpu.peak_cores, 'cores')],
    ['Memory peak (RSS)', formatMeasuredBytes(usage.memory.process_rss_peak_bytes)],
    ['GPU memory peak', formatMeasuredBytes(usage.gpu.memory_peak)],
    ['GPU utilization peak', busy === null ? 'Not recorded' : `${Number(busy.toFixed(1))}%`],
    ['Disk read', formatMeasuredBytes(usage.disk.read_bytes)],
    ['Disk written', formatMeasuredBytes(usage.disk.write_bytes)],
  ];
}
