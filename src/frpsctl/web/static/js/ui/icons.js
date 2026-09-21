/** SVG 图标工厂（零外部图标库；线性图标统一 24×24 viewBox）。 */

import { svgEl } from "./dom.js";

/** 图标路径表（stroke 风格，currentColor 着色）。 */
const PATHS = {
  server: ["M4 5h16v5H4z", "M4 14h16v5H4z", "M7 7.5h.01", "M7 16.5h.01"],
  cube: ["M12 3l8 4.5v9L12 21l-8-4.5v-9z", "M12 12l8-4.5", "M12 12v9", "M12 12L4 7.5"],
  download: ["M12 4v11", "M7 11l5 5 5-5", "M5 19h14"],
  search: ["M10.5 4a6.5 6.5 0 1 0 0 13 6.5 6.5 0 0 0 0-13z", "M15.5 15.5L20 20"],
  activity: ["M3 12h4l2.5-6 4 12L16 12h5"],
  chip: ["M7 7h10v10H7z", "M9 3v4", "M15 3v4", "M9 17v4", "M15 17v4", "M3 9h4", "M3 15h4", "M17 9h4", "M17 15h4"],
  terminal: ["M5 5h14v14H5z", "M8 9l3 3-3 3", "M13 15h4"],
  shield: ["M12 3l7 3v6c0 4.5-3 7.5-7 9-4-1.5-7-4.5-7-9V6z"],
  refresh: ["M4 12a8 8 0 0 1 13.5-5.8L20 8", "M20 4v4h-4", "M20 12a8 8 0 0 1-13.5 5.8L4 16", "M4 20v-4h4"],
};

/** 生成一个图标节点。name 未知时返回空 svg（不抛错）。 */
export function icon(name, size = 16) {
  const svg = svgEl("svg", {
    viewBox: "0 0 24 24", width: size, height: size, fill: "none",
    stroke: "currentColor", "stroke-width": "1.7",
    "stroke-linecap": "round", "stroke-linejoin": "round",
    "aria-hidden": "true",
  });
  for (const d of PATHS[name] || []) svg.appendChild(svgEl("path", { d }));
  return svg;
}
