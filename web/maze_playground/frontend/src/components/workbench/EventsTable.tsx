import type { RuntimeEvent } from './types';

type EventsTableProps = {
  events: RuntimeEvent[];
  onSelectTask?: (taskId: string) => void;
};

export default function EventsTable({ events, onSelectTask }: EventsTableProps) {
  if (events.length === 0) {
    return <div className="runtime-empty-state">No runtime events match the current filters.</div>;
  }

  return (
    <div className="runtime-table-shell" role="table" aria-label="Runtime events">
      <div className="runtime-table-header runtime-events-grid" role="row">
        <span>Time</span>
        <span>Level</span>
        <span>Task</span>
        <span>Event</span>
        <span>Details</span>
      </div>
      {events.map((event) => (
        <button
          key={event.id}
          type="button"
          className="runtime-table-row runtime-events-grid"
          title={event.details}
          onClick={() => event.taskId && onSelectTask?.(event.taskId)}
          disabled={!event.taskId}
        >
          <span>{event.time}</span>
          <span className={`runtime-level-badge is-${event.level}`}>{event.level}</span>
          <strong>{event.taskName || '-'}</strong>
          <span>{event.event}</span>
          <span>{event.details}</span>
        </button>
      ))}
    </div>
  );
}
