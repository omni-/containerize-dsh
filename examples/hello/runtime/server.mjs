// Harmless stdio MCP fixture: one tool, no filesystem or network access.
import { createInterface } from 'node:readline';

for await (const line of createInterface({ input: process.stdin })) {
  let request;
  try {
    request = JSON.parse(line);
  } catch {
    continue;
  }
  if (request.id === undefined) continue;
  let result;
  switch (request.method) {
    case 'initialize':
      result = { protocolVersion: request.params.protocolVersion,
        capabilities: { tools: {} }, serverInfo: { name: 'hello', version: '1.0.0' } };
      break;
    case 'ping':
      result = {};
      break;
    case 'tools/list':
      result = { tools: [{ name: 'hello', description: 'Return a greeting',
        inputSchema: { type: 'object', properties: {}, additionalProperties: false } }] };
      break;
    case 'tools/call':
      if (request.params.name === 'hello') {
        result = { content: [{ type: 'text', text: 'Hello from the example MCP server.' }] };
      }
      break;
  }
  const response = { jsonrpc: '2.0', id: request.id };
  if (result === undefined) response.error = { code: -32601, message: 'Method or tool not found' };
  else response.result = result;
  process.stdout.write(JSON.stringify(response) + '\n');
}
