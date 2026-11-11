// OmaGTD web — a second front-end over the same commands the desktop runs.
//
// It holds no model of its own: every screen is the JSON from `gtd state`,
// `gtd show` and friends, and every button is one command. That is what keeps
// the phone and the desktop from disagreeing about what GTD means.

const $ = (selector) => document.querySelector(selector);

const GLYPH = {
  inbox: "·", active: " ", waiting: "W", someday: "S", reference: "R",
  onhold: "‖", done: "✓", dropped: "×", archived: "▣", trashed: "⌫",
};

const TITLES = {
  inbox: "Inbox", calendar: "Calendar", overdue: "Overdue",
  today_open: "Today", upcoming: "Upcoming", projects: "Projects",
  contexts: "Contexts", review: "Review", waiting: "Waiting",
  someday: "Someday", focus: "Next", clarify: "Clarify",
  flagged: "Flagged", reference: "Reference", onhold: "On hold",
  archived: "Archive", done: "Completed", tasks: "Tasks",
};

// The tab bar is the set the fullscreen app puts on one tap (inbox, calendar,
// today, forecast, plus the overdue list); everything else sits behind More.
const PRIMARY = ["inbox", "calendar", "overdue", "today_open", "upcoming"];

const MORE_VIEWS = [
  ["review", "Review"], ["waiting", "Waiting"], ["someday", "Someday"],
  ["projects", "Projects"], ["contexts", "Contexts"],
];

const MONTHS = ["January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December"];

const app = {
  password: localStorage.getItem("omagtd.password") || "",
  view: "inbox",
  project: null,
  projectTitle: "",
  rev: -1,
  events: null,
  refreshTimer: null,
  pollTimer: null,
  // Every refresh takes a number; a slow response whose view has since been
  // replaced is dropped instead of painting over the newer screen.
  seq: 0,
  // One press, one attempt: Enter and the button must not race each other.
  unlocking: false,
  // The calendar keeps its month and selected day, like the fullscreen
  // app's calendar page.
  calendarMonth: monthKey(new Date()),
  calDay: "",
  // A context drill-down filters the list the same way a project does, with
  // the key back to the view the drill-down was opened from.
  tag: null,
  tagTitle: "",
  backView: "projects",
  // False until the person picks a screen: only then is the opening view a
  // choice rather than the setting.
  viewChosen: false,
};

class ApiError extends Error {
  constructor(message, payload) {
    super(message);
    this.payload = payload || {};
  }
}

// ------------------------------------------------------------------- api

async function api(argv) {
  const response = await fetch("/rpc", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "Authorization": "Bearer " + app.password,
    },
    body: JSON.stringify({ argv }),
  });

  let payload;
  try {
    payload = await response.json();
  } catch {
    payload = { ok: false, error: `HTTP ${response.status}` };
  }

  if (response.status === 401) {
    // The stored secret is no good any more. Dropping it stops the page (and
    // a password manager) from retrying the old value forever.
    app.password = "";
    localStorage.removeItem("omagtd.password");
    lock("That password was not accepted.");
    throw new ApiError("password", payload);
  }
  if (!response.ok || payload.ok === false) {
    throw new ApiError(payload.error || `HTTP ${response.status}`, payload);
  }
  return payload;
}

// ------------------------------------------------------------ formatting

function startOfDay(date) {
  return new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime();
}

// Local calendar keys, the shape the CLI takes and gives back.
function two(n) {
  return n < 10 ? "0" + n : String(n);
}

function dateKey(date) {
  return date.getFullYear() + "-" + two(date.getMonth() + 1) + "-" + two(date.getDate());
}

function monthKey(date) {
  return date.getFullYear() + "-" + two(date.getMonth() + 1);
}

function todayStamp() {
  return dateKey(new Date());
}

