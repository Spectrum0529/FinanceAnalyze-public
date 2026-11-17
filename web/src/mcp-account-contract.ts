export const mcpPath = '/operations/mcp-connection';
export const clientIdPattern = /^[a-z][a-z0-9_-]{0,31}$/;
export const mcpScopeNames = {
  read: '只读（原授权）', research: '允许本地回测（原授权）',
  browse: '日常查看', manage: '管理本人资料', workbench: '本机回测与公开行情', external: '有预算的外部研究',
} as const;
export type McpScope = keyof typeof mcpScopeNames;
export type EasyMcpClient = 'codex' | 'deepseek';
export type EasyConnectRequest = {
  client_id: EasyMcpClient;
  access: 'browse' | 'workbench';
  allow_model_budget: boolean;
};
export type EasyConnectionStatus = {
  client_id: EasyMcpClient;
  state: 'authorization_required' | 'paired_waiting_for_client' | 'connected' | 'unavailable';
  scope: McpScope | null;
  reason_code: string | null;
  expires_at: string | null;
  client_confirmed_at: string | null;
};

export function easyConnectionStatus(value: EasyConnectionStatus | null, client: EasyMcpClient) {
  if (value?.client_id !== client || !['authorization_required', 'paired_waiting_for_client', 'connected', 'unavailable'].includes(value.state)) return null;
  const granted = value.state === 'paired_waiting_for_client' || value.state === 'connected';
  if (granted && (typeof value.scope !== 'string' || !Object.hasOwn(mcpScopeNames, value.scope) ||
      typeof value.expires_at !== 'string' || Date.parse(value.expires_at) <= Date.now() ||
      !Number.isFinite(Date.parse(value.expires_at)))) return null;
  if (value.state === 'connected' && (typeof value.client_confirmed_at !== 'string' || !Number.isFinite(Date.parse(value.client_confirmed_at)))) return null;
  return value;
}
export type PairingRequest = { client_id: string; scope: McpScope };
export type PairingReceipt = PairingRequest & { pairing_code: string; expires_at: string };
export type McpGrant = PairingRequest & {
  grant_id: string;
  client_name: string;
  created_at: string;
  expires_at: string;
  status: 'active' | 'revoked' | 'expired';
};
export type McpGrantPage = { items: McpGrant[]; total: number; limit: number; offset: number };
type Stdio = { command: string; args: string[] };
type Configuration = {
  available?: boolean;
  client_id?: string;
  client_name?: string;
  api_url?: string;
  transport?: string;
  scopes?: unknown[];
  scope_labels?: Partial<Record<McpScope, string>>;
  reason_codes?: string[];
  client_config?: { mcpServers?: { financeanalyze?: Stdio } };
  pairing_command?: Stdio;
};

const normalize = (path: string) => path.replaceAll('\\', '/').toLowerCase();

export function publicClientConfiguration(data: Configuration | null, id: string) {
  if (data?.client_id !== id || data.available !== true || data.transport !== 'stdio') return null;
  const server = data.client_config?.mcpServers?.financeanalyze;
  const pairing = data.pairing_command;
  const port = /^http:\/\/127\.0\.0\.1:(\d{4,5})$/.exec(data.api_url ?? '')?.[1];
  if (!port || Number(port) < 1024 || Number(port) > 65535 || !server || !pairing) return null;
  if (typeof server.command !== 'string' || !/^[a-z]:[\\/]/i.test(server.command) || /[\x00-\x1f\x7f]/.test(server.command)) return null;
  const suffix = '/.venv/scripts/python.exe';
  const command = normalize(server.command);
  if (!command.endsWith(suffix) || !Array.isArray(server.args) || server.args.length !== 7) return null;
  const [isolated, noBytecode, script, portFlag, argPort, clientFlag, argId] = server.args;
  if (typeof script !== 'string' || /[\x00-\x1f\x7f]/.test(script) ||
    normalize(script) !== command.slice(0, -suffix.length) + '/tools/platform_ops/mcp_stdio.py' ||
    isolated !== '-I' || noBytecode !== '-B' || portFlag !== '--port' || argPort !== port ||
    clientFlag !== '--client' || argId !== id) return null;
  const pairingArgs = [...server.args, '--pair'];
  if (pairing.command !== server.command || !Array.isArray(pairing.args) ||
      pairing.args.length !== pairingArgs.length || pairing.args.some((value, index) => value !== pairingArgs[index])) return null;
  const scopes = Array.isArray(data.scopes) ? data.scopes.filter((value): value is McpScope =>
    typeof value === 'string' && Object.hasOwn(mcpScopeNames, value)) : [];
  return {
    server: { command: server.command, args: [...server.args] },
    pairing: { command: server.command, args: pairingArgs },
    scopes, scopeLabels: data.scope_labels ?? {},
    canResearch: scopes.includes('research'),
    canRead: scopes.includes('read'),
  };
}

export function clientConfigurationText(server: Stdio, client: 'codex' | 'deepseek' | 'other') {
  if (client === 'codex') {
    return '[mcp_servers.financeanalyze]\ncommand = ' + JSON.stringify(server.command) +
      '\nargs = ' + JSON.stringify(server.args);
  }
  if (client === 'deepseek') {
    return [
      '- id: mcp-financeanalyze',
      "  name: '@deepseek-ai/dsh-mcp-client'",
      '  config:',
      '    serverName: financeanalyze',
      '    transport: stdio',
      '    command: ' + JSON.stringify(server.command),
      '    args: ' + JSON.stringify(server.args),
    ].join('\n');
  }
  return JSON.stringify({ mcpServers: { financeanalyze: server } }, null, 2);
}

export function localPairingCommand({ command, args }: Stdio) {
  // PowerShell treats these four curly apostrophes as single-quote delimiters too.
  const literals = [command, ...args].map(arg => "'" + arg.replace(/['\u2018-\u201b]/g, quote => quote.repeat(2)) + "'");
  return '& ' + literals.join(' ');
}

export function pairingReceipt(value: PairingReceipt | null, expected: PairingRequest) {
  const until = Date.parse(value?.expires_at ?? '');
  return value?.client_id === expected.client_id && value.scope === expected.scope &&
    typeof value.pairing_code === 'string' && /^[\x21-\x7e]{6,80}$/.test(value.pairing_code) &&
    Number.isFinite(until) && until > Date.now() ? value : null;
}

export type McpConfigurationReceipt = Configuration;
