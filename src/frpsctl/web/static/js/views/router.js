/** 视图路由：导航切换 + 首展钩子（各视图懒加载自行注册，避免循环依赖）。 */

import { $ } from "../ui/dom.js";

/** 视图名 → 首展回调（重复进入不重复触发；需要每次刷新时由视图自管）。 */
const showHooks = new Map();

/** 注册视图的首展回调（config/audit/services/versions 使用）。 */
export function registerView(name, onShow) {
  showHooks.set(name, onShow);
}

const VIEWS = ["dash", "config", "audit", "services", "versions"];

/** 视图名 → (视图容器 id, 导航按钮 id)：全部字面量，便于守卫静态核对。 */
const VIEW_IDS = {
  dash: ["view-dash", "nav-dash"],
  config: ["view-config", "nav-config"],
  audit: ["view-audit", "nav-audit"],
  services: ["view-services", "nav-services"],
  versions: ["view-versions", "nav-versions"],
};

/** 切换视图（含入场动画与首展钩子）。 */
export function switchView(which) {
  if (!VIEWS.includes(which)) return;
  for (const name of VIEWS) {
    const visible = name === which;
    const [viewId, navId] = VIEW_IDS[name];
    $(viewId).classList.toggle("hidden", !visible);
    $(navId).classList.toggle("active", visible);
  }
  const view = $(VIEW_IDS[which][0]);
  view.classList.remove("view-anim");
  void view.offsetWidth;  // 触发重排，让动画重新播放
  view.classList.add("view-anim");
  const hook = showHooks.get(which);
  if (hook) hook();
}

/** 绑定导航按钮（含窄屏菜单折叠）。 */
export function initRouter() {
  for (const name of VIEWS) {
    const navId = VIEW_IDS[name][1];
    $(navId).addEventListener("click", () => switchView(name));
  }
  $("menu-toggle").addEventListener("click", () => $("menu-toggle").nextElementSibling.classList.toggle("open"));
}
