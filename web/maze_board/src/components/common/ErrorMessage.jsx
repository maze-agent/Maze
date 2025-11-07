import React from 'react';

const ErrorMessage = ({ error, onRetry }) => {
  return (
    <div className="flex flex-col items-center justify-center p-8 bg-red-900/20 border border-red-500/50 rounded-lg">
      <div className="text-red-400 text-lg mb-2">⚠️ Error</div>
      <p className="text-gray-300 text-sm mb-4 text-center">
        {error || 'An unexpected error occurred'}
      </p>
      {onRetry && (
        <button
          onClick={onRetry}
          className="px-4 py-2 bg-red-600 hover:bg-red-700 text-white rounded-lg transition"
        >
          Retry
        </button>
      )}
    </div>
  );
};

export default ErrorMessage;

