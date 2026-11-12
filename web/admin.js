/* Админ-панель: правка config/service.json из браузера. */

const state = {
  config: null,
  models: [],
  jsonDirty: false,
  readOnly: false,
};

const el = {
  status: document.getElementById("status"),
  accessBanner: document.getElementById("access-banner"),
  appName: document.getElementById("app-name"),
  appSubtitle: document.getElementById("app-subtitle"),
  phone: document.getElementById("contact-phone"),
  email: document.getElementById("contact-email"),
  site: document.getElementById("contact-site"),
  note: document.getElementById("contact-note"),
  minCoverage: document.getElementById("th-coverage"),
  minScore: document.getElementById("th-score"),
  minTerms: document.getElementById("th-terms"),
  topK: document.getElementById("th-topk"),
  candidates: document.getElementById("th-candidates"),
  parenthetical: document.getElementById("parenthetical"),
  llmEnabled: document.getElementById("llm-enabled"),
  llmBase: document.getElementById("llm-base"),
  llmModel: document.getElementById("llm-model"),
  llmTemperature: document.getElementById("llm-temperature"),
  llmTimeout: document.getElementById("llm-timeout"),
  llmKeyState: document.getElementById("llm-key-state"),
  json: document.getElementById("json-editor"),
  path: document.getElementById("config-path"),
  save: document.getElementById("save"),
  reset: document.getElementById("reset"),
  loadJson: document.getElementById("load-json"),
  downloadJson: document.getElementById("download-json"),
  adminToken: document.getElementById("admin-token"),
  applyToken: document.getElementById("apply-token"),
};

const formInputs = [
  el.appName, el.appSubtitle, el.phone, el.email, el.site, el.note,
  el.minCoverage, el.minScore, el.minTerms, el.topK, el.candidates,
  el.parenthetical, el.llmEnabled, el.llmBase, el.llmModel,
  el.llmTemperature, el.llmTimeout,
];

/* --------------------------------------------------------------- утилиты */

function setStatus(kind, title, lines) {
  el.status.className = `admin-status ${kind}`;
  el.status.textContent = "";
  const heading = document.createElement("strong");
  heading.textContent = title;
  el.status.appendChild(heading);
  (lines || []).forEach((line) => {
    const item = document.createElement("p");
    item.textContent = line;
    el.status.appendChild(item);
  });
}

function hideStatus() {
  el.status.className = "admin-status hidden";
  el.status.textContent = "";
}

function numberValue(input, label, errors, { integer = false } = {}) {
  const raw = String(input.value || "").trim().replace(",", ".");
  if (!raw) {
    errors.push(`${label}: заполните значение`);
    return null;
  }
  const value = integer ? Number.parseInt(raw, 10) : Number.parseFloat(raw);
  if (Number.isNaN(value)) {
    errors.push(`${label}: ожидается число`);
    return null;
  }
  return value;
}

const TOKEN_KEY = "silant-admin-token";

function storedToken() {
  try {
    return localStorage.getItem(TOKEN_KEY) || "";
  } catch (error) {
    // Приватный режим браузера: работаем без сохранения токена.
    return "";
  }
}

function storeToken(token) {
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token);
    else localStorage.removeItem(TOKEN_KEY);
  } catch (error) {
    // Хранилище недоступно — токен будет действовать до перезагрузки страницы.
  }
}

/* --------------------------------------------------------------- форма */

function fillForm() {
  const config = state.config;
  const contacts = config.service_contacts || {};
  const thresholds = config.thresholds || {};
  const llm = config.llm || {};

  el.appName.value = config.app_name || "";
  el.appSubtitle.value = config.app_subtitle || "";
  el.phone.value = contacts.phone || "";
  el.email.value = contacts.email || "";
  el.site.value = contacts.site || "";
  el.note.value = contacts.note || "";

  el.minCoverage.value = thresholds.min_coverage ?? 0.42;
  el.minScore.value = thresholds.min_score ?? 0.3;
  el.minTerms.value = thresholds.min_matched_terms ?? 2;
  el.topK.value = thresholds.top_k ?? 8;
  el.candidates.value = thresholds.candidate_limit ?? 60;

  el.llmEnabled.checked = Boolean(llm.enabled);
  el.llmBase.value = llm.base_url || "";
  el.llmModel.value = llm.model || "";
  el.llmTemperature.value = llm.temperature ?? 0;
  el.llmTimeout.value = llm.timeout_seconds ?? 40;

  const rules = config.model_rules || {};
  el.parenthetical.value = rules.parenthetical_exception_model || "";

  el.llmKeyState.textContent = state.llmKeyPresent
    ? "Ключ найден в окружении."
    : "Ключ в окружении не найден.";

  el.path.textContent = `Файл настроек: ${state.path || "config/service.json"}`;
  state.jsonDirty = false;
  syncJson();
}

