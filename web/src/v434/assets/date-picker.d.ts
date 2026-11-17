export function createDatePicker(options?:{root?:HTMLElement}):{destroy:()=>void;refresh:()=>void;close:()=>void};
export function parseISODate(value: string): { year: number; month: number; day: number; iso: string } | null;
