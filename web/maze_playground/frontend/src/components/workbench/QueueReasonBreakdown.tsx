import type { RuntimeBreakdownItem } from './types';

type QueueReasonBreakdownProps = {
  items: RuntimeBreakdownItem[];
  title: string;
  centerLabel: string;
};

const QUEUE_LEGEND = [
  { key: 'cpu', label: 'CPU', color: '#2563eb' },
  { key: 'gpu', label: 'GPU', color: '#7c3aed' },
  { key: 'i/o', label: 'I/O', color: '#12b76a' },
];

function normalizeReason(value: string) {
  const normalized = value.toLowerCase().replace(/\s+/g, '');
  if (normalized === 'io' || normalized === 'i/o') return 'i/o';
  return normalized;
}

export default function QueueReasonBreakdown({
  items,
  title,
  centerLabel,
}: QueueReasonBreakdownProps) {
  const total = items.reduce((sum, item) => sum + item.count, 0);
  const radius = 42;
  const circumference = 2 * Math.PI * radius;
  let offset = 0;
  const byReason = new Map(items.map((item) => [normalizeReason(item.reason), item]));
  const legendItems = QUEUE_LEGEND.map((entry) => {
    const item = byReason.get(entry.key);
    return {
      ...entry,
      count: item?.count || 0,
      percent: item?.percent || 0,
      color: item?.color || entry.color,
    };
  });

  return (
    <aside className="runtime-queue-breakdown" aria-label={title}>
      <div className="runtime-breakdown-topline">
        <div className="runtime-summary-title">{title}</div>
      </div>
      <div className="runtime-queue-breakdown-body">
        <div className="runtime-queue-donut-wrap">
          <div className="runtime-queue-donut">
            <svg viewBox="0 0 120 120" role="img" aria-label={title}>
              <circle className="runtime-queue-donut-track" cx="60" cy="60" r={radius} />
              {items.map((item) => {
                const length = total ? (item.count / total) * circumference : 0;
                const segment = (
                  <circle
                    key={item.reason}
                    className="runtime-queue-donut-segment"
                    cx="60"
                    cy="60"
                    r={radius}
                    stroke={item.color || '#667085'}
                    strokeDasharray={`${length} ${circumference - length}`}
                    strokeDashoffset={-offset}
                  >
                    <title>{`${item.reason}: ${item.count} queued task${item.count === 1 ? '' : 's'} (${item.percent}%)`}</title>
                  </circle>
                );
                offset += length;
                return segment;
              })}
            </svg>
            <div className="runtime-queue-donut-center">
              <strong>{total}</strong>
              <span>{centerLabel}</span>
            </div>
          </div>
        </div>
        <div className="runtime-queue-legend" aria-label="Queue resource legend">
          {legendItems.map((item) => (
            <div
              className="runtime-queue-legend-item"
              key={item.key}
              title={`${item.label}: ${item.count} queued task${item.count === 1 ? '' : 's'} (${item.percent}%)`}
            >
              <i style={{ background: item.color }} />
              <strong>{item.label}</strong>
            </div>
          ))}
        </div>
      </div>
    </aside>
  );
}
