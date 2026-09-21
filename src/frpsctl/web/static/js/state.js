/** 共享可变状态（v0.3.4 模块化：ESM 的跨模块赋值只能发生在持有者模块，
 *  因此全部集中到这个对象；各模块 import { state } 后读写字段）。 */

export const state = {
  // 认证
  csrf: null,
  // 轮询
  timer: null,
  lastStatus: null,
  busy: false,
  // 列表（客户端 / 代理）
  clientsCache: [],
  clientsTotal: 0,
  proxiesCache: [],
  proxiesTotal: 0,
  clientSort: { key: "name", dir: "asc" },
  proxySort: { key: "name", dir: "asc" },
  expandedProxies: new Set(),
  detailCache: new Map(),
  trafficLoading: false,
  // Hero（概览指标条）
  heroRendered: false,
  heroPrev: null,
  sessionBase: null,     // 本次会话的首个累计流量采样（内存态）
  sessionPeak: null,     // 本次会话速率峰值（B/s）
  sessionAvg: null,      // 本次会话速率均值（B/s）
  // 日志
  logStick: true,
  logOffset: null,
  // 配置编辑
  configEntries: [],
  configLoaded: false,
  dirty: new Map(),
  deletes: new Set(),
  addedKeys: [],
  // 历史回滚
  // 审计
  auditLoaded: false,
  auditScope: "plugin",
  auditFilterScope: "",   // 过滤控件当前对应的 scope（变化时才重建字段选项）
  auditRecords: [],       // 已加载的记录（显示顺序：新 → 旧）
  lastAuditData: null,
  auditSince: "",
  // 体检
  doctorRunning: false,
  // 服务视图（v0.3.4）
  // 版本管理（v0.3.4）
  versionTaskId: null,
  versionTaskTimer: null,
  // 快捷键
  gPrefix: 0,
  // 详情抽屉（客户端 / 代理）
};

/** 主题本地存储键。 */
export const THEME_KEY = "frpsctl_web_theme";
/** 会话内流量采样本地存储键。 */
export const SAMPLES_KEY = "frpsctl_web_samples_v1";
/** 展开行明细缓存的存活时间（毫秒）。 */
export const DETAIL_TTL = 60000;