function stamp(value) {
  if (!value) return null;
  const parsed = new Date(String(value).replace(" ", "T"));
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

function humanDate(value) {
  const date = stamp(value);
  if (!date) return "";
  const days = Math.round((startOfDay(date) - startOfDay(new Date())) / 86400000);

  let label;
  if (days === 0) label = "today";
  else if (days === 1) label = "tomorrow";
  else if (days === -1) label = "yesterday";
  else if (days < 0) label = `${-days}d late`;
  else if (days < 7) label = date.toLocaleDateString(undefined, { weekday: "short" });
  else label = date.toLocaleDateString(undefined, { day: "numeric", month: "short" });

  const hours = date.getHours();
  const minutes = date.getMinutes();
  if (hours || minutes) {
    label += " " + String(hours).padStart(2, "0") + ":" + String(minutes).padStart(2, "0");
  }
  return label;
}

function isLate(value) {
  const date = stamp(value);
  return date !== null && date.getTime() < Date.now();
}

// The fullscreen app's colour decisions, in its order (colorKeyFor): closed
// items keep the foreground, someday is pink, waiting past its deadline is
// purple, past deadline is red, waiting is blue, a future defer is grey, a
// flag is orange. The page's variables already hold the desktop's live
// values, so the web rows paint exactly like the fullscreen rows.
function isClosed(item) {
  return ["done", "dropped", "trashed", "archived"].includes(item.status);
}

function isDeferred(item) {
  if (!item.defer_at || isClosed(item)) return false;
  const when = stamp(item.defer_at);
  return when !== null && when.getTime() > Date.now();
}

function colorKey(item) {
  if (!item || isClosed(item)) return "plain";
  if (item.status === "someday") return "pink";
  const waiting = item.status === "waiting";
  if (waiting && isLate(item.due_at)) return "purple";
  if (isLate(item.due_at)) return "red";
  if (waiting) return "blue";
  if (isDeferred(item)) return "grey";
  if (item.priority > 0) return "orange";
  return "plain";
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function button(label, className, onClick) {
  const node = el("button", className, label);
  node.type = "button";
  node.addEventListener("click", onClick);
  return node;
}

// ---------------------------------------------------------------- toast

let toastTimer = null;
function toast(message, bad = false) {
  const node = $("#toast");
  node.textContent = message;
  node.classList.toggle("bad", bad);
  node.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { node.hidden = true; }, 2600);
}

function markSync(state) {
  const node = $("#sync");
  node.className = "sync " + state;
  node.title = { live: "connected", stale: "reconnecting", off: "offline" }[state] || state;
}

// ----------------------------------------------------------------- lock

// The web app opens where the desktop app opens: the same fullscreen default
// view, read out of the same settings row. "today" is this app's today_open,
// and anything else keeps the plain Inbox default.
async function preferredStartView() {
  try {
    const payload = await api(["state"]);
    const want = (payload.settings || {}).fullscreen_default_view;
    return { inbox: "inbox", calendar: "calendar", today: "today_open" }[want] || "inbox";
  } catch (error) {
    if (error.message !== "password") console.warn("start view:", error.message);
    return "inbox";
  }
}

// The two screens take turns. `hidden` is honoured by the stylesheet, but a
// phone that still has an older copy of it (offline cache) served `.lock` and
// `.app` with `display` of their own, which beat `[hidden]`: the lock stayed
// painted over the app and the button looked dead. The inline style is the
// one thing a stale stylesheet cannot override.
function setScreen(name) {
  const locked = name === "lock";
  const panel = $("#app");
  const gate = $("#lock");
  panel.hidden = locked;
  panel.style.display = locked ? "none" : "";
  gate.hidden = !locked;
  gate.style.display = locked ? "" : "none";
}

function lock(message) {
  stop();
  setScreen("lock");
  $("#lock-error").hidden = !message;
  if (message) {
    $("#lock-error").textContent = message;
    // A rejected value should not sit in the field for the next attempt.
    $("#password").value = "";
  }
  setTimeout(() => $("#password").focus(), 30);
}

// `announce` is true only for a press. A password remembered on this device
// reconnecting on load goes through the same check without ringing the
// desktop, so a reload is not mistaken for someone trying the door.
async function unlock(password, { announce = false } = {}) {
  if (app.unlocking) return;
  app.unlocking = true;

  const button = $("#lock-form .primary");
  const label = button.textContent;
  button.disabled = true;
  button.textContent = "Checking…";
  $("#lock-error").hidden = true;

  try {
    const response = await fetch("/v1/unlock", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password, announce }),
    });

    let payload = {};
    try { payload = await response.json(); } catch { /* not JSON */ }

    if (response.status === 401) {
      // Forget the rejected secret on this device: a stale value in storage
      // (or a password manager) would otherwise retry it on every load.
      app.password = "";
      localStorage.removeItem("omagtd.password");
      lock("That password was not accepted.");
      return;
    }
    if (!response.ok || payload.ok === false) {
      lock(payload.error || `HTTP ${response.status}`);
      return;
    }

    app.password = password;
    localStorage.setItem("omagtd.password", password);
    // The web app opens where the app opens: the same "Opening" setting the
    // full-screen pane owns (Inbox, Calendar or Today). It is read once, on
    // the very first unlock; a later unlock after an expiry is a re-entry, not
    // a fresh choice.
    if (!app.viewChosen) {
      app.view = await preferredStartView() || app.view;
    }
    $("#lock-error").hidden = true;
    setScreen("app");
    start();
  } catch (error) {
    lock(error.message || "The server did not answer.");
  } finally {
    app.unlocking = false;
    button.disabled = false;
    button.textContent = label;
  }
}

