/** 版本管理视图（v0.3.4 F11）：二进制版本状态 + 后台安装任务。
 *
 *  安装是长阻塞的网络操作 → 服务端进程内任务（单飞行），前端 1 秒轮询进度。
 *  运行中的进程不受换链影响；下次 start/restart 生效（页面显式提示）。
 */

import { api } from "../api.js";
import { $, el, relativeTime } from "../ui/dom.js";
import { state } from "../state.js";
import { toast } from "../ui/toast.js";
import { confirmAsync } from "../ui/modal.js";
import { registerView } from "./router.js";
import { doAction } from "./actions.js";

const STATE_LABELS = { running: "进行中", done: "完成", failed: "失败", queued: "排队中" };

/** 拉取版本状态与最近任务。 */
export async function loadVersions() {
  try {
    const data = await api("/api/versions");
    renderVersions(data);
  } catch (err) {
    $("versions-status").textContent = "版本信息读取失败：" + err.message;
    return;
  }
  try {
    const data = await api("/api/tasks");
    renderTasks(data.tasks || []);
  } catch (err) {
    $("versions-tasks").textContent = "任务列表读取失败：" + err.message;
  }
}

function renderVersions(data) {
  const box = $("versions-status");
  const binary = data.binary || {};
  const rows = [
    ["frpsctl", data.frpsctl || "-"],
    ["运行中版本", binary.running || "（未运行）"],
    ["磁盘版本", binary.disk || "（未安装）"],
    ["一致性", binary.running && binary.disk ? (binary.match ? "一致" : "不一致（重启后生效）") : "-"],
    ["最低支持", data.minimum || "-"],
    ["建议版本", data.reckoned || "-"],
  ];
  box.replaceChildren();
  const kv = el("div", { class: "kv" });
  for (const [k, v] of rows) {
    kv.append(el("div", { text: k }), el("div", { text: v }));
  }
  box.appendChild(kv);
  if (data.hint) box.appendChild(el("p", { class: "muted small", text: "⚠ " + data.hint }));
  if (binary.running && binary.disk && !binary.match) {
    box.appendChild(el("div", { class: "row mt-12" }, [
      el("button", { class: "primary", text: "重启实例使新版本生效", onclick: () => doAction("restart") }),
    ]));
  }
  const versionInput = $("ver-version");
  if (!versionInput.value) versionInput.value = data.reckoned || "";
}

function renderTasks(tasks) {
  const box = $("versions-tasks");
  box.replaceChildren();
  if (!tasks.length) {
    box.appendChild(el("div", { class: "muted", text: "暂无安装任务" }));
    return;
  }
  const head = el("thead", {}, [el("tr", {}, [
    el("th", { text: "版本" }),
    el("th", { text: "状态" }),
    el("th", { text: "开始" }),
    el("th", { text: "结果/错误" }),
  ])]);
  const body = el("tbody", {}, tasks.map((task) => el("tr", {}, [
    el("td", { text: task.version || "-" }),
    el("td", {}, [el("span", { class: task.state === "done" ? "tag online" : "tag", text: STATE_LABELS[task.state] || task.state })]),
    el("td", { text: relativeTime(task.started_at) }),
    el("td", { text: task.error || (task.result ? (task.result.switched ? "已切换软链" : "已落盘（未切换）") : "-") }),
  ])));
  box.appendChild(el("table", {}, [head, body]));
}

function renderTaskProgress(task) {
  const box = $("versions-progress");
  box.classList.remove("hidden");
  const phase = task.progress && task.progress.phase ? task.progress.phase : "准备中";
  const received = task.progress && task.progress.received ? task.progress.received : 0;
  const total = task.progress && task.progress.total ? task.progress.total : null;
  const percent = total ? Math.min(100, Math.round((received / total) * 100)) : null;
  // 进度宽度用档位 class（10% 一档）——红线：无内联 style、无 JS style 赋值
  const step = percent === null ? "indeterminate" : `w-${Math.round(percent / 10) * 10}`;
  const bar = el("div", { class: "progress-bar" }, [el("div", { class: "progress-fill " + step })]);
  box.replaceChildren(
    el("div", { class: "row" }, [
      el("strong", { text: `frps ${task.version || ""}` }),
      el("span", { class: "muted small", text: `${STATE_LABELS[task.state] || task.state} · ${phase}${percent === null ? "" : ` · ${percent}%`}` }),
    ]),
    bar,
  );
}

async function startInstall() {
  const version = $("ver-version").value.trim();
  if (!version) { toast("请填写版本号", "err"); return; }
  if (!/^\d+\.\d+\.\d+$/.test(version)) { toast("版本号格式应为 x.y.z", "err"); return; }
  const onlyDownload = $("ver-only-download").checked;
  const yes = await confirmAsync(
    `下载并安装 frps ${version}？`,
    "经官方校验和强校验；运行中的进程不受影响，下次启动生效。" + (onlyDownload ? "（仅落盘，不切换软链）" : ""),
  );
  if (!yes) return;
  try {
    const data = await api("/api/tasks/install", { method: "POST", body: { version, only_download: onlyDownload } });
    state.versionTaskId = data.task_id;
    pollTask();
  } catch (err) {
    toast("安装任务创建失败：\n" + err.message, "err");
  }
}

function pollTask() {
  if (state.versionTaskTimer) clearInterval(state.versionTaskTimer);
  const tick = async () => {
    try {
      const task = await api(`/api/tasks/${encodeURIComponent(state.versionTaskId)}`);
      renderTaskProgress(task);
      if (task.state === "done" || task.state === "failed") {
        clearInterval(state.versionTaskTimer);
        state.versionTaskTimer = null;
        toast(task.state === "done"
          ? `frps ${task.version} 安装完成（下次启动生效）`
          : "安装失败：" + (task.error || "未知错误"),
        task.state === "done" ? "ok" : "err");
        loadVersions();
      }
    } catch (err) {
      clearInterval(state.versionTaskTimer);
      state.versionTaskTimer = null;
      toast("任务状态查询失败：\n" + err.message, "err");
    }
  };
  state.versionTaskTimer = setInterval(tick, 1000);
  tick();
}

/** 注册版本视图与安装表单。 */
export function initVersions() {
  registerView("versions", () => loadVersions());
  $("ver-install").addEventListener("click", startInstall);
  $("versions-refresh").addEventListener("click", () => loadVersions());
}
