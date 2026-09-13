/* Supply Chain Assistant — browser client.
 *
 * Deliberately plain: no framework, no bundler, no build step. The whole file
 * is readable top to bottom, which matters more in a teaching repository than
 * saving a few lines with a library.
 *
 * Two identifiers drive the agent's memory behaviour:
 *
 *   actorId    stable per browser. Long-term memories are filed under it, so
 *              a preference stated today is recalled in a session created
 *              next week.
 *   sessionId  one conversation. Starting a new session proves recall is
 *              coming from AgentCore Memory rather than from chat history.
 *
 * Both live in localStorage. A real deployment would take the actor from an
 * authenticated user rather than trusting the browser.
 */

const CONFIG = window.APP_CONFIG || {};
const API_BASE = (CONFIG.apiEndpoint || "").replace(/\/$/, "");

const STORAGE_KEYS = {
  actor: "supplychain.actorId",
  sessions: "supplychain.sessions",
  current: "supplychain.currentSession",
};

const elements = {
  messages: document.getElementById("messages"),
  form: document.getElementById("composer"),
  input: document.getElementById("input"),
  send: document.getElementById("send"),
  sessionList: document.getElementById("session-list"),
  newSession: document.getElementById("new-session"),
  sessionLabel: document.getElementById("session-label"),
  actorLabel: document.getElementById("actor-label"),
  capabilityList: document.getElementById("capability-list"),
};

/* ------------------------------------------------------------- persistence */

function readJson(key, fallback) {
  try {
    const raw = localStorage.getItem(key);
    return raw ? JSON.parse(raw) : fallback;
  } catch {
    // Private browsing and blocked site data both throw here. The app should
    // still work, just without remembering anything between reloads.
    return fallback;
  }
}

function writeJson(key, value) {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    /* ignore — storage is a convenience, not a requirement */
  }
}

function getActorId() {
  let actorId = localStorage.getItem(STORAGE_KEYS.actor);
  if (!actorId) {
    actorId = `user-${Math.random().toString(36).slice(2, 10)}`;
    try {
      localStorage.setItem(STORAGE_KEYS.actor, actorId);
    } catch {
      /* ignore */
    }
  }
  return actorId;
}

/* ----------------------------------------------------------------- state */

const actorId = getActorId();
let sessions = readJson(STORAGE_KEYS.sessions, []);
let currentSessionId = localStorage.getItem(STORAGE_KEYS.current) || null;

function currentSession() {
  return sessions.find((session) => session.id === currentSessionId);
}

function createSession() {
  const session = {
    // AgentCore requires runtime session ids of at least 33 characters. The
    // chat handler pads short ones, but generating a long id here keeps the
    // browser id and the runtime id identical, which makes logs easier to
    // follow. crypto.randomUUID is 36 characters on its own.
    id: `session-${crypto.randomUUID()}`,
    title: "New conversation",
    createdAt: new Date().toISOString(),
    messages: [],
  };
  sessions.unshift(session);
  currentSessionId = session.id;
  persist();
  renderSessions();
  renderMessages();
  return session;
}

function persist() {
  // Keep only the ten most recent conversations; localStorage is small and
  // old transcripts are not worth the quota.
  sessions = sessions.slice(0, 10);
  writeJson(STORAGE_KEYS.sessions, sessions);
  try {
    localStorage.setItem(STORAGE_KEYS.current, currentSessionId);
  } catch {
    /* ignore */
  }
}

/* --------------------------------------------------------------- rendering */

function renderCapabilities() {
  const features = CONFIG.features || {};
  const labels = [
    ["oauth", "OAuth2 (Cognito)"],
    ["gateway", "Gateway tools"],
    ["knowledgeBase", "Knowledge base"],
    ["memory", "Memory"],
    ["guardrails", "Guardrails"],
    ["vpc", "VPC networking"],
  ];

  elements.capabilityList.innerHTML = labels
    .map(([key, label]) => {
      const on = Boolean(features[key]);
      return `<li><span>${label}</span><span class="badge ${
        on ? "on" : "off"
      }">${on ? "on" : "off"}</span></li>`;
    })
    .join("");
}

