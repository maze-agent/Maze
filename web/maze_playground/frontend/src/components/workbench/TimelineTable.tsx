import type { TimelineItem } from './types';

type TimelineTableProps = {
  timeline: TimelineItem[];
  onSelectTask?: (taskId: string) => void;
};

export default function TimelineTable({ timeline, onSelectTask }: TimelineTableProps) {
  if (timeline.length === 0) {
    return <div className="runtime-empty-state">No timeline items match the current filters.</div>;
  }

  return (
    <div className="runtime-table-shell" role="table" aria-label="Workflow timeline">
      <div className="runtime-table-header runtime-timeline-grid" role="row">
        <span>Task</span>
        <span>State</span>
        <span>Worker</span>
        <span>Started</span>
        <span>End</span>
        <span>Duration</span>
      </div>
      {timeline.map((item) => (
        <button
          key={item.taskId}
          type="button"
          className="runtime-table-row runtime-timeline-grid"
          title={item.queueReason || item.duration || item.state}
          onClick={() => onSelectTask?.(item.taskId)}
        >
          <strong>
            {item.taskName}
            {item.isDynamic && <span className="runtime-dynamic-chip">Dynamic</span>}
          </strong>
          <span className={`runtime-state-badge is-${item.state}`}>{item.state}</span>
          <span>{item.worker || '-'}</span>
          <span>{item.startedAt || '-'}</span>
          <span>{item.endedAt || '-'}</span>
          <span>{item.duration || item.queueReason || '-'}</span>
        </button>
      ))}
    </div>
  );
}
