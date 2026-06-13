import { ReactNode } from 'react';
import { Alert, Button, Space, Tag, Tooltip, Typography } from 'antd';
import { ShieldCheck, XCircle } from 'lucide-react';

const { Text } = Typography;

interface PendingActionCardProps {
  title?: string;
  actionLabel: string;
  actionColor?: string;
  subject: ReactNode;
  description?: ReactNode;
  tags?: ReactNode[];
  error?: ReactNode;
  approveLabel: string;
  denyLabel?: string;
  approveIcon?: ReactNode;
  loading?: boolean;
  onApprove: () => void;
  onDeny: () => void;
}

export default function PendingActionCard({
  title = 'Pending Action',
  actionLabel,
  actionColor = 'gold',
  subject,
  description,
  tags = [],
  error,
  approveLabel,
  denyLabel = 'Deny',
  approveIcon,
  loading = false,
  onApprove,
  onDeny,
}: PendingActionCardProps) {
  return (
    <div className="pending-action-card">
      <div className="pending-action-title">
        <Space size={6} wrap>
          <ShieldCheck size={15} />
          <Text strong>{title}</Text>
          <Tag color={actionColor}>{actionLabel}</Tag>
        </Space>
        <Tooltip title="Cancel">
          <Button
            size="small"
            icon={<XCircle size={14} />}
            disabled={loading}
            onClick={onDeny}
          />
        </Tooltip>
      </div>
      <Text>{subject}</Text>
      {description ? (
        <Text type="secondary" className="pending-action-description">
          {description}
        </Text>
      ) : null}
      {tags.length > 0 ? (
        <div className="pending-action-meta">
          {tags.map((tag, index) => (
            <span key={index}>{tag}</span>
          ))}
        </div>
      ) : null}
      {error ? (
        <Alert
          type="error"
          showIcon
          message={error}
          style={{ marginTop: 8 }}
        />
      ) : null}
      <Space size={6} wrap style={{ marginTop: 8 }}>
        <Button
          size="small"
          type="primary"
          icon={approveIcon}
          loading={loading}
          onClick={onApprove}
        >
          {approveLabel}
        </Button>
        <Button
          size="small"
          danger
          disabled={loading}
          onClick={onDeny}
        >
          {denyLabel}
        </Button>
      </Space>
    </div>
  );
}
