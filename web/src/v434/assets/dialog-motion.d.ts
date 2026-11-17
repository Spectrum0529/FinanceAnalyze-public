export function createDialogMotion(dialog: HTMLDialogElement, options?: { motion?: () => boolean }): {
  open(): boolean;
  close(returnValue?: string, options?: { immediate?: boolean }): boolean;
  refreshMotion(): void;
  destroy(): void;
};
