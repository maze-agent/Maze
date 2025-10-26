import React from 'react';
import LoadingSpinner from './LoadingSpinner';
import ErrorMessage from './ErrorMessage';

/**
 * 数据状态包装组件
 * 统一处理 loading、error、empty 状态
 */
const DataStatusWrapper = ({
  loading,
  error,
  data,
  emptyMessage = 'No data available',
  onRetry,
  children,
}) => {
  if (loading) {
    return <LoadingSpinner />;
  }

  if (error) {
    return <ErrorMessage error={error} onRetry={onRetry} />;
  }

  if (!data || (Array.isArray(data) && data.length === 0)) {
    return (
      <div className="flex items-center justify-center p-8 text-gray-400">
        {emptyMessage}
      </div>
    );
  }

  return <>{children}</>;
};

export default DataStatusWrapper;

