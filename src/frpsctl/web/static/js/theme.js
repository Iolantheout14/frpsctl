/** 主题（赛博暗默认 / 亮色重绘；支持跟随系统实时变化）。
 *
 *  v0.3.4：切换时短暂启用 `theme-anim` 类，让背景/文字/边框做平滑过渡
 *  （v0.3.3 的 `:root.theme-anim` 样式此前从未被激活——真实缺陷，本轮接通）。
 */

import { $ } from "./ui/dom.js";
import { state, THEME_KEY } from "./state.js";

let animTimer = null;

export function isLight() {
  return document.documentElement.classList.contains("light");
}

/** 应用主题；persist=false 用于"跟随系统"路径（不写入本地存储）。 */
export function applyTheme(light, persist = true) {
  const root = document.documentElement;
  root.classList.add("theme-anim");
  if (animTimer) clearTimeout(animTimer);
  animTimer = setTimeout(() => root.classList.remove("theme-anim"), 300);

  root.classList.toggle("light", light);
  $("theme-toggle").textContent = light ? "赛博暗" : "亮色";
  if (persist) {
    try { localStorage.setItem(THEME_KEY, light ? "light" : "dark"); } catch (e) { /* 隐私模式 */ }
  }
}

/** 初始化主题控件（按钮 + 系统跟随）。 */
export function initTheme() {
  $("theme-toggle").textContent = isLight() ? "赛博暗" : "亮色";
  $("theme-toggle").addEventListener("click", () => applyTheme(!isLight()));
  try {
    const scheme = window.matchMedia("(prefers-color-scheme: light)");
    scheme.addEventListener("change", (e) => {
      try { if (!localStorage.getItem(THEME_KEY)) applyTheme(e.matches, false); } catch (err) { /* 忽略 */ }
    });
  } catch (e) { /* 老浏览器不支持 addEventListener：仅初始跟随 */ }
}
