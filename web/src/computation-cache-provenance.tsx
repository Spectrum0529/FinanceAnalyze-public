import { readCacheDisposition } from './computation-cache-api';

const receipts = {
  hit: '本次复用了已校验的计算缓存。报告属于当前任务，原实验与用量记录照常保留。',
  miss: '本次没有匹配的计算缓存，已执行计算。',
  bypassed: '本次已强制重算，计算期间绕过缓存。',
  disabled: '计算缓存已停用，本次已执行计算。',
  ineligible: '本次计算未使用缓存。',
};

export function ComputationCacheProvenance({ value }: { value: unknown }) {
  const disposition = readCacheDisposition(value);
  return disposition ? <p>{receipts[disposition]}</p> : null;
}
