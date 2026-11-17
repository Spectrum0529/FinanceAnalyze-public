import { ApiError, request } from './api';
export const storageCategories=['market_data','reports','artifacts'] as const;
export type StorageCategory=typeof storageCategories[number];
export type StorageCategoryView={category:StorageCategory;label:string;revision:number;location_id:string|null;directory:string|null;effective_directory:string;default_directory:string;uses_default:boolean};
export type StorageCheck={schema:'storage-location-check-1';category:StorageCategory;directory:string|null;effective_directory:string;valid:boolean;code:string;message:string;creates_only_owned_probe:true};
export type StorageIntent={directory:string|null;expected_revision:number;idempotency_key:string};
export const absoluteWindowsDirectory=(value:unknown):value is string=>typeof value==='string'&&/^[A-Za-z]:[\\/]/.test(value);
const object=(value:unknown):Record<string,unknown>=>value!==null&&typeof value==='object'&&!Array.isArray(value)?value as Record<string,unknown>:{};
const mismatch=()=>new ApiError(0,'STORAGE_RECEIPT_MISMATCH','保存位置的返回信息不匹配，尚未确认操作结果。');
function categoryView(value:unknown,category?:StorageCategory){const row=object(value);
 if(!storageCategories.includes(row.category as StorageCategory)||category!==undefined&&row.category!==category||!Number.isSafeInteger(row.revision)||Number(row.revision)<0||row.location_id!==null&&(typeof row.location_id!=='string'||!row.location_id)||row.location_id===null&&(row.revision!==0||row.directory!==null||row.uses_default!==true)||typeof row.label!=='string'||typeof row.uses_default!=='boolean'||row.uses_default!==(row.directory===null)||row.directory!==null&&!absoluteWindowsDirectory(row.directory)||!absoluteWindowsDirectory(row.effective_directory)||!absoluteWindowsDirectory(row.default_directory))throw mismatch();
 return row as StorageCategoryView;
}
export const storageLocationsApi={
 async get(signal?:AbortSignal){const row=object(await request<unknown>('/storage-locations',{signal}));
  if(row.schema!=='storage-settings-1'||row.effective_for!=='NEW_JOBS_AND_NEW_SYNCHRONOUS_ALLOCATIONS'||row.moves_existing!==false||row.runtime_temporary_files!=='MANAGED_SEPARATELY'||!Array.isArray(row.categories)||row.categories.length!==3)throw mismatch();
  const values=row.categories.map(value=>categoryView(value));if(values.some((value,index)=>value.category!==storageCategories[index]))throw mismatch();return values;
 },
 async check(category:StorageCategory,directory:string|null){const row=object(await request<unknown>(`/storage-locations/${category}/check`,{method:'POST',body:JSON.stringify({directory})}));
  if(row.schema!=='storage-location-check-1'||row.category!==category||(directory===null)!==(row.directory===null)||row.directory!==null&&!absoluteWindowsDirectory(row.directory)||!absoluteWindowsDirectory(row.effective_directory)||typeof row.valid!=='boolean'||typeof row.code!=='string'||typeof row.message!=='string'||row.message.length>2048||row.creates_only_owned_probe!==true||row.valid&&row.code!=='STORAGE_LOCATION_OK')throw mismatch();return row as StorageCheck;
 },
 async save(category:StorageCategory,body:StorageIntent){const row=categoryView(await request<unknown>(`/storage-locations/${category}`,{method:'PUT',body:JSON.stringify(body)}),category);
  if(row.location_id===null||row.revision<body.expected_revision||row.directory!==body.directory)throw mismatch();return row;
 },
};
const storageErrors:Record<string,string>={STORAGE_PATH_REJECTED:'请选择已创建的本机文件夹。系统目录、受保护目录和网络共享目录不可用。',STORAGE_UNAVAILABLE:'文件夹不可用。请确认磁盘已连接、文件夹已创建，并且可写入。',STORAGE_ROOT_CHANGED:'文件夹状态有变化。请重新检查目录。旧文件仍在原处。',STORAGE_REVISION_CONFLICT:'保存位置已变化。请刷新后重新检查目录。',STORAGE_HASH_MISMATCH:'文件检查未通过，操作已停止。',STORAGE_CATEGORY_INVALID:'此类文件的保存位置暂时无法设置。',STORAGE_BINDING_INVALID:'无法确认此任务使用的保存位置，操作已停止。',STORAGE_REFERENCE_INVALID:'无法确认已保存文件的位置，操作已停止。',STORAGE_PAYLOAD_INVALID:'要保存的内容格式有误，操作已停止。',STORAGE_PLATFORM_UNSUPPORTED:'当前运行环境不支持这种保存位置。',IDEMPOTENCY_CONFLICT:'无法确认这是否为上次的操作。请先核对当前使用的目录。',FORBIDDEN:'当前账户或文件夹的权限不允许此操作。'};
export function storageProblem(error:unknown){if(error instanceof ApiError){if(storageErrors[error.code])return storageErrors[error.code];if(error.status===403)return '当前账户无权修改保存位置。';if(error.status===401)return '当前登录无效，请重新进入工作区。';if(error.status===409)return '保存位置已变化。请刷新后重新检查目录。';if(error.status===400||error.status===422)return '请输入带本机盘符的完整路径。系统目录、受保护目录和网络共享目录不可用。';if(error.status===404||error.status===501)return '当前服务暂时无法修改保存位置。';}return '暂时无法确认操作结果。请检查磁盘、文件夹路径和写入权限。';}