function renderSessions() {
  elements.sessionList.innerHTML = sessions
    .map((session) => {
      const active = session.id === currentSessionId ? "active" : "";
      const time = new Date(session.createdAt).toLocaleString();
      return `<li class="${active}" data-id="${session.id}">
        ${escapeHtml(session.title)}
        <span class="session-time">${time}</span>
      </li>`;
    })
    .join("");

  elements.sessionLabel.textContent = currentSession()
    ? `Session started ${new Date(currentSession().createdAt).toLocaleString()}`
    : "";
  elements.actorLabel.textContent = `Actor: ${actorId}`;
}

function renderMessages() {
  const session = currentSession();
  const history = session ? session.messages : [];

  if (history.length === 0) {
    elements.messages.innerHTML = `
      <div class="message assistant">
        <div class="bubble">Ask about inventory, suppliers, shipments, quality, or supply chain procedures.</div>
      </div>`;
    return;
  }

  elements.messages.innerHTML = history
    .map(
      (message) => `<div class="message ${message.role}">
        <div class="bubble">${escapeHtml(message.text)}</div>
      </div>`
    )
    .join("");

  elements.messages.scrollTop = elements.messages.scrollHeight;
}

function appendMessage(role, text) {
  const session = currentSession() || createSession();
  session.messages.push({ role, text });

  // Name the conversation after its opening question, the way most chat
  // applications do.
  if (role === "user" && session.messages.length === 1) {
    session.title = text.length > 38 ? `${text.slice(0, 38)}…` : text;
  }

  persist();
  renderSessions();
  renderMessages();
}

function showTyping() {
  const node = document.createElement("div");
  node.className = "message assistant";
  node.id = "typing";
  node.innerHTML =
    '<div class="bubble typing"><span></span><span></span><span></span></div>';
  elements.messages.appendChild(node);
  elements.messages.scrollTop = elements.messages.scrollHeight;
}

function hideTyping() {
  document.getElementById("typing")?.remove();
}

function escapeHtml(value) {
  const div = document.createElement("div");
  div.textContent = value;
  return div.innerHTML;
}

/* -------------------------------------------------------------- networking */

async function sendMessage(text) {
  appendMessage("user", text);
  showTyping();
  elements.send.disabled = true;

  try {
    const response = await fetch(`${API_BASE}/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        message: text,
        session_id: currentSessionId,
        actor_id: actorId,
      }),
    });

    const payload = await response.json().catch(() => ({}));
    hideTyping();

    if (!response.ok) {
      appendMessage("error", payload.error || `Request failed (${response.status}).`);
      return;
    }

    appendMessage("assistant", payload.reply || "(empty response)");
  } catch (error) {
    hideTyping();
    // A timeout here is usually the 30 second API Gateway integration limit,
    // which is a known ceiling of this design rather than a transient fault.
    appendMessage(
      "error",
      `Could not reach the assistant: ${error.message}. If the question was complex, the 30 second API timeout may have been hit.`
    );
  } finally {
    elements.send.disabled = false;
    elements.input.focus();
  }
}

/* ------------------------------------------------------------------ events */

elements.form.addEventListener("submit", (event) => {
  event.preventDefault();
  const text = elements.input.value.trim();
  if (!text) return;
  elements.input.value = "";
  sendMessage(text);
});

elements.newSession.addEventListener("click", () => {
  createSession();
  elements.input.focus();
});

elements.sessionList.addEventListener("click", (event) => {
  const item = event.target.closest("li[data-id]");
  if (!item) return;
  currentSessionId = item.dataset.id;
  persist();
  renderSessions();
  renderMessages();
});

document.querySelectorAll(".chip").forEach((chip) => {
  chip.addEventListener("click", () => {
    elements.input.value = chip.dataset.prompt;
    elements.input.focus();
  });
});

/* ------------------------------------------------------------------- start */

if (!API_BASE) {
  elements.messages.innerHTML = `
    <div class="message error">
      <div class="bubble">config.js is missing or has no apiEndpoint. Run <code>make frontend</code> to generate it from the CloudFormation stack outputs.</div>
    </div>`;
  elements.send.disabled = true;
} else {
  if (sessions.length === 0 || !currentSession()) {
    createSession();
  }
  renderCapabilities();
  renderSessions();
  renderMessages();
}
