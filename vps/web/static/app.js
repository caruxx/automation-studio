"use strict";

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (options.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const response = await fetch(path, {
    ...options,
    headers,
    credentials: "same-origin",
  });
  const contentType = response.headers.get("content-type") || "";
  const body = contentType.includes("application/json") ? await response.json() : null;
  if (!response.ok) {
    const error = new Error("Request failed");
    error.status = response.status;
    error.detail = body && Object.prototype.hasOwnProperty.call(body, "detail")
      ? body.detail
      : response.statusText;
    throw error;
  }
  return {body, response};
}

function detailText(detail) {
  return typeof detail === "string" ? detail : JSON.stringify(detail);
}

function setMessage(element, detail) {
  element.textContent = detail ? detailText(detail) : "";
}

function formatDate(value) {
  if (!value) return "未設定";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("ja-JP");
}

function appendCell(row, value) {
  const cell = document.createElement("td");
  cell.textContent = String(value ?? "");
  row.appendChild(cell);
  return cell;
}

async function copyText(value) {
  if (navigator.clipboard && window.isSecureContext) {
    await navigator.clipboard.writeText(value);
    return;
  }
  const textarea = document.createElement("textarea");
  textarea.value = value;
  textarea.style.position = "fixed";
  textarea.style.opacity = "0";
  document.body.appendChild(textarea);
  textarea.select();
  document.execCommand("copy");
  textarea.remove();
}

function initLogin() {
  const form = document.getElementById("login-form");
  const message = document.getElementById("message");
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    setMessage(message, "");
    const button = form.querySelector("button[type='submit']");
    button.disabled = true;
    try {
      const payload = {
        email: document.getElementById("email").value,
        password: document.getElementById("password").value,
        totp_code: document.getElementById("totp-code").value || null,
      };
      const result = await api("/api/auth/login", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      const requiresSetup = result.response.headers.get("X-MFA-Setup-Required") === "true"
        || (result.body && result.body.auth_state === "mfa_setup");
      window.location.replace(requiresSetup ? "/totp-setup.html" : "/index.html");
    } catch (error) {
      setMessage(message, error.detail);
      button.disabled = false;
    }
  });
}

async function initTotpSetup() {
  const message = document.getElementById("message");
  const setupContent = document.getElementById("setup-content");
  try {
    const {body} = await api("/api/auth/totp/setup", {method: "POST"});
    document.getElementById("totp-secret").value = body.secret;
    document.getElementById("otpauth-uri").value = body.otpauth_uri;
    setupContent.hidden = false;
  } catch (error) {
    if (error.status === 401 || error.status === 403) {
      window.location.replace("/login.html");
      return;
    }
    setMessage(message, error.detail);
  }

  document.querySelectorAll("[data-copy-target]").forEach((button) => {
    button.addEventListener("click", async () => {
      await copyText(document.getElementById(button.dataset.copyTarget).value);
      button.textContent = "コピー済み";
    });
  });

  const form = document.getElementById("totp-verify-form");
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    setMessage(message, "");
    const button = form.querySelector("button[type='submit']");
    button.disabled = true;
    try {
      const {body} = await api("/api/auth/totp/verify", {
        method: "POST",
        body: JSON.stringify({code: document.getElementById("verify-code").value}),
      });
      setupContent.hidden = true;
      const list = document.getElementById("recovery-codes");
      body.recovery_codes.forEach((code) => {
        const item = document.createElement("li");
        item.textContent = code;
        list.appendChild(item);
      });
      document.getElementById("recovery-content").hidden = false;
      document.getElementById("copy-recovery").addEventListener("click", async (copyEvent) => {
        await copyText(body.recovery_codes.join("\n"));
        copyEvent.currentTarget.textContent = "コピー済み";
      });
    } catch (error) {
      setMessage(message, error.detail);
      button.disabled = false;
    }
  });
}

async function loadChannels() {
  const message = document.getElementById("channels-message");
  const bodyElement = document.getElementById("channels-body");
  const {body: channels} = await api("/api/channels");
  const statuses = await Promise.all(channels.map(async (channel) => {
    try {
      const {body} = await api(`/api/channels/${channel.id}/oauth/status`);
      return body;
    } catch (error) {
      return {error: error.detail};
    }
  }));

  const names = new Map();
  channels.forEach((channel, index) => {
    names.set(channel.id, channel.name || channel.channel_key);
    const oauthStatus = statuses[index];
    const row = document.createElement("tr");
    appendCell(row, channel.id);
    appendCell(row, channel.channel_key);
    appendCell(row, channel.name);
    appendCell(row, channel.is_default ? "はい" : "いいえ");
    appendCell(row, oauthStatus.error ? detailText(oauthStatus.error) : (oauthStatus.has_credentials ? "接続済み" : "未接続"));
    appendCell(row, oauthStatus.error ? "" : formatDate(oauthStatus.credentials_updated_at));
    const actionCell = appendCell(row, "");
    if (!oauthStatus.error && !oauthStatus.has_credentials) {
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = "YouTubeと接続";
      button.addEventListener("click", async () => {
        button.disabled = true;
        setMessage(message, "");
        try {
          const {body} = await api(`/api/channels/${channel.id}/oauth/start`, {method: "POST"});
          window.location.assign(body.authorization_url);
        } catch (error) {
          setMessage(message, error.detail);
          button.disabled = false;
        }
      });
      actionCell.appendChild(button);
    }
    bodyElement.appendChild(row);
  });
  return names;
}

async function loadJobs(channelNames) {
  const {body} = await api("/api/jobs?page=1&page_size=20");
  const jobsBody = document.getElementById("jobs-body");
  body.items.forEach((job) => {
    const row = document.createElement("tr");
    appendCell(row, job.state);
    appendCell(row, job.job_type);
    appendCell(row, channelNames.get(job.channel_id) || job.channel_id);
    appendCell(row, formatDate(job.created_at));
    jobsBody.appendChild(row);
  });
}

async function initDashboard() {
  try {
    const {body: user} = await api("/api/auth/check");
    document.getElementById("user-info").textContent = `${user.name || user.email} (${user.email}) / ${user.role}`;
  } catch (error) {
    if (error.status === 401 || error.status === 403) {
      window.location.replace("/login.html");
      return;
    }
    document.getElementById("user-info").textContent = detailText(error.detail);
    return;
  }

  let channelNames = new Map();
  try {
    channelNames = await loadChannels();
  } catch (error) {
    setMessage(document.getElementById("channels-message"), error.detail);
  }
  try {
    await loadJobs(channelNames);
  } catch (error) {
    setMessage(document.getElementById("jobs-message"), error.detail);
  }

  document.getElementById("logout").addEventListener("click", async () => {
    try {
      await api("/api/auth/logout", {method: "POST"});
    } finally {
      window.location.replace("/login.html");
    }
  });
}

function initOauthDone() {
  const result = document.getElementById("oauth-result");
  const error = new URLSearchParams(window.location.search).get("error");
  if (error) {
    result.className = "message";
    result.textContent = error;
  } else {
    result.className = "success";
    result.textContent = "YouTubeとの接続が完了しました。";
  }
}

const page = document.body.dataset.page;
if (page === "login") initLogin();
if (page === "totp-setup") initTotpSetup();
if (page === "dashboard") initDashboard();
if (page === "oauth-done") initOauthDone();
