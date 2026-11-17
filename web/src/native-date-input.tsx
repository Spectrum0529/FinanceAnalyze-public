import { useEffect, useRef } from 'react';
import { createDatePicker } from './v434/assets/date-picker.js';

type DateInputProps = {
  value?: string;
  onChange: (event: { target: HTMLInputElement }) => void;
  disabled?: boolean;
  min?: string;
  max?: string;
  id?: string;
  required?: boolean;
  name?: string;
  'aria-label'?: string;
};

export function NativeDateInput({ value = '', onChange, disabled = false, min, max, ...attributes }: DateInputProps) {
  const host = useRef<HTMLSpanElement>(null);
  const input = useRef<HTMLInputElement | null>(null);
  const reported = useRef(value);
  const notify = useRef(onChange);
  notify.current = onChange;
  useEffect(() => {
    const root = host.current!;
    const element = document.createElement('input');
    element.type = 'date';
    element.value = value;
    for (const [key, val] of Object.entries(attributes)) {
      if (val != null && val !== false) element.setAttribute(key, String(val));
    }
    element.disabled = disabled;
    if (min) element.min = min;
    if (max) element.max = max;
    root.append(element);
    input.current = element;
    const changed = () => {
      if (element.value === reported.current) return;
      reported.current = element.value;
      notify.current({ target: element });
    };
    element.addEventListener('input', changed);
    element.addEventListener('change', changed);
    const picker = createDatePicker({ root });
    return () => {
      picker.destroy();
      element.removeEventListener('input', changed);
      element.removeEventListener('change', changed);
      element.remove();
      input.current = null;
    };
  }, []);
  useEffect(() => {
    const element = input.current;
    if (!element) return;
    // Updates reported from this input already match the DOM and keep its caret.
    if (value !== reported.current) {
      element.value = value;
      reported.current = value;
    }
    element.disabled = disabled;
    if (min) element.min = min; else element.removeAttribute('min');
    if (max) element.max = max; else element.removeAttribute('max');
  }, [value, disabled, min, max]);
  return <span className="native-date-field" ref={host} />;
}