// ---------------------------------------------------------------- start

function stop() {
  if (app.events) { app.events.close(); app.events = null; }
  clearInterval(app.pollTimer);
  app.pollTimer = null;
}

function start() {
  if (!app.password) { lock(); return; }
  stop();
  setView(app.view, { reload: false });
  refresh({ quiet: true });
  connect();
  app.pollTimer = setInterval(() => {
    if (!document.hidden) refresh({ quiet: true });
  }, 30000);
}

function connect() {
  if (!("EventSource" in window)) return;
  const events = new EventSource("/events?password=" + encodeURIComponent(app.password));
  app.events = events;

  events.onopen = () => markSync("live");
  events.onerror = () => markSync("stale");
  events.onmessage = (event) => {
    const rev = Number(event.data);
    if (!Number.isFinite(rev)) return;
    markSync("live");
    if (rev === app.rev) return;
    const first = app.rev === -1;
    app.rev = rev;
    if (!first) scheduleRefresh();
  };
}

function scheduleRefresh() {
  clearTimeout(app.refreshTimer);
  app.refreshTimer = setTimeout(() => refresh({ quiet: true }), 200);
}

// ---------------------------------------------------------------- views

function setView(view, { reload = true } = {}) {
  app.view = view;
  app.viewChosen = true;
  app.project = null;
  app.projectTitle = "";
  app.tag = null;
  app.tagTitle = "";
  app.calDay = "";
  document.querySelectorAll(".tabs button").forEach((node) => {
    node.classList.toggle("active", node.dataset.view === view);
  });
  $("#more-tab").classList.toggle("active", !PRIMARY.includes(view));
  $("#back").hidden = true;
  $("#view-title").textContent = TITLES[view] || view;
  $("#banner").hidden = true;
  if (reload) refresh({ quiet: true });
}

// A container's own screen: its tasks in the middle column, the view it came
// from remembered for the back arrow.
function openProject(id, title) {
  app.backView = "projects";
  app.viewChosen = true;
  app.project = id;
  app.projectTitle = title;
  app.view = "focus";
  document.querySelectorAll(".tabs button").forEach((node) => {
    node.classList.remove("active");
  });
  $("#more-tab").classList.add("active");
  $("#view-title").textContent = title;
  $("#back").hidden = false;
  refresh({ quiet: true });
}

function openContext(name) {
  app.backView = "contexts";
  app.viewChosen = true;
  app.tag = name;
  app.tagTitle = name;
  app.view = "tasks";
  document.querySelectorAll(".tabs button").forEach((node) => {
    node.classList.remove("active");
  });
  $("#more-tab").classList.add("active");
  $("#view-title").textContent = "#" + name;
  $("#back").hidden = false;
  refresh({ quiet: true });
}

function goBack() {
  app.project = null;
  app.projectTitle = "";
  app.tag = null;
  app.tagTitle = "";
  setView(app.backView || "projects");
}

function openMore() {
  sheetItem = null;
  $("#sheet-title").textContent = "More views";
  const body = $("#sheet-body");
  body.replaceChildren();

  const row = el("div", "actions");
  for (const [key, label] of MORE_VIEWS) {
    row.append(button(label, key === app.view ? "accent" : "", () => {
      hideSheet();
      setView(key);
    }));
  }
  body.append(row);
  showSheet();
}

// --------------------------------------------------------------- refresh

