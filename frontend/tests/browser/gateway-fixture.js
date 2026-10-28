// Explicit setup for simulator tests. Opening a dashboard must be read-only.
export async function seedSimulatorRun(port) {
  const response = await fetch(`http://127.0.0.1:${port}/api/v1/runs`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name: 'Invoice Processing Run', task_scope: {
      schema_version: '1', task_description: 'Audit approved vendor invoices and generate summary report.',
      allowed_paths: ['invoices/approved', 'outputs'], blocked_paths: ['invoices/private'],
      allowed_tools: ['workspace'], allowed_operations: ['list_directory', 'read_text', 'write_text'],
      allowed_network_destinations: [], requires_approval: ['delete_path'],
      created_at: new Date().toISOString(),
    } }),
  });
  if (!response.ok) throw new Error(`Simulator setup failed: ${response.status}`);
}