function collect() {
  const errors = [];
  const config = JSON.parse(JSON.stringify(state.config));

  config.app_name = el.appName.value.trim();
  config.app_subtitle = el.appSubtitle.value.trim();
  config.service_contacts = {
    ...(config.service_contacts || {}),
    phone: el.phone.value.trim(),
    email: el.email.value.trim(),
    site: el.site.value.trim(),
    note: el.note.value.trim(),
  };

  const thresholds = { ...(config.thresholds || {}) };
  const values = {
    min_coverage: numberValue(el.minCoverage, "min_coverage", errors),
    min_score: numberValue(el.minScore, "min_score", errors),
    min_matched_terms: numberValue(el.minTerms, "min_matched_terms", errors, { integer: true }),
    top_k: numberValue(el.topK, "top_k", errors, { integer: true }),
    candidate_limit: numberValue(el.candidates, "candidate_limit", errors, { integer: true }),
  };
  Object.entries(values).forEach(([key, value]) => {
    if (value !== null) thresholds[key] = value;
  });
  config.thresholds = thresholds;

  config.model_rules = {
    ...(config.model_rules || {}),
    parenthetical_exception_model: el.parenthetical.value,
  };

  config.llm = {
    ...(config.llm || {}),
    enabled: el.llmEnabled.checked,
    base_url: el.llmBase.value.trim(),
    model: el.llmModel.value.trim(),
    temperature: numberValue(el.llmTemperature, "llm.temperature", errors),
    timeout_seconds: numberValue(el.llmTimeout, "llm.timeout_seconds", errors, { integer: true }),
  };

  return { config, errors };
}

function syncJson() {
  if (state.jsonDirty || !state.config) return;
  const { config, errors } = collect();
  if (errors.length) return;
  el.json.value = JSON.stringify(config, null, 2);
}

/* --------------------------------------------------------------- сеть */

async function request(url, options) {
  const headers = { "Content-Type": "application/json" };
  const token = storedToken();
  if (token) headers["X-Admin-Token"] = token;
  const response = await fetch(url, {
    ...options,
    headers: { ...headers, ...((options && options.headers) || {}) },
  });
  let payload = {};
  try {
    payload = await response.json();
  } catch (error) {
    payload = { status: "error", message: `Некорректный ответ сервера (${response.status})` };
  }
  return { ok: response.ok, status: response.status, payload };
}

async function loadConfig() {
  const { ok, status, payload } = await request("/api/admin/config");
  if (status === 403) {
    state.readOnly = true;
    el.accessBanner.classList.remove("hidden");
    setStatus("error", "Нет доступа к настройкам", [
      "Откройте страницу на самом сервере (127.0.0.1) или передайте X-Admin-Token.",
    ]);
    el.save.disabled = true;
    el.reset.disabled = true;
    return;
  }
  if (!ok) {
    setStatus("error", "Не удалось прочитать настройки", [payload.message || "Ошибка сервера"]);
    return;
  }
  state.config = payload.config;
  state.models = payload.models || [];
  state.path = payload.path;
  state.llmKeyPresent = payload.llm_key_present;
  state.readOnly = false;
  el.save.disabled = false;
  el.reset.disabled = false;

  el.parenthetical.innerHTML = "";
  const empty = document.createElement("option");
  empty.value = "";
  empty.textContent = "— нет —";
  el.parenthetical.appendChild(empty);
  state.models.forEach((model) => {
    const option = document.createElement("option");
    option.value = model;
    option.textContent = model;
    el.parenthetical.appendChild(option);
  });

  fillForm();
  if (payload.warnings && payload.warnings.length) {
    setStatus("warn", "Настройки загружены, есть замечания", payload.warnings);
  } else {
    hideStatus();
  }
}

async function saveConfig(payloadConfig) {
  el.save.disabled = true;
  try {
    const { ok, payload } = await request("/api/admin/config", {
      method: "POST",
      body: JSON.stringify({ config: payloadConfig }),
    });
    if (ok && payload.saved) {
      state.config = payload.config;
      state.jsonDirty = false;
      fillForm();
      const warnings = payload.warnings || [];
      if (warnings.length) {
        setStatus("warn", "Сохранено, но есть замечания", warnings);
      } else {
        setStatus("ok", "Настройки сохранены", [
          "Изменения применены сразу: перезапуск сервиса не нужен.",
        ]);
      }
      return;
    }
    const errors = payload.errors || (payload.message ? [payload.message] : ["Неизвестная ошибка"]);
    setStatus("error", "Настройки не сохранены", errors);
  } catch (error) {
    setStatus("error", "Сервер недоступен", [String(error)]);
  } finally {
    el.save.disabled = state.readOnly;
  }
}

/* --------------------------------------------------------------- события */

formInputs.forEach((input) => {
  input.addEventListener("input", syncJson);
  input.addEventListener("change", syncJson);
});

el.json.addEventListener("input", () => {
  state.jsonDirty = true;
});

el.save.addEventListener("click", () => {
  let config;
  if (state.jsonDirty) {
    try {
      config = JSON.parse(el.json.value);
    } catch (error) {
      setStatus("error", "В JSON редактора есть синтаксическая ошибка", [String(error)]);
      return;
    }
  } else {
    const collected = collect();
    if (collected.errors.length) {
      setStatus("error", "Проверьте поля", collected.errors);
      return;
    }
    config = collected.config;
  }
  saveConfig(config);
});

el.reset.addEventListener("click", () => {
  if (!state.config) return;
  fillForm();
  hideStatus();
});

el.loadJson.addEventListener("click", async () => {
  await loadConfig();
});

el.downloadJson.addEventListener("click", () => {
  const blob = new Blob([el.json.value], { type: "application/json;charset=utf-8" });
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = "service.json";
  link.click();
  URL.revokeObjectURL(link.href);
});

el.applyToken.addEventListener("click", async () => {
  storeToken(el.adminToken.value.trim());
  state.readOnly = false;
  el.accessBanner.classList.add("hidden");
  await loadConfig();
});

if (storedToken()) el.adminToken.value = storedToken();

loadConfig();