async function refresh({ quiet = false } = {}) {
  if (!app.password) return;
  const seq = ++app.seq;
  const view = app.view;
  const project = app.project;
  const tag = app.tag;
  try {
    if (view === "projects") {
      const payload = await api(["state"]);
      if (seq !== app.seq) return;
      renderTree(payload);
      $("#view-count").textContent = payload.stats ? `${payload.stats.projects_open ?? ""} open` : "";
    } else if (view === "contexts") {
      const payload = await api(["state"]);
      if (seq !== app.seq) return;
      renderContexts(payload.contexts || []);
      $("#view-count").textContent = "";
    } else if (view === "calendar") {
      const payload = await api(["calendar", "--month", app.calendarMonth, "--all"]);
      if (seq !== app.seq) return;
      renderCalendar(payload);
      $("#view-count").textContent = "";
    } else if (view === "review") {
      const payload = await api(["review", "weekly"]);
      if (seq !== app.seq) return;
      renderReview(payload);
      $("#view-count").textContent = "";
    } else {
      const argv = ["state", "--view", view, "--limit", "200"];
      if (project) argv.push("--project", project);
      if (tag) argv.push("--tag", tag);
      const payload = await api(argv);
      if (seq !== app.seq) return;
      const list = payload.view || { items: [], count: 0 };
      renderItems(list.items);
      $("#view-count").textContent = String(list.count ?? list.items.length);
    }
    $("#banner").hidden = true;
    markSync("live");
  } catch (error) {
    if (error.message === "password") return;
    if (seq !== app.seq) return;
    if (!quiet) toast(error.message, true);
    $("#banner").textContent = error.message;
    $("#banner").hidden = false;
    markSync("off");
  }
}

// ---------------------------------------------------------------- render

function renderItems(items) {
  const list = $("#list");
  list.hidden = false;
  $("#tree").hidden = true;
  list.replaceChildren();

  if (!items || !items.length) {
    list.append(el("p", "empty", "Nothing here."));
    return;
  }

  for (const item of items) {
    list.append(itemRow(item));
  }
}

function itemRow(item) {
  const key = colorKey(item);
  const row = el("div", `row status-${item.status}`
    + (key !== "plain" ? ` color-${key}` : "")
    + (item.priority ? " priority" : ""));

  const glyph = el("span", "glyph", GLYPH[item.status] || " ");

  const body = el("div", "body");
  const title = el("button", "title" + (item.status === "done" ? " done" : ""), item.title || "(untitled)");
  title.type = "button";
  title.addEventListener("click", () => openItem(item.id));
  body.append(title);

  const meta = el("div", "meta");
  if (item.parent_title) meta.append(el("span", "project", item.parent_title));
  if (item.due_at) {
    meta.append(el("span", isLate(item.due_at) ? "late" : "due", humanDate(item.due_at)));
  } else if (item.defer_at) {
    meta.append(el("span", "", "from " + humanDate(item.defer_at)));
  }
  for (const tag of item.tags || []) meta.append(el("span", "tag", "#" + tag));
  if (item.duration) meta.append(el("span", "", `${item.duration}m`));
  if (item.repeat) meta.append(el("span", "", "↻"));
  if (meta.childNodes.length) body.append(meta);

  const side = el("div", "side");
  if (item.priority) {
    const flag = button("!", "flag", () => run(["unflag", item.id], "Unflagged"));
    side.append(flag);
  }
  if (item.url) {
    side.append(button("↗", "icon", () => window.open(item.url, "_blank", "noopener")));
  }

  row.append(glyph, body, side);
  return row;
}

function renderTree(payload) {
  const tree = $("#tree");
  const list = $("#list");
  list.hidden = true;
  tree.hidden = false;
  tree.replaceChildren();

  const areas = payload.areas || [];
  let projects = 0;

  for (const area of areas) {
    if (!area.projects || !area.projects.length) continue;
    const section = el("section");
    section.append(el("h2", "", area.title || "No area"));
    for (const project of area.projects) section.append(projectRows(project, 0));
    tree.append(section);
    projects += 1;
  }

  if (payload.loose_count) {
    const section = el("section", "loose");
    section.append(el("h2", "", "Unfiled projects"));
    section.append(el("p", "empty", `${payload.loose_count} project(s) with no area.`));
    tree.append(section);
  }

  if (!tree.childNodes.length) {
    tree.append(el("p", "empty", "No projects with open work."));
  }
}

function projectRows(project, depth) {
  const wrapper = document.createDocumentFragment();

  const row = button("", "project" + (depth ? " nested" : ""),
                     () => openProject(project.id, project.title));
  row.type = "button";
  row.append(el("span", "name", project.title));

  const stats = el("span", "stats");
  const open = project.action_count ?? project.total ?? 0;
  stats.append(el("span", "", `${open} open`));
  if (project.late) stats.append(el("span", "late", `  ${project.late} late`));
  row.append(stats);
  wrapper.append(row);

  for (const child of project.projects || []) {
    wrapper.append(projectRows(child, depth + 1));
  }
  return wrapper;
}

