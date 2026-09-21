/** 会话管理抽屉（v0.3.5 F2）：查看活跃会话并登出其他所有会话。
 *
 *  安全价值：口令一旦怀疑泄露，"踢掉所有已登录会话"是最直接的止血动作——
 *  以前只能重启管理台进程（会断开所有人，包括自己）。这里保留当前会话，
 *  只登出其他会话；被登出的浏览器下一次请求会拿到 401 并自动回到登录页。
 */

import { api } from "../api.js";
import { $, el, emptyState, tableOf } from "../ui/dom.js";
import { toast } from "../ui/toast.js";
import { confirmAsync, openDrawer } from "../ui/modal.js";
import { humanDuration } from "../lib/format.js";

/** 会话创建时间（墙钟 Unix 时间戳 → 本地时间；异常值不显示为假时间）。 */
function createdText(seconds) {
  const value = Number(seconds);
  if (!Number.isFinite(value) || value <= 0) return "-";
  return new Date(value * 1000).toLocaleString();
}

function sessionsTable(data) {
  const rows = (data.sessions || []).map((item) =>
    el("tr", {}, [
      el("td", {}, [
        el("span", { class: item.current ? "tag online" : "tag", text: item.current ? "当前" : "其他" }),
      ]),
      el("td", { text: item.source || "(未知来源)" }),
      el("td", { text: createdText(item.created_at) }),
      el("td", { text: humanDuration(Math.round(item.expires_in)) }),
      el("td", { class: "mono", text: item.id }),
    ]),
  );
  if (!rows.length) return emptyState("没有活跃会话");
  return tableOf(["标记", "来源", "创建时间", "剩余有效期", "会话指纹"], rows);
}

function sessionsContent(data) {
  const box = el("div", {});
  box.appendChild(
    el("p", {
      class: "muted small mt-0",
      text: `活跃会话 ${data.count} 个（会话只存服务端内存，管理台重启即全部失效）`,
    }),
  );
  box.appendChild(sessionsTable(data));
  const others = (data.sessions || []).filter((item) => !item.current).length;
  const revoke = el("button", {
    class: "danger mt-12",
    text: `登出其他所有会话（${others}）`,
    onclick: async () => {
      const ok = await confirmAsync(
        "登出其他所有会话？",
        `将强制 ${others} 个其他会话重新登录（当前会话保留）。`,
        { danger: true },
      );
      if (!ok) return;
      try {
        const result = await api("/api/actions/sessions-revoke", {
          method: "POST",
          body: { keep_current: true },
        });
        toast(`已登出 ${result.revoked} 个会话`, "ok");
        await openSessions();
      } catch (err) {
        toast("登出失败：" + err.message, "err");
      }
    },
  });
  if (others === 0) revoke.disabled = true;  // 无其他会话时按钮禁用（attr 赋值非 style）
  box.appendChild(revoke);
  return box;
}

/** 打开会话管理抽屉（每次打开都重新拉取，避免展示过期的会话表）。 */
export async function openSessions() {
  try {
    const data = await api("/api/sessions");
    openDrawer("会话管理", sessionsContent(data));
  } catch (err) {
    toast("会话读取失败：" + err.message, "err");
  }
}

/** 绑定入口按钮。 */
export function initSessions() {
  $("sessions-btn").addEventListener("click", openSessions);
}
