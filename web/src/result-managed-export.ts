import { ApiError, download, request } from './api';
import type { RecordData } from './api';
import { record } from './native-research-api';

function reportFilename(label: string) {
  const name = label.replace(/\.json$/i, '').replace(/[<>:"/\\|?*\x00-\x1f]/g, '_').trim().replace(/[. ]+$/, '').slice(0, 100);
  return '报告-' + (name || '保存结果') + '.json';
}

export async function exportManagedResult(id: string, title: string, signal: AbortSignal) {
  const reportPath = '/results/' + encodeURIComponent(id);
  try {
    await download(reportPath + '/export', reportFilename(title));
    return;
  } catch (cause) {
    if (!(cause instanceof ApiError) || ![413, 422].includes(cause.status)) throw cause;
  }
  const manifest = await request<RecordData>(reportPath + '/export-manifest', { signal });
  const sha = manifest.source_view_sha256;
  if (manifest.result_id !== id || manifest.format !== 'finance-result-pages-1' ||
      typeof sha !== 'string' || !/^[0-9a-f]{64}$/.test(sha) ||
      !Object.hasOwn(manifest, 'summary') || !Array.isArray(manifest.sections)) {
    throw new Error('完整导出清单与所选报告不匹配。');
  }

  async function readSection(section: string, kind: string, total: number) {
    if (!Number.isSafeInteger(total) || total < 0) throw Error('导出分区未给出有效条目总数。');
    const list: unknown[] = [], chunks: string[] = [];
    const object: Record<string, unknown> = Object.create(null);
    let offset = 0;
    while (offset < total) {
      const query = new URLSearchParams({ section, limit: '250', offset: String(offset), expected_sha256: sha as string });
      // Smaller pages can recover from an unusually large individual report section.
      let page: RecordData;
      try { page = await request<RecordData>(reportPath + '/series?' + query, { signal }); }
      catch (cause) {
        if (!(cause instanceof ApiError) || cause.status !== 413) throw cause;
        query.set('limit', '1');
        page = await request<RecordData>(reportPath + '/series?' + query, { signal });
      }
      if (page.result_id !== id || page.source_view_sha256 !== sha || page.section !== section ||
          page.kind !== kind || page.offset !== offset || page.total !== total) throw Error('导出期间保存报告或分区已改变。');
      let count = 0;
      if (kind === 'text' && typeof page.text === 'string') {
        chunks.push(page.text); count = Array.from(page.text).length;
      } else if (kind === 'list' && Array.isArray(page.items)) {
        for (const item of page.items) list.push(await unpack(item, section + '/' + (offset + count++)));
      } else if (kind === 'object' && Array.isArray(page.items)) {
        for (const raw of page.items) {
          const item = record(raw);
          if (typeof item.key !== 'string' || Object.hasOwn(object, item.key)) throw Error('导出分区对象字段重复或不完整。');
          object[item.key] = await unpack(item.value, pointer(section, item.key)); count++;
        }
      } else throw Error('导出分区返回了无效内容。');
      const next = offset + count;
      if (!count || next > total || page.next_offset !== (next < total ? next : null)) throw Error('导出分区未返回完整分页。');
      offset = next;
    }
    return kind === 'text' ? chunks.join('') : kind === 'list' ? list : object;
  }

  function pointer(base: string, key: string) {
    return base + '/' + key.replace(/~/g, '~0').replace(/\//g, '~1');
  }
  async function unpack(value: unknown, path: string): Promise<unknown> {
    if (Array.isArray(value)) return Promise.all(value.map((item, index) => unpack(item, path + '/' + index)));
    if (!value || typeof value !== 'object') return value;
    const projected = record(value);
    if (projected._paged === true) {
      if (projected.section !== path || !['list', 'text', 'object'].includes(String(projected.kind)))
        throw Error('导出引用的分区与原报告位置不一致。');
      return readSection(path, String(projected.kind), Number(projected.total));
    }
    const result: Record<string, unknown> = Object.create(null);
    for (const [key, child] of Object.entries(projected)) result[key] = await unpack(child, pointer(path, key));
    return result;
  }

  const report = await unpack(manifest.summary, '');
  const series: Record<string, unknown> = Object.create(null);
  for (const raw of manifest.sections) {
    const section = record(raw);
    if (section.native_saved_series === true && typeof section.section === 'string' && /^@[a-z_]+$/.test(section.section)) {
      series[section.section.slice(1)] = await readSection(section.section, 'list', Number(section.total));
    }
  }
  signal.throwIfAborted();
  const value = Object.keys(series).length ? { run_id: id.split(':')[1], report, series } : report;
  const url = URL.createObjectURL(new Blob([JSON.stringify(value)], { type: 'application/json' }));
  const link = document.createElement('a');
  link.href = url;
  link.download = reportFilename(typeof manifest.filename === 'string' ? manifest.filename : title);
  document.body.append(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}
