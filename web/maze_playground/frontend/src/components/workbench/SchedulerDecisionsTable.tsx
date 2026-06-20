import type { SchedulerDecision } from './types';

type SchedulerDecisionsTableProps = {
  decisions: SchedulerDecision[];
  onSelectTask?: (taskId: string) => void;
};

function decisionLabel(type: SchedulerDecision['type']) {
  return type
    .split('_')
    .map((part) => part[0].toUpperCase() + part.slice(1))
    .join(' ');
}

export default function SchedulerDecisionsTable({ decisions, onSelectTask }: SchedulerDecisionsTableProps) {
  if (decisions.length === 0) {
    return <div className="runtime-empty-state">No scheduler decisions match the current filters.</div>;
  }

  return (
    <div className="runtime-table-shell" role="table" aria-label="Scheduler decisions">
      <div className="runtime-table-header runtime-decisions-grid" role="row">
        <span>Time</span>
        <span>Decision</span>
        <span>Task</span>
        <span>Reason</span>
        <span>Result</span>
      </div>
      {decisions.map((decision) => (
        <button
          key={decision.id}
          type="button"
          className="runtime-table-row runtime-decisions-grid"
          title={`${decision.reason}: ${decision.result}`}
          onClick={() => decision.taskId && onSelectTask?.(decision.taskId)}
          disabled={!decision.taskId}
        >
          <span>{decision.time}</span>
          <span className={`runtime-decision-badge is-${decision.severity || 'info'}`}>
            {decisionLabel(decision.type)}
          </span>
          <strong>{decision.taskName || '-'}</strong>
          <span>{decision.reason}</span>
          <span>{decision.result}</span>
        </button>
      ))}
    </div>
  );
}
