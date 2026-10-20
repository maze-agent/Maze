import assert from 'node:assert/strict';
import test from 'node:test';
import { formatMeasuredBytes, resolveResourceUsage, resourceUsageRows } from './resourceUsage.ts';

test('the usage panel has exactly the nine requested measurements', () => {
  const rows = resourceUsageRows(resolveResourceUsage());
  assert.deepEqual(rows.map(([label]) => label), ['Execution time', 'CPU time', 'CPU average cores',
    'CPU peak cores', 'Memory peak (RSS)', 'GPU memory peak', 'GPU utilization peak', 'Disk read', 'Disk written']);
  assert.ok(rows.every(([,value]) => value === 'Not recorded'));
});

test('recorded zero counters and binary byte units remain visible', () => {
  const rows = Object.fromEntries(resourceUsageRows(resolveResourceUsage({duration_seconds: 2,
    metrics: {cpu_time_seconds: 0, cpu_peak_cores: 0, process_io_read_bytes: 0,
      process_io_write_bytes: 1024, process_rss_peak_bytes: 1024 ** 3}})));
  assert.equal(rows['Execution time'], '2 s');
  assert.equal(rows['CPU time'], '0 s');
  assert.equal(rows['CPU average cores'], '0 cores');
  assert.equal(rows['CPU peak cores'], '0 cores');
  assert.equal(rows['Disk read'], '0 B');
  assert.equal(rows['Memory peak (RSS)'], '1 GiB');
});

test('old model execution records produce one set of service usage values', () => {
  const usage = resolveResourceUsage({duration_seconds: 2, metrics: {cpu_time_seconds: 99,
    model_instance: {measurement_phase: 'task_execution', cpu_time_seconds: 1, cpu_peak_cores: 1.5,
      process_rss_peak_bytes: 2 * 1024 ** 3, gpu_memory_peak_observed_bytes: 4 * 1024 ** 3,
      gpu_utilization_peak_percent: 87}}});
  const rows = Object.fromEntries(resourceUsageRows(usage));
  assert.equal(rows['CPU time'], '1 s');
  assert.equal(rows['CPU average cores'], '0.5 cores');
  assert.equal(rows['GPU memory peak'], '4 GiB');
  assert.equal(rows['GPU utilization peak'], '87%');
});

test('a historical warmup snapshot does not become a task peak', () => {
  const usage = resolveResourceUsage({metrics: {model_instance: {measurement_phase: 'warmup',
    gpu_memory_observed_bytes: 8 * 1024 ** 3}}});
  assert.equal(usage.gpu.memory_peak, null);
});

test('the compact server record is used directly', () => {
  const compact = resolveResourceUsage({duration_seconds: 1, metrics: {cpu_time_seconds: 0.2}});
  assert.deepEqual(resolveResourceUsage({resource_usage: compact}), compact);
});

test('CUDA allocator records remain readable through the single memory peak', () => {
  const usage = resolveResourceUsage({metrics: {memory_scope: 'task',
    gpu_memory_peak_allocated_bytes: 3 * 1024 ** 3, peak_cuda_reserved_bytes: 4 * 1024 ** 3}});
  assert.equal(usage.gpu.memory_peak, 4 * 1024 ** 3);
  for (const invalid of [null, undefined, NaN, Infinity, -1, '1024']) {
    assert.equal(formatMeasuredBytes(invalid), 'Not recorded');
  }
});
