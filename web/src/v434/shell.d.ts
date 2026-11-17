import type { AccountEvent, SessionRotation, WorkbenchAccess, WorkbenchSession } from '../platform-account-session';

export function startShell(bridge: {
  checkAccess: (signal: AbortSignal) => Promise<WorkbenchAccess>;
  getAccountSession: (signal: AbortSignal) => Promise<WorkbenchSession>;
  acceptSession: (session: WorkbenchSession) => void;
  warmBusiness: (immediate?: boolean) => void;
  logoutAccount: () => Promise<void>;
  watchAccountEvents: (reopen: (kind: AccountEvent) => void) => (kind: AccountEvent) => void;
  mountBusiness: (host: HTMLElement, page: string, session: WorkbenchSession | null,
    onSignedOut: () => void, onSessionRotated: (receipt: SessionRotation, signal: AbortSignal) => Promise<void>,
    motion: () => boolean) => () => void;
}): { render: () => void };