// ---------------------------------------------------------- calendar page

function shiftMonth(delta) {
  const parts = app.calendarMonth.split("-").map(Number);
  app.calendarMonth = monthKey(new Date(parts[0], parts[1] - 1 + delta, 1));
  refresh({ quiet: true });
}

// The same month the desktop's calendar draws: a Monday-first grid over
// `calendar --all`, with the selected day's commitments below it.
function renderCalendar(data) {
  const host = $("#list");
  host.hidden = false;
  $("#tree").hidden = true;
  host.replaceChildren();

  const byDate = {};
  for (const day of data.days || []) {
    day.late = (day.overdue || []).length > 0
      || (day.due || []).some((entry) => isLate(entry.due_at || entry.defer_at));
    byDate[day.date] = day;
  }

  if (app.calDay && !byDate[app.calDay]) app.calDay = "";
  if (!app.calDay) {
    app.calDay = todayStamp();
    if (!byDate[app.calDay]) {
      const days = data.days || [];
      app.calDay = (days.find((day) => day.count) || days[0] || {}).date || "";
    }
  }

  const cal = el("div", "cal");

  const head = el("div", "cal-head");
  const [year, month] = app.calendarMonth.split("-").map(Number);
  head.append(
    button("‹", "icon", () => shiftMonth(-1)),
    el("div", "cal-month-label", MONTHS[month - 1] + " " + year),
    button("›", "icon", () => shiftMonth(1)),
  );
  cal.append(head);

  const nav = el("div", "cal-nav");
  for (const name of ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]) {
    nav.append(el("span", "cal-dow", name[0]));
  }
  cal.append(nav);

  const grid = el("div", "cal-grid");
  const first = new Date(year, month - 1, 1);
  const cursor = new Date(year, month - 1, 1 - ((first.getDay() + 6) % 7));
  for (let i = 0; i < 42; i++) {
    const key = dateKey(cursor);
    const day = byDate[key];
    const cell = el("button", "cal-cell"
      + (cursor.getMonth() === month - 1 ? "" : " out")
      + (key === app.calDay ? " sel" : "")
      + (key === todayStamp() ? " today" : "")
      + (day && day.late ? " late" : ""));
    cell.type = "button";
    cell.append(el("span", "num", String(cursor.getDate())));
    if (day) {
      const dots = el("span", "cal-dots");
      if ((day.overdue || []).length) dots.append(el("i", "cal-dot over"));
      if ((day.due || []).length) dots.append(el("i", "cal-dot due"));
      if ((day.scheduled || []).length) dots.append(el("i", "cal-dot sched"));
      if ((day.completed || []).length) dots.append(el("i", "cal-dot done"));
      cell.append(dots);
    }
    cell.addEventListener("click", () => {
      app.calDay = key;
      refresh({ quiet: true });
    });
    grid.append(cell);
    cursor.setDate(cursor.getDate() + 1);
  }
  cal.append(grid);
  host.append(cal);

  const selected = byDate[app.calDay];
  if (!selected) {
    host.append(el("p", "empty", "Nothing on this day."));
    return;
  }

  const detail = el("div", "cal-day-list");
  const dayDate = stamp(app.calDay);
  detail.append(el("h2", "cal-day-title",
    dayDate ? dayDate.toLocaleDateString(undefined, { weekday: "long" }) : app.calDay));

  const groups = [["overdue", "Overdue"], ["due", "Due"], ["scheduled", "Scheduled"]];
  for (const [key, label] of groups) {
    const items = selected[key] || [];
    if (!items.length) continue;
    detail.append(el("h3", "cal-group " + key, label + " (" + items.length + ")"));
    for (const entry of items) detail.append(itemRow(entry));
  }

  if (!selected.count) {
    detail.append(el("p", "empty", "Nothing on this day."));
  }
  host.append(detail);
}

// ---------------------------------------------------------------- contexts

function renderContexts(contexts) {
  const host = $("#list");
  host.hidden = false;
  $("#tree").hidden = true;
  host.replaceChildren();

  if (!contexts.length) {
    host.append(el("p", "empty", "No contexts yet — capture with #tag and it appears here."));
    return;
  }

  for (const context of contexts) {
    if (!context.name) continue;
    const row = el("div", "row");
    row.append(el("span", "glyph", "#"));
    const body = el("div", "body");
    body.append(button("#" + context.name, "title", () => openContext(context.name)));
    row.append(body);
    if (context.open_count != null) {
      const side = el("div", "side");
      side.append(el("span", "stats", context.open_count + " open"));
      row.append(side);
    }
    host.append(row);
  }

  if (!host.querySelector(".row")) {
    host.append(el("p", "empty", "No contexts yet."));
  }
}

