/* Клиент сервисного ассистента: без фреймворков и сборки. */

const state = {
  meta: null,
  lastQuestion: "",
  pending: false,
};

const STATUS_LABEL = {
  answered: "ответ по документации",
  no_answer: "в документации ответа нет",
  no_source: "руководство не загружено",
  empty_base: "база пуста",
  clarify_model: "нужно уточнить модель",
  clarify_question: "уточните вопрос",
  out_of_scope: "вне базы документов",
  model_mismatch: "данные по другой модели",
};

const EXAMPLES = [
  "Через сколько моточасов проводится ТО-2 на ПД5,0?",
  "Какое моторное масло применять на ПД2,0?",
  "Как часто подтягивать гайки крепления колёс?",
  "Что проверить на погрузчике перед началом смены?",
  "Горит контрольная лампа температуры гидрожидкости трансмиссии — возможные причины?",
  "Как поднять погрузчик домкратом и какой домкрат нужен?",
  "Допустимо ли буксировать погрузчик, на какое расстояние и с какой скоростью?",
  "Как обслуживать тяговую аккумуляторную батарею и где её разрешено заряжать?",
  "В каком порядке выполняется замена масла в двигателе?",
  "Какой момент затяжки болтов головки блока цилиндра по документации изготовителя двигателя?",
];

const messages = document.getElementById("messages");
const form = document.getElementById("chat-form");
const input = document.getElementById("question");
const sendButton = document.getElementById("send");
const modelSelect = document.getElementById("model-select");

