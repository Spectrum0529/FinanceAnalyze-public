import { createContext, useContext, useState } from 'react';
import type { ReactNode } from 'react';
import type { RecordData } from './api';
type Selection = {
  reference: RecordData | null;
  label: string;
  select: (reference: RecordData, label: string) => void;
};
const Context = createContext<Selection | null>(null);
export function WorkspaceSelection({ children }: { children: ReactNode }) {
  const [reference, setReference] = useState<RecordData | null>(null),
    [label, setLabel] = useState('');
  return (
    <Context.Provider
      value={{
        reference,
        label,
        select: (r, l) => {
          setReference(r);
          setLabel(l);
        },
      }}
    >
      {children}
    </Context.Provider>
  );
}
export function useWorkspaceSelection() {
  const selection = useContext(Context);
  if (!selection) throw new Error('Workspace selection provider missing');
  return selection;
}