// ----------------------------------------------------------------- review

// Where a checklist question lands, when the phone has a view for it.
const REVIEW_TARGETS = {
  inbox: "inbox",
  forecast: "upcoming",
  waiting: "waiting",
  someday: "someday",
  overdue: "overdue",
  flagged: "flagged",
  stalled: "stalled",
  empty: "projects",
  references: "reference",
};

function renderReview(data) {
  const host = $("#list");
  host.hidden = false;
  $("#tree").hidden = true;
  host.replaceChildren();

  const stats = data.stats || {};
  host.append(el("h2", "review-head",
    (data.kind || "weekly") + " review  ·  " + (data.date || "")));

  const bits = [];
  for (const key of ["inbox", "next", "today", "overdue", "waiting", "someday",
                     "flagged", "projects", "stalled", "clarify"]) {
    if (stats[key]) bits.push(key + " " + stats[key]);
  }
  if (bits.length) host.append(el("p", "summary-line", bits.join("  ·  ")));

  if (stats.created_30 != null || stats.closed_30 != null) {
    host.append(el("p", "dim small stat-30",
      "last 30 days  ·  " + (stats.created_30 || 0)
      + " created  ·  " + (stats.closed_30 || 0) + " closed"));
  }

  for (const step of data.steps || []) {
    const row = el("div", "row");
    const glyph = el("span", "glyph", step.ok ? "✓" : "•");
    if (step.ok) glyph.style.color = "var(--green)";
    const body = el("div", "body");
    const target = REVIEW_TARGETS[step.key];
    const label = step.question || step.key;
    body.append(target
      ? button(label, "title", () => setView(target))
      : el("span", "title", label));
    const meta = el("div", "meta");
    meta.append(el("span", "", step.count + " / " + step.target));
    if (step.hint) meta.append(el("span", "", step.hint));
    body.append(meta);
    row.append(glyph, body);
    host.append(row);
  }

  const actions = el("div", "actions review-actions");
  actions.append(button("Mark reviewed", "accent",
    () => run(["review", "weekly", "--mark"], "Marked reviewed")));
  host.append(actions);
}

// ---------------------------------------------------------------- sheet

let sheetItem = null;

function showSheet() {
  $("#sheet").hidden = false;
  document.body.style.overflow = "hidden";
}

function hideSheet() {
  $("#sheet").hidden = true;
  document.body.style.overflow = "";
  sheetItem = null;
}

async function openItem(id) {
  try {
    // The item and the pickers' choice lists (the projects, the contexts)
    // in one round of two calls that start together.
    const [payload, state] = await Promise.all([api(["show", id]), api(["state"])]);
    sheetItem = payload.item;
    renderSheet(payload.item, state);
    showSheet();
  } catch (error) {
    if (error.message !== "password") toast(error.message, true);
  }
}

function renderSheet(item, state) {
  $("#sheet-title").textContent = item.title || "(untitled)";
  const body = $("#sheet-body");
  body.replaceChildren();

  const kind = [item.kind, item.status, item.parent_title].filter(Boolean).join(" · ");
  body.append(el("div", "sub", kind + "  " + (item.id || "")));

  const facts = el("dl", "facts");
  const fact = (label, value) => {
    if (!value) return;
    facts.append(el("dt", "", label), el("dd", "", value));
  };
  fact("Due", item.due_at ? `${humanDate(item.due_at)}  (${item.due_at})` : "");
  fact("Defer", item.defer_at ? `${humanDate(item.defer_at)}  (${item.defer_at})` : "");
  fact("Repeat", item.repeat ? `every ${item.repeat.interval} ${item.repeat.unit}` : "");
  fact("Effort", item.duration ? `${item.duration} min` : "");
  fact("Contexts", (item.tags || []).map((t) => "#" + t).join("  "));
  if (facts.childNodes.length) body.append(facts);

  if (item.notes) body.append(el("div", "notes", item.notes));

  if (item.url) {
    const actions = el("div", "actions");
    actions.append(button("Open link ↗", "accent", () => window.open(item.url, "_blank", "noopener")));
    body.append(actions);
  }

  if (item.status === "inbox") {
    body.append(el("div", "section-label", "Clarify"));
    body.append(clarifyActions(item));
  }

  if (item.children && item.children.length) {
    body.append(el("div", "section-label", `${item.open_children} of ${item.children.length} still open`));
  }

  body.append(el("div", "section-label", "Do"));
  body.append(doActions(item));

  if (item.kind === "action" && !isClosed(item)) {
    body.append(el("div", "section-label", "Project"));
    body.append(projectPicker(item, state));
    body.append(el("div", "section-label", "Contexts"));
    body.append(contextPicker(item, state));
  }

  body.append(el("div", "section-label", "Organize"));
  body.append(organizeActions(item));
}

