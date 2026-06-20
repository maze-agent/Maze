import type { RuntimeLogLine } from './types';

type LogsPanelProps = {
  logs: RuntimeLogLine[];
  onSelectTask?: (taskId: string) => void;
};

export default function LogsPanel({ logs, onSelectTask }: LogsPanelProps) {
  if (logs.length === 0) {
    return <div className="runtime-empty-state">No logs available for the current filters.</div>;
  }

  return (
    <div className="runtime-table-shell" role="table" aria-label="Task logs">
      <div className="runtime-table-header runtime-logs-grid" role="row">
        <span>Time</span>
        <span>Level</span>
        <span>Task</span>
        <span>Message</span>
      </div>
      {logs.map((line) => (
        <button
          key={line.id}
          type="button"
          className="runtime-table-row runtime-logs-grid"
          title={line.message}
          onClick={() => onSelectTask?.(line.taskId)}
        >
          <span>{line.time}</span>
          <span className={`runtime-level-badge is-${line.level}`}>{line.level}</span>
          <strong>{line.taskName}</strong>
          <span>{line.message}</span>
        </button>
      ))}
    </div>
  );
}