/* ------------------------------------------------------------- утилиты */

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function renderInline(text) {
  return escapeHtml(text)
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/`([^`]+)`/g, "<code>$1</code>");
}

function renderTable(block) {
  const rows = block
    .map((line) => line.replace(/^\|/, "").replace(/\|$/, "").split("|").map((cell) => cell.trim()))
    .filter((cells) => !cells.every((cell) => /^:?-{2,}:?$/.test(cell) || cell === ""));
  if (!rows.length) return "";
  const head = rows[0];
  const body = rows.slice(1);
  const thead = `<thead><tr>${head.map((cell) => `<th>${renderInline(cell)}</th>`).join("")}</tr></thead>`;
  const tbody = body.length
    ? `<tbody>${body
        .map((cells) => `<tr>${cells.map((cell) => `<td>${renderInline(cell)}</td>`).join("")}</tr>`)
        .join("")}</tbody>`
    : "";
  return `<table>${thead}${tbody}</table>`;
}

function renderMarkdown(markdown) {
  const lines = String(markdown || "").split("\n");
  const out = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    const trimmed = line.trim();

    if (trimmed.startsWith("|")) {
      const block = [];
      while (i < lines.length && lines[i].trim().startsWith("|")) {
        block.push(lines[i].trim());
        i += 1;
      }
      out.push(renderTable(block));
      continue;
    }

    if (trimmed.startsWith(">")) {
      const quote = [];
      while (i < lines.length && lines[i].trim().startsWith(">")) {
        quote.push(lines[i].trim().replace(/^>\s?/, ""));
        i += 1;
      }
      out.push(`<blockquote>${renderInline(quote.join("\n"))}</blockquote>`);
      continue;
    }

    if (!trimmed) {
      i += 1;
      continue;
    }

    if (/^[-*]\s+/.test(trimmed)) {
      const items = [];
      while (i < lines.length && /^[-*]\s+/.test(lines[i].trim())) {
        items.push(lines[i].trim().replace(/^[-*]\s+/, ""));
        i += 1;
      }
      out.push(`<ul>${items.map((item) => `<li>${renderInline(item)}</li>`).join("")}</ul>`);
      continue;
    }

    if (/^\d+[.)]\s+/.test(trimmed)) {
      const items = [];
      while (i < lines.length && /^\d+[.)]\s+/.test(lines[i].trim())) {
        items.push(lines[i].trim().replace(/^\d+[.)]\s+/, ""));
        i += 1;
      }
      out.push(`<ol>${items.map((item) => `<li>${renderInline(item)}</li>`).join("")}</ol>`);
      continue;
    }

    const paragraph = [];
    while (i < lines.length) {
      const current = lines[i].trim();
      if (!current || current.startsWith("|") || current.startsWith(">")) break;
      if (/^[-*]\s+/.test(current) || /^\d+[.)]\s+/.test(current)) break;
      paragraph.push(current);
      i += 1;
    }
    out.push(`<p>${renderInline(paragraph.join(" "))}</p>`);
  }
  return out.join("\n");
}

/* ------------------------------------------------------------- отрисовка */

function statusBadge(status) {
  const label = STATUS_LABEL[status] || status;
  return `<span class="status-line status-${escapeHtml(status)}">${escapeHtml(label)}</span>`;
}

function sourceBlock(citation) {
  const location = [];
  if (citation.section_no) location.push(`раздел ${citation.section_no}`);
  if (citation.section_title) location.push(`«${citation.section_title}»`);
  if (citation.page) location.push(`стр. ${citation.page}`);
  const meta = location.join(" · ") || "раздел не определён";
  const quote = citation.quote ? renderInline(citation.quote) : "";
  return `
    <div class="source">
      <div class="source-title">${escapeHtml(citation.doc_title || "Документ")}</div>
      <div class="source-meta">${escapeHtml(meta)}</div>
      ${quote ? `<details><summary>Показать цитату</summary><blockquote>${quote}</blockquote></details>` : ""}
    </div>`;
}

function warningsBlock(warnings) {
  if (!warnings || !warnings.length) return "";
  const items = warnings.map((item) => `<p>${escapeHtml(item)}</p>`).join("");
  return `<div class="warnings"><h4>Предупреждения по операции</h4>${items}</div>`;
}

function relatedBlock(related) {
  if (!related || !related.length) return "";
  const items = related
    .map((item) => {
      const section = item.section_no ? `раздел ${item.section_no}` : "раздел не определён";
      const title = item.section_title ? ` «${item.section_title}»` : "";
      return `<li>${escapeHtml(item.doc_title)} — ${escapeHtml(section + title)}, стр. ${item.page}</li>`;
    })
    .join("");
  return `<div class="related"><h4>Рядом по теме</h4><ul>${items}</ul></div>`;
}

function chipsBlock(payload) {
  if (payload.status !== "clarify_model" || !Array.isArray(payload.model_options)) return "";
  const chips = payload.model_options
    .map((model) => `<button type="button" class="chip" data-model="${escapeHtml(model)}">${escapeHtml(model)}</button>`)
    .join("");
  return `<div class="chips">${chips}</div>`;
}

function addMessage(role, html) {
  const article = document.createElement("article");
  article.className = `message message-${role}`;
  const author = role === "user" ? "Вы" : "Ассистент";
  const avatar = role === "user" ? "Я" : "С";
  article.innerHTML = `
    <div class="message-head"><span class="avatar">${avatar}</span><span class="author">${author}</span></div>
    <div class="message-body">${html}</div>`;
  messages.appendChild(article);
  messages.scrollIntoView({ behavior: "smooth", block: "end" });
  return article;
}

function addPending() {
  return addMessage(
    "bot",
    `<div class="typing" role="status" aria-label="Идёт поиск по документации">
       <span></span><span></span><span></span>
     </div>`
  );
}

function renderAnswer(payload) {
  const parts = [statusBadge(payload.status), `<div>${renderMarkdown(payload.answer)}</div>`];
  parts.push(warningsBlock(payload.warnings));
  if (payload.citations && payload.citations.length) {
    parts.push(
      `<div class="sources"><h4>Источники</h4>${payload.citations.map(sourceBlock).join("")}</div>`
    );
  }
  if (payload.status === "no_answer") parts.push(relatedBlock(payload.related));
  parts.push(chipsBlock(payload));
  addMessage("bot", parts.join(""));
}

function showError(text) {
  addMessage(
    "bot",
    `${statusBadge("no_answer")}<div><p>${escapeHtml(text)}</p></div>`
  );
}

/* ------------------------------------------------------------- сеть */

async function ask(question, model) {
  if (state.pending) return;
  state.pending = true;
  state.lastQuestion = question;
  sendButton.disabled = true;
  addMessage("user", `<div><p>${escapeHtml(question)}</p></div>`);
  const pending = addPending();
  try {
    const response = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: question, model: model || null }),
    });
    const data = await response.json();
    pending.remove();
    if (!response.ok) {
      showError(data.message || `Ошибка сервиса (${response.status})`);
    } else {
      renderAnswer(data);
    }
  } catch (error) {
    pending.remove();
    showError(`Не удалось связаться с сервисом: ${error.message}`);
  } finally {
    state.pending = false;
    sendButton.disabled = false;
    input.focus();
  }
}

/* ------------------------------------------------------------- метаданные */

function renderDocuments(meta) {
  const container = document.getElementById("documents");
  const documents = meta.documents || [];
  if (!documents.length) {
    container.innerHTML = `<p class="muted">Документы не загружены.</p>`;
  } else {
    container.innerHTML = documents
      .map(
        (doc) => `
        <div class="doc">
          <div class="doc-title">${escapeHtml(doc.title)}</div>
          <div class="doc-meta">
            ${doc.pages} стр. · ${doc.sections} разд. · ${doc.chunks} фрагм.
            ${doc.version ? `· версия ${escapeHtml(doc.version)}` : ""}
          </div>
        </div>`
      )
      .join("");
  }
  const stats = meta.stats || {};
  document.getElementById("stats").textContent =
    `Фрагментов: ${stats.chunks || 0}, таблиц: ${stats.tables || 0}, процедур: ${stats.procedures || 0}`;
}

function renderMeta(meta) {
  document.getElementById("app-name").textContent = meta.app_name;
  document.getElementById("app-subtitle").textContent = meta.app_subtitle;
  document.title = meta.app_name;

  modelSelect.innerHTML =
    `<option value="">— уточнить в вопросе —</option>` +
    (meta.models || []).map((model) => `<option value="${escapeHtml(model)}">${escapeHtml(model)}</option>`).join("");

  renderDocuments(meta);

  if (!meta.documents || !meta.documents.length) {
    document.getElementById("empty-banner").classList.remove("hidden");
  }

  const examples = document.getElementById("examples");
  examples.innerHTML = "";
  EXAMPLES.forEach((text) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "example";
    button.textContent = text;
    button.addEventListener("click", () => {
      input.value = text;
      input.focus();
    });
    examples.appendChild(button);
  });
}

async function loadMeta() {
  try {
    const response = await fetch("/api/meta");
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    state.meta = await response.json();
    renderMeta(state.meta);
  } catch (error) {
    document.getElementById("documents").innerHTML =
      `<p class="muted">Не удалось получить состояние базы: ${escapeHtml(error.message)}</p>`;
  }
}

/* ------------------------------------------------------------- события */

form.addEventListener("submit", (event) => {
  event.preventDefault();
  const question = input.value.trim();
  if (!question) return;
  input.value = "";
  ask(question, modelSelect.value);
});

input.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    form.requestSubmit();
  }
});

messages.addEventListener("click", (event) => {
  const chip = event.target.closest(".chip");
  if (!chip) return;
  const model = chip.dataset.model;
  modelSelect.value = model;
  const question = state.lastQuestion;
  if (question) ask(question, model);
});

loadMeta();