function clarifyActions(item) {
  const row = el("div", "actions");
  const outcomes = [
    ["Actionable", "actionable"],
    ["Do now", "do-now"],
    ["Defer", "defer"],
    ["Waiting", "wait"],
    ["Someday", "someday"],
    ["Reference", "reference"],
    ["Delete", "delete"],
  ];
  for (const [label, outcome] of outcomes) {
    row.append(button(label, outcome === "delete" ? "bad" : "", async () => {
      const argv = ["clarify", item.id, outcome];
      if (outcome === "actionable" || outcome === "defer") {
        const home = prompt("Project or area (blank for none):", item.parent_title || "") || "";
        if (home.trim().startsWith("/a/")) argv.push("--area", home.trim().replace(/^\/a\//, "").replace(/\/$/, ""));
        else if (home.trim()) argv.push("--project", home.trim().replace(/^\//, "").replace(/\/$/, ""));
      }
      await run(argv, `Clarified: ${label.toLowerCase()}`);
    }));
  }
  return row;
}

// ------------------------------------------- project & context pickers

// A picker's tap re-reads the item and the choice lists, then repaints the
// panel in place: the ✓ and the chips must show the new truth, and the panel
// must stay where the person is working.
async function sheetEdit(item, state, argv, message) {
  try {
    await api(argv);
    if (message) toast(message);
    await refresh({ quiet: true });
    const [payload, fresh] = await Promise.all([api(["show", item.id]), api(["state"])]);
    sheetItem = payload.item;
    renderSheet(payload.item, fresh);
  } catch (error) {
    if (error.message !== "password") toast(error.message, true);
  }
}

// The state's areas and their projects, nesting and all.
function projectEntries(state) {
  const entries = [];
  const walk = (projects, depth) => {
    for (const project of projects || []) {
      entries.push({ label: "   ".repeat(depth) + project.title, project });
      walk(project.projects, depth + 1);
    }
  };
  for (const area of (state && state.areas) || []) {
    entries.push({ area: (area.title || "").trim() || "Unfiled" });
    walk(area.projects, 0);
  }
  return entries;
}

function projectPicker(item, state) {
  const block = el("div", "pickblock");
  const current = item.parent_id || "";
  const choice = (label, isCurrent, argv, message) =>
    button((isCurrent ? "✓ " : "") + label, isCurrent ? "accent" : "",
           () => sheetEdit(item, state, argv, message));

  block.append(choice("Back to the inbox (no project)", !current,
                      ["edit", item.id, "--inbox"], "Back to the inbox"));

  let rows = 0;
  for (const entry of projectEntries(state)) {
    if (entry.area) {
      block.append(el("div", "pick-area", entry.area));
      continue;
    }
    rows += 1;
    const project = entry.project;
    block.append(choice(entry.label, current === project.id,
                        ["edit", item.id, "--project", project.id],
                        "Moved to " + project.title));
  }
  if (!rows) block.append(el("p", "empty small", "No projects yet."));
  return block;
}

function contextPicker(item, state) {
  const block = el("div", "chips");
  const tagged = new Set(item.tags || []);
  const contexts = ((state && state.contexts) || []).filter((c) => c.name);

  if (!contexts.length) {
    block.append(el("span", "dim small", "no contexts yet — ＋ new… names one"));
  }
  for (const context of contexts) {
    const on = tagged.has(context.name);
    block.append(button((on ? "✓ " : "") + "#" + context.name, on ? "on" : "", () =>
      sheetEdit(item, state,
        ["edit", item.id, on ? "--untag" : "--tag", context.name],
        (on ? "Untagged " : "Tagged ") + context.name)));
  }

  block.append(button("＋ new context…", "", async () => {
    const name = (prompt("New context name, without the hash:", "") || "")
      .trim().replace(/^#/, "");
    if (!name) return;
    await sheetEdit(item, state, ["edit", item.id, "--tag", name], "Tagged " + name);
  }));
  return block;
}

function doActions(item) {
  const row = el("div", "actions");

  if (item.status !== "done") {
    row.append(button("✓ Done", "good", () => run(["done", item.id], "Done")));
  }
  row.append(button(item.priority ? "Unflag" : "Flag", "", () =>
    run([item.priority ? "unflag" : "flag", item.id], "Flag updated")));

  row.append(button("Defer +1d", "", () => run(["defer", item.id, "+1d"], "Deferred")));
  row.append(button("Defer +1w", "", () => run(["defer", item.id, "+1w"], "Deferred")));
  row.append(button("Postpone due", "", () => run(["postpone", item.id, "--days", "1"], "Deadline moved")));
  row.append(button("Undo", "", () => run(["undo", item.id], "Undone")));
  row.append(button("Drop", "bad", () => run(["drop", item.id], "Dropped")));

  return row;
}

function organizeActions(item) {
  const row = el("div", "actions");

  row.append(button("Rename", "", async () => {
    const title = prompt("New title:", item.title || "");
    if (!title || title === item.title) return;
    await run(["edit", item.id, "--title", title], "Renamed");
  }));

  row.append(button("Due date", "", async () => {
    const when = prompt("Due: today, tomorrow, fri, +2w, 2026-10-05", "tomorrow");
    if (!when) return;
    await run(["edit", item.id, "--due", when], "Deadline set");
  }));

  row.append(button("Defer date", "", async () => {
    const when = prompt("Defer: today, tomorrow, fri, +2w, 2026-10-05", "tomorrow");
    if (!when) return;
    await run(["edit", item.id, "--defer", when], "Deferred");
  }));

  row.append(button("Clear dates", "", () =>
    run(["edit", item.id, "--undefer", "--undue"], "Dates cleared")));

  return row;
}

// ------------------------------------------------------------------ run

async function run(argv, message) {
  try {
    await api(argv);
    hideSheet();
    if (message) toast(message);
    await refresh({ quiet: true });
  } catch (error) {
    if (error.message !== "password") toast(error.message, true);
  }
}

// ----------------------------------------------------------------- boot

function paintStatusBar() {
  // The installed app's status bar should be the page, not a fixed guess.
  const colour = getComputedStyle(document.documentElement)
    .getPropertyValue("--background").trim();
  if (colour) $("#theme-color").setAttribute("content", colour);
}

function wire() {
  const submitLock = () => {
    const value = $("#password").value.trim();
    if (!value) {
      $("#password").focus();
      return;
    }
    unlock(value, { announce: true });
  };

  $("#lock-form").addEventListener("submit", (event) => {
    event.preventDefault();
    submitLock();
  });

  // Phone keyboards send "Go"/"Enter" without always submitting the form.
  // Handle the key explicitly so one press is one attempt, and prevent the
  // default submit that would otherwise fire twice.
  $("#password").addEventListener("keydown", (event) => {
    if (event.key !== "Enter") return;
    event.preventDefault();
    submitLock();
  });

  $("#capture").addEventListener("submit", async (event) => {
    event.preventDefault();
    const field = $("#capture-text");
    const text = field.value.trim();
    if (!text) return;
    field.value = "";
    try {
      await api(["capture", text]);
      toast("Captured");
      await refresh({ quiet: true });
    } catch (error) {
      if (error.message !== "password") toast(error.message, true);
      field.value = text;
    }
  });

  document.querySelectorAll(".tabs button").forEach((node) => {
    node.addEventListener("click", () => {
      if (node.dataset.more) openMore();
      else setView(node.dataset.view);
    });
  });

  $("#refresh").addEventListener("click", () => refresh());
  $("#back").addEventListener("click", goBack);

  $("#sheet").addEventListener("click", (event) => {
    if (event.target.dataset.close !== undefined) hideSheet();
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") hideSheet();
  });

  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && app.password) {
      refresh({ quiet: true });
      if (!app.events) connect();
    }
  });
}

if ("serviceWorker" in navigator && (location.protocol === "https:" || location.hostname === "localhost")) {
  navigator.serviceWorker.register("/sw.js").catch(() => {});
}

wire();
paintStatusBar();
if (app.password) {
  unlock(app.password);
} else {
  lock();
}
