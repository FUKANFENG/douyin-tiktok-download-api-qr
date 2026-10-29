"use strict";
const $ = (id) => document.getElementById(id);
const terminal = new Set(["complete", "cancelled", "expired", "error"]);
const titles = {
  starting: "正在准备",
  waiting: "等待扫码",
  scanned: "等待手机确认",
  verification_required: "需要验证",
  complete: "已保存",
  cancelled: "已取消",
  expired: "已超时",
  error: "暂时未能完成",
};
let csrf = "",
  current = null,
  generation = 0,
  pollTimer = null,
  accounts = [];
const dateFormat = new Intl.DateTimeFormat("zh-CN", {
  timeZone: "Asia/Shanghai",
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
});

function notify(message, success = false) {
  $("notice").textContent = message;
  $("notice").className = success ? "notice success" : "notice";
  $("notice").hidden = !message;
}
async function api(path, options = {}) {
  const headers = { "X-DY-CSRF": csrf, ...options.headers };
  if (options.body) headers["Content-Type"] = "application/json";
  const response = await fetch(path, {
    ...options,
    headers,
    credentials: "same-origin",
    cache: "no-store",
  });
  const result = await response.json().catch(() => ({}));
  if (!response.ok)
    throw new Error(
      typeof result.detail === "string"
        ? result.detail
        : "请求未完成，请稍后再试。",
    );
  return result;
}
function renderSession(session) {
  current = session;
  const active = session && !terminal.has(session.status);
  $("start").hidden = Boolean(active);
  $("refresh").hidden = !active;
  $("cancel").hidden = !active;
  $("account-label").disabled = Boolean(active);
  $("verification-form").hidden = session?.status !== "verification_required";
  $("status-title").textContent = session
    ? titles[session.status] || "正在处理"
    : "准备开始";
  $("status-message").textContent =
    session?.message || "整个过程无需手动打开开发者工具。";
  $("status-dot").className = "status-dot " + (session?.status || "");
  if (session?.qr_available) {
    const imagePath = `/api/logins/${session.id}/qr?v=${session.image_revision}`;
    if ($("qr-image").getAttribute("src") !== imagePath)
      $("qr-image").src = imagePath;
    $("qr-image").hidden = false;
    $("qr-placeholder").hidden = true;
  } else {
    $("qr-image").hidden = true;
    $("qr-image").removeAttribute("src");
    $("qr-placeholder").hidden = false;
    $("placeholder-text").textContent =
      session?.status === "starting"
        ? "正在获取二维码，请稍候…"
        : session?.status === "complete"
          ? "登录信息已保存"
          : "点击下方按钮获取二维码";
  }
  $("start").textContent =
    session?.status === "complete"
      ? "扫码添加另一个账号"
      : session
        ? "重新获取二维码"
        : "获取登录二维码";
  updateCountdown();
}
function updateCountdown() {
  if (!current || terminal.has(current.status)) {
    $("countdown").textContent = "";
    return;
  }
  const seconds = Math.max(
    0,
    Math.ceil((Date.parse(current.expires_at) - Date.now()) / 1000),
  );
  $("countdown").textContent =
    `本次扫码剩余 ${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
}
async function poll(sessionId, token) {
  if (token !== generation) return;
  try {
    const session = await api(`/api/logins/${sessionId}`);
    if (token !== generation) return;
    renderSession(session);
    if (terminal.has(session.status)) {
      if (session.status === "complete") {
        await reloadAccounts();
        notify("登录信息已保存。你可以复制 Cookie 或导出完整登录信息。", true);
      }
      return;
    }
  } catch (error) {
    if (token !== generation) return;
    notify(error.message);
  }
  if (token === generation)
    pollTimer = setTimeout(() => poll(sessionId, token), 1500);
}
async function start(replaceId = null, label = null) {
  const previous = current;
  const token = ++generation;
  clearTimeout(pollTimer);
  notify("");
  $("start").disabled = true;
  $("refresh").disabled = true;
  try {
    if (previous && !terminal.has(previous.status))
      await api(`/api/logins/${previous.id}`, { method: "DELETE" });
    const session = await api("/api/logins", {
      method: "POST",
      body: JSON.stringify({
        label: label ?? $("account-label").value,
        replace_id: replaceId,
      }),
    });
    if (token !== generation) return;
    renderSession(session);
    poll(session.id, token);
  } catch (error) {
    if (token === generation) {
      renderSession(null);
      notify(error.message);
    }
  } finally {
    $("start").disabled = false;
    $("refresh").disabled = false;
  }
}
async function cancel() {
  if (!current) return;
  ++generation;
  clearTimeout(pollTimer);
  try {
    renderSession(await api(`/api/logins/${current.id}`, { method: "DELETE" }));
  } catch (error) {
    notify(error.message);
  }
}
function textElement(tag, className, text) {
  const element = document.createElement(tag);
  element.className = className;
  element.textContent = text;
  return element;
}
function action(text, callback, className = "") {
  const button = textElement("button", className, text);
  button.addEventListener("click", async () => {
    button.disabled = true;
    try {
      await callback();
    } catch (error) {
      notify(error.message);
    } finally {
      button.disabled = false;
    }
  });
  return button;
}
async function exportAccount(account) {
  const data = await api(`/api/accounts/${account.id}/export`);
  const link = document.createElement("a");
  const url = URL.createObjectURL(
    new Blob([JSON.stringify(data, null, 2)], { type: "application/json" }),
  );
  link.href = url;
  link.download = `douyin-login-${account.id}.json`;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 10000);
}
function renderAccounts() {
  $("accounts").replaceChildren();
  if (!accounts.length) {
    $("accounts").append(
      textElement(
        "div",
        "empty",
        "还没有保存的登录信息。完成一次扫码后，会显示在这里。",
      ),
    );
    return;
  }
  for (const account of accounts) {
    const row = textElement("article", "account-row", "");
    const info = document.createElement("div");
    const name = textElement("div", "", "");
    name.append(textElement("span", "account-name", account.label));
    const stateText = {
      valid: "已保存",
      expiring: "即将到期",
      expired: "预计已到期",
      unknown: "期限未知",
    }[account.expiry_state];
    name.append(
      textElement("span", `account-badge ${account.expiry_state}`, stateText),
    );
    const expiry = account.expires_at
      ? dateFormat.format(new Date(account.expires_at)) + " 预计到期"
      : "平台未提供可识别的到期时间";
    info.append(
      name,
      textElement(
        "p",
        "account-details",
        `${expiry} · ${account.cookie_count} 项 Cookie`,
      ),
    );
    const buttons = textElement("div", "account-actions", "");
    buttons.append(
      action("复制 Cookie", async () => {
        const data = await api(`/api/accounts/${account.id}/cookie`);
        if (!navigator.clipboard)
          throw new Error("当前浏览器无法使用剪贴板，请改用导出登录信息。");
        await navigator.clipboard.writeText(data.cookie_header);
        notify("Cookie 已复制到剪贴板。", true);
      }),
      action("导出登录信息", () => exportAccount(account)),
      action("重新扫码", () => {
        $("account-label").value = account.label;
        return start(account.id, account.label);
      }),
      action(
        "删除",
        async () => {
          if (!confirm(`删除“${account.label}”在本机保存的登录信息？`)) return;
          await api(`/api/accounts/${account.id}`, { method: "DELETE" });
          await reloadAccounts();
        },
        "remove",
      ),
    );
    row.append(info, buttons);
    $("accounts").append(row);
  }
}
async function reloadAccounts() {
  accounts = await api("/api/accounts");
  renderAccounts();
}
$("start").addEventListener("click", () => start());
$("refresh").addEventListener("click", () =>
  start(current?.replace_id || null),
);
$("cancel").addEventListener("click", cancel);
$("reload").addEventListener("click", () =>
  reloadAccounts().catch((error) => notify(error.message)),
);
$("verification-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!current) return;
  const code = $("verification-code").value;
  $("verification-code").value = "";
  try {
    await api(`/api/logins/${current.id}/code`, {
      method: "POST",
      body: JSON.stringify({ code }),
    });
    notify("验证码已提交，正在等待确认。", true);
  } catch (error) {
    notify(error.message);
  }
});
setInterval(updateCountdown, 1000);
setInterval(() => {
  if (!document.hidden) reloadAccounts().catch(() => {});
}, 60000);
(async () => {
  try {
    const data = await api("/api/bootstrap");
    csrf = data.csrf_token;
    accounts = data.accounts;
    renderAccounts();
    renderSession(data.session);
    if (data.session && !terminal.has(data.session.status)) {
      generation++;
      poll(data.session.id, generation);
    }
  } catch (error) {
    notify(error.message);
    $("start").disabled = true;
  }
})();
