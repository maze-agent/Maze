import React, { useState } from 'react';

const ApiDocumentation = ({ workflowId, apiConfig }) => {
    const [copiedText, setCopiedText] = useState('');

    const copyToClipboard = (text, label) => {
        navigator.clipboard.writeText(text);
        setCopiedText(label);
        setTimeout(() => setCopiedText(''), 2000);
    };

    const renderPropertyTable = (properties) => {
        return (
            <div className="overflow-x-auto">
                <table className="min-w-full divide-y divide-gray-700">
                    <thead className="bg-gray-900">
                    <tr>
                        <th className="px-4 py-2 text-left text-xs font-medium text-gray-400 uppercase">Name</th>
                        <th className="px-4 py-2 text-left text-xs font-medium text-gray-400 uppercase">Type</th>
                        <th className="px-4 py-2 text-left text-xs font-medium text-gray-400 uppercase">Required</th>
                        <th className="px-4 py-2 text-left text-xs font-medium text-gray-400 uppercase">Description</th>
                        <th className="px-4 py-2 text-left text-xs font-medium text-gray-400 uppercase">Example</th>
                    </tr>
                    </thead>
                    <tbody className="divide-y divide-gray-700">
                    {Object.entries(properties).map(([name, prop]) => (
                        <tr key={name}>
                            <td className="px-4 py-2 text-sm font-mono text-blue-400">{name}</td>
                            <td className="px-4 py-2 text-sm">
                  <span className="px-2 py-1 bg-gray-700 rounded text-xs">
                    {prop.type}
                      {prop.items && ` of ${prop.items.type}`}
                  </span>
                            </td>
                            <td className="px-4 py-2 text-sm">
                                {prop.required ? (
                                    <span className="text-red-400">Yes</span>
                                ) : (
                                    <span className="text-gray-500">No</span>
                                )}
                            </td>
                            <td className="px-4 py-2 text-sm text-gray-300">
                                {prop.description}
                                {prop.default !== undefined && (
                                    <div className="text-xs text-gray-500 mt-1">
                                        Default: <code className="bg-gray-700 px-1 rounded">{JSON.stringify(prop.default)}</code>
                                    </div>
                                )}
                                {prop.enum && (
                                    <div className="text-xs text-gray-500 mt-1">
                                        Enum: {prop.enum.map(v => `"${v}"`).join(', ')}
                                    </div>
                                )}
                            </td>
                            <td className="px-4 py-2 text-sm font-mono text-gray-400">
                                {prop.example !== undefined && JSON.stringify(prop.example)}
                            </td>
                        </tr>
                    ))}
                    </tbody>
                </table>
            </div>
        );
    };

    const exampleRequest = {
        ...Object.fromEntries(
            Object.entries(apiConfig.request_schema.properties)
                .filter(([, prop]) => prop.required || prop.example !== undefined)
                .map(([name, prop]) => [name, prop.example ?? prop.default])
        )
    };

    const curlCommand = `curl -X ${apiConfig.method} \\
  ${apiConfig.endpoint} \\
  -H "Content-Type: application/json" \\
  -H "${apiConfig.auth.header}: YOUR_API_KEY" \\
  -d '${JSON.stringify(exampleRequest, null, 2)}'`;

    const pythonCode = `import requests

url = "${apiConfig.endpoint}"
headers = {
    "Content-Type": "application/json",
    "${apiConfig.auth.header}": "YOUR_API_KEY"
}
data = ${JSON.stringify(exampleRequest, null, 4)}

response = requests.post(url, headers=headers, json=data)
print(response.json())`;

    const javascriptCode = `const response = await fetch("${apiConfig.endpoint}", {
  method: "${apiConfig.method}",
  headers: {
    "Content-Type": "application/json",
    "${apiConfig.auth.header}": "YOUR_API_KEY"
  },
  body: JSON.stringify(${JSON.stringify(exampleRequest, null, 4)})
});

const result = await response.json();
console.log(result);`;

    return (
        <div className="space-y-6">
            {/* Header */}
            <div className="bg-gradient-to-r from-blue-900/50 to-purple-900/50 p-6 rounded-lg border border-blue-700">
                <h3 className="text-2xl font-bold mb-2">API Documentation</h3>
                <p className="text-gray-300">
                    This workflow is exposed as a RESTful API service. Use the endpoint below to submit requests.
                </p>
            </div>

            {/* Endpoint Information */}
            <div className="bg-gray-800 p-6 rounded-lg border border-gray-700">
                <h4 className="text-lg font-semibold mb-4 flex items-center gap-2">
                    <span className="text-blue-400"></span>
                    Endpoint
                </h4>
                <div className="space-y-3">
                    <div className="flex items-center gap-3">
            <span className="px-3 py-1 bg-green-600 rounded font-mono text-sm">
              {apiConfig.method}
            </span>
                        <code className="flex-1 bg-gray-900 px-4 py-2 rounded font-mono text-sm text-blue-300">
                            {apiConfig.endpoint}
                        </code>
                        <button
                            onClick={() => copyToClipboard(apiConfig.endpoint, 'endpoint')}
                            className="px-3 py-1 bg-gray-700 hover:bg-gray-600 rounded text-sm transition"
                        >
                            {copiedText === 'endpoint' ? '✓ Copied' : 'Copy'}
                        </button>
                    </div>
                </div>
            </div>

            {/* Authentication */}
            <div className="bg-gray-800 p-6 rounded-lg border border-gray-700">
                <h4 className="text-lg font-semibold mb-4 flex items-center gap-2">
                    <span className="text-yellow-400"></span>
                    Authentication
                </h4>
                <div className="space-y-2 text-sm">
                    <div className="flex items-center gap-2">
                        <span className="text-gray-400">Type:</span>
                        <span className="px-2 py-1 bg-gray-700 rounded">{apiConfig.auth.type}</span>
                    </div>
                    <div className="flex items-center gap-2">
                        <span className="text-gray-400">Header:</span>
                        <code className="px-2 py-1 bg-gray-900 rounded font-mono text-blue-300">
                            {apiConfig.auth.header}
                        </code>
                    </div>
                    <div className="flex items-center gap-2">
                        <span className="text-gray-400">Required:</span>
                        <span className={apiConfig.auth.required ? 'text-red-400' : 'text-green-400'}>
              {apiConfig.auth.required ? 'Yes' : 'No'}
            </span>
                    </div>
                    <div className="mt-3 p-3 bg-yellow-900/20 border border-yellow-700/50 rounded">
                        <p className="text-xs text-yellow-200">
                            ⚠️ All API requests must include the <code className="bg-gray-900 px-1 rounded">maze_api_key</code> in the request header.
                        </p>
                    </div>
                </div>
            </div>

            {/* Request Schema */}
            <div className="bg-gray-800 p-6 rounded-lg border border-gray-700">
                <h4 className="text-lg font-semibold mb-4 flex items-center gap-2">
                    <span className="text-purple-400"></span>
                    Request Schema
                </h4>
                {renderPropertyTable(apiConfig.request_schema.properties)}
            </div>

            {/* Response Schema */}
            <div className="bg-gray-800 p-6 rounded-lg border border-gray-700">
                <h4 className="text-lg font-semibold mb-4 flex items-center gap-2">
                    <span className="text-green-400"></span>
                    Response Schema
                </h4>
                {renderPropertyTable(apiConfig.response_schema.properties)}
            </div>

            {/* Code Examples */}
            <div className="bg-gray-800 p-6 rounded-lg border border-gray-700">
                <h4 className="text-lg font-semibold mb-4 flex items-center gap-2">
                    <span className="text-cyan-400"></span>
                    Code Examples
                </h4>

                <div className="space-y-4">
                    {/* cURL */}
                    <div>
                        <div className="flex items-center justify-between mb-2">
                            <span className="text-sm font-semibold text-gray-300">cURL</span>
                            <button
                                onClick={() => copyToClipboard(curlCommand, 'curl')}
                                className="px-3 py-1 bg-gray-700 hover:bg-gray-600 rounded text-xs transition"
                            >
                                {copiedText === 'curl' ? '✓ Copied' : 'Copy'}
                            </button>
                        </div>
                        <pre className="bg-gray-900 p-4 rounded overflow-x-auto text-xs">
              <code className="text-green-400">{curlCommand}</code>
            </pre>
                    </div>

                    {/* Python */}
                    <div>
                        <div className="flex items-center justify-between mb-2">
                            <span className="text-sm font-semibold text-gray-300">Python</span>
                            <button
                                onClick={() => copyToClipboard(pythonCode, 'python')}
                                className="px-3 py-1 bg-gray-700 hover:bg-gray-600 rounded text-xs transition"
                            >
                                {copiedText === 'python' ? '✓ Copied' : 'Copy'}
                            </button>
                        </div>
                        <pre className="bg-gray-900 p-4 rounded overflow-x-auto text-xs">
              <code className="text-blue-400">{pythonCode}</code>
            </pre>
                    </div>

                    {/* JavaScript */}
                    <div>
                        <div className="flex items-center justify-between mb-2">
                            <span className="text-sm font-semibold text-gray-300">JavaScript</span>
                            <button
                                onClick={() => copyToClipboard(javascriptCode, 'javascript')}
                                className="px-3 py-1 bg-gray-700 hover:bg-gray-600 rounded text-xs transition"
                            >
                                {copiedText === 'javascript' ? '✓ Copied' : 'Copy'}
                            </button>
                        </div>
                        <pre className="bg-gray-900 p-4 rounded overflow-x-auto text-xs">
              <code className="text-yellow-400">{javascriptCode}</code>
            </pre>
                    </div>
                </div>
            </div>

            {/* Example Response */}
            <div className="bg-gray-800 p-6 rounded-lg border border-gray-700">
                <h4 className="text-lg font-semibold mb-4 flex items-center gap-2">
                    <span className="text-green-400">✨</span>
                    Example Response
                </h4>
                <pre className="bg-gray-900 p-4 rounded overflow-x-auto text-sm">
          <code className="text-green-300">
{JSON.stringify({
    run_id: `run-${workflowId}-${Date.now()}`,
    status: 'accepted',
    message: 'Request accepted and queued for processing'
}, null, 2)}
          </code>
        </pre>
            </div>
        </div>
    );
};

export default ApiDocumentation;