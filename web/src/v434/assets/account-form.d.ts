export type AccountField = {
  name: string;
  label: string;
  type?: 'text' | 'password';
  autocomplete?: string;
  inputMode?: string;
  maxLength?: number;
  required?: boolean;
  help?: string;
  value?: string;
};
export type FormReceipt = { confirmed?: boolean; message?: string; field?: string };
export type AccountForm = {
  form: HTMLFormElement;
  actions: HTMLDivElement;
  destroy(): void;
  reset(): void;
  refreshMotion(): void;
};
export function mountAccountForm<T extends FormReceipt>(host: HTMLElement, options: {
  id: string;
  heading: string;
  headingLevel?: 1 | 2 | 3 | 4;
  description?: string;
  fields: AccountField[];
  submitLabel: string;
  pendingLabel?: string;
  submit: (data: FormData, signal: AbortSignal) => Promise<T>;
  motion?: () => boolean;
  phaseTarget?: HTMLElement;
  onPhase?: (phase: 'idle' | 'verifying' | 'granted' | 'error') => void;
  onConfirmed?: (receipt: T) => void;
  onRejected?: (receipt: T | undefined) => void;
}): AccountForm;
