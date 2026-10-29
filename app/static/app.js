// Small helpers for the server-rendered pages; everything works without them except connecting banks.

// Dropdowns marked data-autosubmit (categories in lists, filters) apply as soon as you pick a value; fields so marked
// (the amount filter) apply when you leave them or press Return.
document.addEventListener("change", (event) => {
  if (event.target.matches("select[data-autosubmit], input[data-autosubmit]")) event.target.form.requestSubmit();
});

// On a transaction's page, the category field (where Excluded is one of the choices) shows only for regular
// transactions; income and transfers take no category and can't be excluded, since they never count as spending.
function showCategoryFor(typeInput) {
  for (const field of document.querySelectorAll("[data-category-field], [data-exclude-field]")) {
    field.hidden = typeInput.value !== "regular";
  }
  // A transfer isn't a recurring charge either.
  const recurringField = document.querySelector("[data-recurring-field]");
  if (recurringField) recurringField.hidden = typeInput.value === "transfer";
}
document.addEventListener("change", (event) => {
  if (event.target.matches("[data-type-select]")) showCategoryFor(event.target);
});

document.addEventListener("submit", (event) => {
  const form = event.target;
  if (form.dataset.confirm && !window.confirm(form.dataset.confirm)) {
    event.preventDefault();
    return;
  }
  // Show progress on slow buttons (sync, import) and prevent double submits. A button with a label beside its icon
  // changes only the label.
  const button = event.submitter;
  if (button && button.dataset.busy) {
    (button.querySelector("[data-busy-label]") || button).textContent = button.dataset.busy;
    setTimeout(() => { button.disabled = true; }, 0);
  }
});

// Puts the page `page` (parsed HTML from `url`) in place of this one's content, keeping the scroll position. The
// navigation shows how many transactions are waiting for review, and the sidebar the accounts' balances, so they're
// replaced too, keeping the sidebar's scroll position.
function swapIn(page, url) {
  const scrolled = [".sidebar", ".side-accounts"].map((part) => document.querySelector(part)?.scrollTop || 0);
  const place = listRows().findIndex((row) => row.id === currentId);
  for (const part of ["main", ".sidebar", ".topbar", ".tabbar"]) {
    const fresh = page.querySelector(part);
    if (fresh) document.querySelector(part).replaceWith(fresh);
  }
  [".sidebar", ".side-accounts"].forEach((part, i) => {
    const fresh = document.querySelector(part);
    if (fresh) fresh.scrollTop = scrolled[i];
  });
  document.title = page.title;
  history.replaceState(history.state, "", url);
  startToast();
  refreshSplits();
  updatePicks();
  watchOlder();
  markOpen();
  // The row the keys act on stays, or, gone from the list (reviewed on the review list, say), the one now in its place.
  const rows = listRows();
  if (currentId && !document.getElementById(currentId) && place !== -1 && rows.length) currentId = rows[Math.min(place, rows.length - 1)].id;
  markCurrent(false);
}

// Forms are posted to their action attribute: form.action would give a field named "action" instead, if one exists.
async function fetchPage(url, options) {
  const response = await fetch(url, options);
  const page = new DOMParser().parseFromString(await response.text(), "text/html");
  if (!page.querySelector("main")) throw new Error(`Unexpected answer (${response.status})`);
  return { page, url: response.url };
}

// Transactions open in a pane instead of on their own page, so the list stays where it was: beside the list on a wide
// screen, where the list can still be used and another row opens in its place, and over the page elsewhere. So do
// other pages' details, from links marked data-pane: a part of a report's chart, say.
const drawer = document.querySelector("[data-drawer]");
const wide = window.matchMedia("(min-width: 1280px)");
let openId = null;  // the transaction the pane shows

// Lights up the row of the transaction in the pane, if the list has it.
function markOpen() {
  for (const row of document.querySelectorAll(".txn.selected")) row.classList.remove("selected");
  const row = drawer && drawer.open && openId && (document.getElementById(`t${openId}`) || document.getElementById(`split${openId}`));
  if (row) row.classList.add("selected");
}

function showDrawer() {
  const docked = wide.matches;
  if (drawer.open && drawer.classList.contains("docked") !== docked) drawer.close();
  drawer.classList.toggle("docked", docked);
  document.body.classList.toggle("pane-open", docked);
  if (!drawer.open) {
    if (docked) drawer.show();
    else drawer.showModal();
  }
}

// Puts the transaction's sheet `sheet` in the pane. When it's the one already there, after a change that sent `sent`,
// the field with the focus keeps it, and what was typed in it since, and so do the scroll position and the actions shown.
function showSheet(sheet, sent = null) {
  const body = drawer.querySelector(".drawer-body");
  const same = openId === sheet.dataset.transaction;
  const active = document.activeElement;
  const typing = same && active && body.contains(active) && active.name && active.matches("input:not([type=checkbox], [type=radio]), textarea, select") ? active : null;
  const kept = typing && { name: typing.name, value: typing.value, start: typing.selectionStart, end: typing.selectionEnd };
  const shown = same ? [...body.querySelectorAll("[data-panel].shown")].map((panel) => panel.dataset.panel) : [];
  const scroll = same ? body.scrollTop : 0;
  body.replaceChildren(sheet);
  if (!document.documentElement.classList.contains("no-date-picker")) quietDates(sheet);
  openId = sheet.dataset.transaction;
  for (const name of shown) showPanel(sheet, name, false);
  refreshSplits();
  body.scrollTop = scroll;
  if (kept) {
    const again = body.querySelector(`[name="${CSS.escape(kept.name)}"]`);
    if (again) {
      if (again.tagName !== "SELECT" && !(sent && sent.get(kept.name) === kept.value)) again.value = kept.value;
      again.focus({ preventScroll: true });
      if (again.setSelectionRange && kept.start !== null) again.setSelectionRange(kept.start, kept.end);
    }
  }
}

async function openTransaction(url) {
  const { page } = await fetchPage(url);
  const sheet = page.querySelector(".sheet");
  if (!sheet) throw new Error("No transaction on that page");
  showSheet(sheet);
  showDrawer();
  markOpen();
}

function closeDrawer() {
  if (drawer && drawer.open) drawer.close();
}

if (drawer) {
  drawer.addEventListener("close", () => {
    if (drawer.open) return;  // NOTE: the event comes later; reopened in the meantime (docked or not), it stays open
    document.body.classList.remove("pane-open");
    openId = null;
    markOpen();
  });
  wide.addEventListener("change", () => { if (drawer.open) showDrawer(); });  // docked or over the page, as the width allows
  document.addEventListener("keydown", (event) => {
    // A pane over the page closes with Escape by itself; one beside the list, here.
    if (event.key === "Escape" && drawer.open && drawer.classList.contains("docked") && !event.defaultPrevented) {
      event.preventDefault();
      closeDrawer();
    }
  });
}

// The buttons and the menu at the top of a transaction show its rarer actions, one at a time; the same button again
// hides it.
function showPanel(sheet, name, focus = true) {
  let shown = null;
  for (const panel of sheet.querySelectorAll("[data-panel]")) {
    const show = panel.dataset.panel === name && !(focus && panel.classList.contains("shown"));
    panel.classList.toggle("shown", show);
    if (show) {
      panel.open = true;
      shown = panel;
    }
  }
  if (shown && focus) {
    shown.scrollIntoView({ block: "nearest", behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
    const field = shown.querySelector("select, input:not([type=hidden]), textarea, button");
    if (field) field.focus({ preventScroll: true });
  }
}

document.addEventListener("click", (event) => {
  const button = event.target.closest && event.target.closest("[data-show]");
  if (!button) return;
  showPanel(button.closest(".sheet"), button.dataset.show);
  const menu = button.closest("details.more-menu");
  if (menu) menu.removeAttribute("open");
});

// A transaction's day shows as words, which open the date picker of the date field kept unseen behind them. Browsers
// that can't open it from a script show the date field itself.
if (!("showPicker" in HTMLInputElement.prototype)) document.documentElement.classList.add("no-date-picker");

function quietDates(root) {
  for (const input of root.querySelectorAll(".sheet-date input[type=date]")) input.tabIndex = -1;  // its words take the focus
}

document.addEventListener("click", (event) => {
  const button = event.target.closest && event.target.closest("[data-pick-date]");
  if (!button) return;
  const input = button.parentElement.querySelector("input[type=date]");
  try {
    input.showPicker();
  } catch (error) {
    document.documentElement.classList.add("no-date-picker");
    input.focus();
  }
});
if (!document.documentElement.classList.contains("no-date-picker")) quietDates(document);

// A transaction's fields save as soon as they change; text, once it's left.
document.addEventListener("change", (event) => {
  const form = event.target.form;
  if (form && form.matches("form[data-autosave]")) form.requestSubmit();
});

document.addEventListener("click", (event) => {
  if (!drawer || event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
  if (event.target === drawer || (drawer.contains(event.target) && event.target.closest("[data-close]"))) {
    event.preventDefault();  // the backdrop, the close button, or Cancel (on a transaction's own page, Cancel goes back)
    closeDrawer();
    return;
  }
  const link = event.target.closest && event.target.closest("a.txn-name, a.linked, a.mark:not([data-jump]), a[data-pane]");
  if (!link) return;
  event.preventDefault();
  openTransaction(link.href).catch(() => window.location.assign(link.href));
});

// Forms in the panel post in the background. Saving or deleting brings back the list: it takes this page's place and
// the panel closes. Anything else (linking a payback, say) brings back the transaction: the panel shows it again and
// the list behind catches up.
document.addEventListener("submit", async (event) => {
  const form = event.target;
  if (event.defaultPrevented || !drawer || !drawer.contains(form)) return;
  event.preventDefault();
  if (form.matches("[data-autosave]")) form.elements.quiet.value = "1";  // the pane shows it's saved; no message needed
  const sent = new FormData(form, event.submitter);
  try {
    const { page, url } = await fetchPage(form.getAttribute("action"), { method: "POST", body: sent });
    const sheet = page.querySelector(".sheet");
    if (!sheet) {
      if (new URL(url).pathname !== window.location.pathname) {
        window.location.assign(url);
        return;
      }
      closeDrawer();
      swapIn(page, url);
      return;
    }
    showSheet(sheet, sent);
    const toast = page.querySelector("[data-toast]");
    const behind = await fetchPage(window.location.href);
    swapIn(behind.page, behind.url);
    if (toast) {
      document.querySelector("main").append(toast);
      startToast();
    }
  } catch (error) {
    window.location.reload();  // the change may or may not have been saved; show what the server has
  }
});

// Forms marked data-swap post in the background, then show the page they lead back to in place of this one's content,
// keeping the scroll position: recategorizing or reviewing in a long list doesn't jump back to the top.
document.addEventListener("submit", async (event) => {
  const form = event.target;
  if (event.defaultPrevented || !form.matches("form[data-swap]")) return;
  event.preventDefault();
  const focused = document.activeElement;
  const focusRow = focused && focused.closest("[id]") ? focused.closest("[id]").id : null;
  const focusName = focused ? focused.getAttribute("name") : null;
  if (form.matches("[data-autosave]")) form.elements.quiet.value = "1";  // a transaction on its own page, saved as it changed
  try {
    const { page, url } = await fetchPage(form.getAttribute("action"), { method: "POST", body: new FormData(form, event.submitter) });
    if (new URL(url).pathname !== window.location.pathname) {
      window.location.assign(url);
      return;
    }
    swapIn(page, url);
    const again = focusRow && focusName && document.getElementById(focusRow);
    const control = again && again.querySelector(`[name="${CSS.escape(focusName)}"]`);
    if (control) control.focus({ preventScroll: true });
  } catch (error) {
    window.location.reload();  // the change may or may not have been saved; show what the server has
  }
});

// The list's filters apply as soon as one changes: the list they give takes this one's place, with the filter menu
// still open where it was, and the address follows. The search waits for Return, and an amount for its numbers.
function amountReady(form) {
  const choice = form.elements.amount_op.value;
  const [first, second] = [form.elements.amount_min.value.trim(), form.elements.amount_max.value.trim()];
  return !choice || Boolean(choice === "between" ? first && second : first);
}

document.addEventListener("change", (event) => {
  const form = event.target.form;
  if (!form || !form.matches("form[data-filters]") || event.target.name === "q") return;
  if (event.target.name.startsWith("amount_") && !amountReady(form)) return;
  form.requestSubmit();
});

document.addEventListener("submit", async (event) => {
  const form = event.target;
  if (event.defaultPrevented || !form.matches("form[data-filters]")) return;
  event.preventDefault();
  const menu = form.querySelector("[data-filter-menu]");
  const stayOpen = menu && menu.open && !(event.submitter && event.submitter.closest(".filter-actions"));  // its button means done
  const scroll = menu ? menu.querySelector(".filter-panel").scrollTop : 0;
  const active = form.contains(document.activeElement) ? document.activeElement : null;
  const url = `${form.getAttribute("action")}?${new URLSearchParams(new FormData(form, event.submitter))}`;
  try {
    const { page, url: shown } = await fetchPage(url);
    swapIn(page, shown);
  } catch (error) {
    window.location.assign(url);
    return;
  }
  const again = document.querySelector("[data-filter-menu]");
  if (!again) return;
  again.open = stayOpen;
  again.querySelector(".filter-panel").scrollTop = scroll;
  const field = active && [...document.querySelectorAll(`form[data-filters] [name="${CSS.escape(active.name)}"]`)]
    .find((other) => !["checkbox", "radio"].includes(active.type) || other.value === active.value);
  if (field && (stayOpen || !again.contains(field))) field.focus({ preventScroll: true });
});

// NOTE: while it goes down the page, so the pane beside the list, which closes on Escape too, stays open
document.addEventListener("keydown", (event) => {
  const menu = document.querySelector("[data-filter-menu][open]");
  if (event.key !== "Escape" || !menu) return;
  event.preventDefault();
  menu.open = false;
  menu.querySelector("summary").focus();
}, true);

// Earlier months load as the list nears their link: the page that lists them too takes this one's place, keeping the
// scroll position and what's ticked, and its address, so later changes come back with every month loaded. Without
// JavaScript the link opens that page.
const olderWatch = "IntersectionObserver" in window
  ? new IntersectionObserver((seen) => { for (const entry of seen) if (entry.isIntersecting) loadOlder(entry.target); }, { rootMargin: "800px 0px" })
  : null;

function watchOlder() {
  const link = document.querySelector("[data-older]");
  if (link && olderWatch) olderWatch.observe(link);
}

async function loadOlder(link) {
  olderWatch.unobserve(link);
  const focused = document.activeElement && document.activeElement.closest("main") ? document.activeElement.getAttribute("name") : null;
  try {
    const { page, url } = await fetchPage(link.href.split("#")[0]);
    swapIn(page, url);
  } catch (error) {
    return;  // the link stays, to open the page by hand
  }
  const again = focused && document.querySelector(`main [name="${CSS.escape(focused)}"]`);
  if (again) again.focus({ preventScroll: true });
}

// The message after an action fades after a few seconds, or stays a while when it offers something to do.
function startToast() {
  const toast = document.querySelector("[data-toast]");
  if (!toast || toast.dataset.timer) return;
  const close = () => {
    toast.classList.add("leaving");
    setTimeout(() => toast.remove(), 300);
  };
  toast.dataset.timer = setTimeout(close, toast.hasAttribute("data-sticky") ? 20000 : 5000);
  toast.querySelector("[data-toast-close]").addEventListener("click", close);
}
startToast();
watchOlder();

// Rows that belong together (a purchase and its paybacks, the two sides of a move, linked transactions) light up
// together: those sharing a word of data-link.
function lightLinked(row, on) {
  for (const word of row.dataset.link.split(" ")) {
    for (const other of document.querySelectorAll(`.txn[data-link~="${CSS.escape(word)}"]`)) {
      other.classList.toggle("link-hover", on);
    }
  }
}
for (const [type, on] of [["mouseover", true], ["mouseout", false], ["focusin", true], ["focusout", false]]) {
  document.addEventListener(type, (event) => {
    const row = event.target.closest && event.target.closest(".txn[data-link]");
    if (!row || (event.relatedTarget && row.contains(event.relatedTarget))) return;
    lightLinked(row, on);
  });
}

// A link to the other half jumps to it when it's on this page, and opens its own page otherwise.
document.addEventListener("click", (event) => {
  const badge = event.target.closest && event.target.closest("[data-jump]");
  if (!badge || event.metaKey || event.ctrlKey || event.shiftKey) return;
  event.preventDefault();
  const target = document.getElementById(`t${badge.dataset.jump}`);
  if (!target || (drawer && drawer.contains(badge))) {
    if (drawer) openTransaction(badge.href).catch(() => window.location.assign(badge.href));
    else window.location.assign(badge.href);
    return;
  }
  const still = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  target.scrollIntoView({ block: "center", behavior: still ? "auto" : "smooth" });
  target.classList.remove("flash");
  void target.offsetWidth;  // restart the animation
  target.classList.add("flash");
});

// The sidebar's account groups stay folded or open from page to page: the server reads which are folded from a cookie.
// NOTE: toggle events don't bubble, so this listens as they go down to the group.
document.addEventListener("toggle", (event) => {
  if (!event.target.matches || !event.target.matches("details[data-group]")) return;
  const folded = [...document.querySelectorAll("details[data-group]:not([open])")].map((group) => group.dataset.group);
  document.cookie = `sidebar_folded=${folded.join(",")}; path=/; max-age=31536000; samesite=lax`;
}, true);

// Light or dark: the sidebar's button switches to the other at once, and so do Settings' choices; the cookie keeps it
// for the server (main.THEME_COOKIE), so the next page comes in it with no flash.
function setTheme(theme) {
  const root = document.documentElement;
  if (theme === "light" || theme === "dark") {
    root.dataset.theme = theme;
    document.cookie = `theme=${theme}; path=/; max-age=31536000; samesite=lax`;
  } else {
    delete root.dataset.theme;
    document.cookie = "theme=; path=/; max-age=0; samesite=lax";
  }
  document.querySelector('meta[name="color-scheme"]').content = root.dataset.theme || "light dark";
  for (const meta of document.querySelectorAll("meta[data-theme-color]")) {
    meta.media = root.dataset.theme ? (meta.dataset.themeColor === root.dataset.theme ? "all" : "not all") : `(prefers-color-scheme: ${meta.dataset.themeColor})`;
  }
  for (const choice of document.querySelectorAll("[data-theme-form] input[name=theme]")) choice.checked = choice.value === (root.dataset.theme || "system");
}

document.addEventListener("click", (event) => {
  const button = event.target.closest && event.target.closest("[data-theme-to]");
  if (!button) return;
  event.preventDefault();
  setTheme(button.dataset.themeTo);
});
document.addEventListener("change", (event) => {
  if (event.target.matches("[data-theme-form] input[name=theme]")) setTheme(event.target.value);
});

// Close the phone's More menu, and the filter menu, when tapping elsewhere.
document.addEventListener("click", (event) => {
  for (const menu of document.querySelectorAll("details.more-menu[open], details.filter-menu[open]")) {
    if (!menu.contains(event.target)) menu.removeAttribute("open");
  }
});

// Line charts: a crosshair and readout follow the pointer, or the arrow keys once the chart has focus.
const chartData = new WeakMap();
const dollars = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" });

function chartOf(plot) {
  if (!chartData.has(plot)) chartData.set(plot, JSON.parse(plot.querySelector("script[type='application/json']").textContent));
  return chartData.get(plot);
}

function showPoint(plot, index) {
  const data = chartOf(plot);
  const count = data.labels.length;
  index = Math.max(0, Math.min(count - 1, index));
  plot.dataset.index = index;
  const left = count > 1 ? (index / (count - 1)) * 100 : 0;
  const crosshair = plot.querySelector(".crosshair");
  crosshair.style.left = `${left}%`;
  crosshair.hidden = false;
  const dots = plot.querySelectorAll(".hover-dot");
  data.series.forEach((s, i) => {
    const value = s.values[index];
    dots[i].hidden = value === undefined;
    if (value === undefined) return;
    dots[i].style.left = `${left}%`;
    dots[i].style.top = `${((data.high - value) / (data.high - data.low)) * 100}%`;
  });
  const tip = plot.querySelector(".tip");
  const title = document.createElement("div");
  title.className = "tip-title";
  title.textContent = data.labels[index];
  tip.replaceChildren(title);
  for (const s of [...data.series].reverse()) {  // the series shown first in the legend first
    const value = s.values[index];
    if (value === undefined) continue;
    const row = document.createElement("div");
    row.className = "tip-row";
    const key = document.createElement("i");
    key.className = `key role-${s.role}`;
    const amount = document.createElement("b");
    amount.textContent = dollars.format(value);
    const name = document.createElement("span");
    name.textContent = s.name;
    row.append(key, amount, name);
    tip.append(row);
  }
  // Beside the crosshair, on whichever side has room.
  tip.style.left = left < 50 ? `calc(${left}% + 12px)` : "auto";
  tip.style.right = left < 50 ? "auto" : `calc(${100 - left}% + 12px)`;
  tip.hidden = false;
}

function hidePoint(plot) {
  for (const part of plot.querySelectorAll(".crosshair, .hover-dot, .tip")) part.hidden = true;
  delete plot.dataset.index;
}

function pointAt(plot, event) {
  const box = plot.getBoundingClientRect();
  showPoint(plot, Math.round(((event.clientX - box.left) / box.width) * (chartOf(plot).labels.length - 1)));
}

for (const type of ["pointermove", "pointerdown"]) {
  document.addEventListener(type, (event) => {
    const plot = event.target.closest && event.target.closest("[data-plot]");
    if (plot) pointAt(plot, event);
  });
}
document.addEventListener("pointerout", (event) => {
  const plot = event.target.closest && event.target.closest("[data-plot]");
  if (plot && !plot.contains(event.relatedTarget) && document.activeElement !== plot) hidePoint(plot);
});
document.addEventListener("focusin", (event) => {
  if (event.target.matches("[data-plot]")) {
    const data = chartOf(event.target);
    const main = data.series[data.series.length - 1];
    showPoint(event.target, Math.max(main.values.length - 1, 0));
  }
});
document.addEventListener("focusout", (event) => {
  if (event.target.matches("[data-plot]")) hidePoint(event.target);
});
document.addEventListener("keydown", (event) => {
  const plot = event.target;
  if (!plot.matches || !plot.matches("[data-plot]")) return;
  const count = chartOf(plot).labels.length;
  const index = Number(plot.dataset.index || 0);
  const moves = { ArrowLeft: index - 1, ArrowRight: index + 1, Home: 0, End: count - 1 };
  if (event.key in moves) {
    event.preventDefault();
    showPoint(plot, moves[event.key]);
  } else if (event.key === "Escape") {
    hidePoint(plot);
  }
});

// Splitting a transaction: a running total of the parts against the transaction's, with what's left shown in the part
// that has no amount, and rows to add or remove. The server checks the same rules (splits.read_parts).
function partCents(text) {
  const clean = text.trim().replace(/[$,]/g, "");
  if (!clean) return null;
  return /^(\d+(\.\d{0,2})?|\.\d{1,2})$/.test(clean) ? Math.round(Number(clean) * 100) : NaN;
}

function updateSplit(form) {
  const total = Number(form.dataset.total);
  const without = [];  // amount fields of parts that get what's left
  let parts = 0;  // rows with a name or an amount; empty ones are left out
  let typed = 0;
  let unreadable = false;
  for (const row of form.querySelectorAll(".split-row")) {
    const amount = row.querySelector("[name=part_amount]");
    const value = partCents(amount.value);
    amount.placeholder = "The rest";
    if (value === null) {
      if (row.querySelector("[name=part_name]").value.trim()) {
        without.push(amount);
        parts += 1;
      }
    } else {
      parts += 1;
      if (Number.isNaN(value) || value <= 0) unreadable = true;
      else typed += value;
    }
  }
  const left = total - typed;
  const show = (value) => dollars.format(value / 100);
  let text;
  let off = true;
  if (unreadable) {
    text = "Type each amount like 12.50, without a sign.";
  } else if (parts < 2) {
    text = "A split needs at least two parts.";
  } else if (without.length > 1) {
    off = typed > 0;  // nothing typed yet isn't a mistake
    text = off ? `Type the amounts of all the parts but one, which gets what's left (${show(Math.max(left, 0))}).`
      : `Type how much each part is. Leave one empty and it gets what's left of ${show(total)}.`;
  } else if (without.length === 1) {
    off = left <= 0;
    text = off ? `The other parts already add up to ${show(typed)} of ${show(total)}.` : `${show(left)} left for the part without an amount.`;
    if (!off) without[0].placeholder = (left / 100).toFixed(2);
  } else if (left === 0) {
    off = false;
    text = `The parts add up to ${show(total)}.`;
  } else {
    text = left > 0 ? `${show(left)} left to give a part.` : `The parts are ${show(-left)} over the total of ${show(total)}.`;
  }
  const status = form.querySelector("[data-split-status]");
  status.textContent = text;
  status.classList.toggle("off", off);
}

function renumberParts(form) {
  form.querySelectorAll(".split-row").forEach((row, i) => {
    for (const [selector, label] of [["[name=part_name]", "Name"], ["[name=part_amount]", "Amount"], ["select[name=part_category]", "Category"]]) {
      const field = row.querySelector(selector);
      if (field) field.setAttribute("aria-label", `${label} of part ${i + 1}`);
    }
    row.querySelector("[data-remove-part]").setAttribute("aria-label", `Remove part ${i + 1}`);
  });
}

function refreshSplits() {
  for (const form of document.querySelectorAll("form[data-split]")) updateSplit(form);
}

document.addEventListener("input", (event) => {
  const form = event.target.closest && event.target.closest("form[data-split]");
  if (form) updateSplit(form);
});

document.addEventListener("click", (event) => {
  const button = event.target.closest && event.target.closest("[data-add-part], [data-remove-part]");
  if (!button) return;
  const form = button.closest("form[data-split]");
  const rows = form.querySelectorAll(".split-row");
  if (button.matches("[data-add-part]")) {
    const row = rows[rows.length - 1].cloneNode(true);
    for (const field of row.querySelectorAll("input, select")) field.value = "";
    rows[rows.length - 1].after(row);
    renumberParts(form);
    row.querySelector("[name=part_amount]").focus();
  } else if (rows.length > 1) {
    button.closest(".split-row").remove();
    renumberParts(form);
  }
  updateSplit(form);
});
refreshSplits();

// Ticking transactions in the list. What's ticked stays ticked as the page changes around it (swapIn), as long as it's
// listed. With two or more ticked on a wide screen, the pane beside the list changes them together; otherwise the bar
// at the bottom does.
const picked = new Set();

let selecting = false;  // on a touch screen, after Select: every row shows its checkbox, and a tap on a row ticks it
let lastPick = null;  // the value of the checkbox ticked or unticked last, for Shift-clicks

function updatePicks() {
  const months = document.querySelector(".months");
  if (months) months.classList.toggle("selecting", selecting);
  const toggle = document.querySelector("[data-select-toggle]");
  if (toggle) {
    toggle.classList.toggle("on", selecting);
    toggle.setAttribute("aria-pressed", String(selecting));
    toggle.querySelector("span").textContent = selecting ? "Done" : "Select";
  }
  const picks = [...document.querySelectorAll("input.pick")];
  const listed = new Set(picks.map((pick) => pick.value));
  for (const id of [...picked]) if (!listed.has(id)) picked.delete(id);
  for (const pick of picks) pick.checked = picked.has(pick.value);
  // A split's box is ticked when all of its parts listed here are.
  for (const group of document.querySelectorAll("[data-pick-group]")) {
    const parts = picks.filter((pick) => pick.dataset.partOf === group.dataset.pickGroup);
    group.checked = parts.length > 0 && parts.every((pick) => pick.checked);
  }
  const bar = document.querySelector("[data-bulk]");
  if (!bar) return;
  bar.querySelector("[data-picked]").textContent = picked.size ? `${picked.size} selected` : "Tick the transactions to change";
  for (const button of bar.querySelectorAll("button[name=action]")) button.disabled = !picked.size;
  bar.classList.toggle("empty", !picked.size);  // the actions show once something is ticked
  bar.querySelector("[data-pick-all]").checked = picked.size > 0 && picked.size === picks.length;
}

// The pane shows what's ticked: two or more together, one on its own, none closes it. Beside the list, it follows as
// they're ticked; over the page (a narrower screen), once asked to (`now`).
let pickTimer = null;
function showPicked(now = false) {
  clearTimeout(pickTimer);
  pickTimer = setTimeout(async () => {
    if (!drawer || !(wide.matches || now)) return;
    const ids = [...picked];
    if (now && ids.length === 1) {
      openTransaction(`/transactions/${ids[0]}?back=${encodeURIComponent(location.pathname + location.search)}`).catch(() => {});
      return;
    }
    if (ids.length < 2) {
      if (openId !== "selected") return;
      if (ids.length) openTransaction(`/transactions/${ids[0]}?back=${encodeURIComponent(location.pathname + location.search)}`).catch(() => {});
      else closeDrawer();
      return;
    }
    const query = new URLSearchParams([...ids.map((id) => ["ids", id]), ["back", location.pathname + location.search]]);
    try {
      const { page } = await fetchPage(`/transactions/selected?${query}`);
      const sheet = page.querySelector(".sheet");
      if (!sheet || [...picked].join() !== ids.join()) return;  // ticked differently meanwhile: the newer call shows it
      showSheet(sheet);
      showDrawer();
      markOpen();
    } catch (error) {
      // the bar at the bottom can still change them
    }
  }, 120);
}

document.addEventListener("change", (event) => {
  const target = event.target;
  if (!target.matches("input.pick, [data-pick-all], [data-pick-group]")) return;
  const all = [...document.querySelectorAll("input.pick")];
  const changed = target.matches("[data-pick-all]") ? all
    : target.matches("[data-pick-group]") ? all.filter((pick) => pick.dataset.partOf === target.dataset.pickGroup) : [target];
  for (const pick of changed) {
    if (target.checked) picked.add(pick.value);
    else picked.delete(pick.value);
  }
  updatePicks();
  showPicked();
});

document.addEventListener("click", (event) => {
  if (!event.target.closest) return;
  if (event.target.closest("[data-edit-picks]")) showPicked(true);
  // Shift-click: everything between this checkbox and the one clicked before follows it.
  const pick = event.target.closest("input.pick");
  if (pick) {
    const all = [...document.querySelectorAll("input.pick")];
    const before = all.findIndex((other) => other.value === lastPick);
    if (event.shiftKey && before !== -1 && all[before] !== pick) {
      const [from, to] = [before, all.indexOf(pick)].sort((a, b) => a - b);
      for (const other of all.slice(from, to + 1)) {
        if (pick.checked) picked.add(other.value);
        else picked.delete(other.value);
      }
    }
    lastPick = pick.value;
  }
  if (event.target.closest("[data-select-toggle]")) {
    selecting = !selecting;
    if (!selecting) picked.clear();  // done selecting, as on a phone's own lists
    updatePicks();
    if (!selecting && openId === "selected") closeDrawer();
    return;
  }
  // While selecting, a tap on a row ticks it, rather than opening it; its menus and buttons work as ever.
  const row = selecting && event.target.closest("ul.txns > li.txn");
  if (row && !event.target.closest("input, select, button, label, .pill-pick")) {
    event.preventDefault();
    event.stopImmediatePropagation();
    const box = row.querySelector("input.pick, input.pick-group");
    if (box) box.click();
    return;
  }
  if (!event.target.closest("[data-clear-picks]")) return;
  picked.clear();
  updatePicks();
  if (openId === "selected") closeDrawer();
}, true);
updatePicks();

// The keys the list takes (the shortcuts dialog lists them): not while typing, nor with a modifier key, nor while a
// dialog is open over the page.
let currentId = null;  // the element id of the row the keys act on, like "t12"

function listRows() {
  return document.querySelector("[data-shortcuts]") ? [...document.querySelectorAll("ul.txns > li.txn")] : [];
}

function markCurrent(scroll) {
  for (const row of document.querySelectorAll(".txn.current")) row.classList.remove("current");
  const row = currentId && document.getElementById(currentId);
  if (!row) return null;
  row.classList.add("current");
  if (scroll) row.scrollIntoView({ block: "nearest" });
  return row;
}

function openRow(row) {
  const link = row && row.querySelector("a.txn-name");
  if (link) openTransaction(link.href).catch(() => window.location.assign(link.href));
}

// ↑ and ↓: the next row or the one before, from the one the keys act on, else the one open; an open transaction follows.
function moveCurrent(step) {
  const rows = listRows();
  if (!rows.length) return;
  let index = rows.findIndex((row) => row.id === currentId);
  if (index === -1) index = rows.findIndex((row) => row.classList.contains("selected"));
  index = index === -1 ? (step > 0 ? 0 : rows.length - 1) : Math.max(0, Math.min(rows.length - 1, index + step));
  currentId = rows[index].id;
  const row = markCurrent(true);
  if (drawer && drawer.open && drawer.classList.contains("docked") && openId !== "selected") openRow(row);
}

// R: the ticked ones, else the row the keys act on, marked as reviewed.
async function markReviewed(row) {
  const ids = picked.size ? [...picked] : row && /^t\d+$/.test(row.id) ? [row.id.slice(1)] : [];
  if (!ids.length) return;
  const body = new FormData();
  for (const id of ids) body.append("ids", id);
  body.append("back", location.pathname + location.search);
  try {
    const { page, url } = await fetchPage("/transactions/review", { method: "POST", body });
    swapIn(page, url);
  } catch (error) {
    window.location.reload();
  }
}

// C: the category menu of the row, or of the transaction open beside the list when it's that one.
function pickCategory(row) {
  const open = drawer && drawer.open && row.id === `t${openId}` && drawer.querySelector(".sheet [name=category_id]");
  const menu = open || row.querySelector(".pill-menu");
  if (!menu) return;
  menu.focus();
  try {
    menu.showPicker();
  } catch (error) {
    // NOTE: a browser that can't open it from a script leaves it focused, for the arrow keys or the space bar
  }
}

function focusSearch() {
  const field = [...document.querySelectorAll(".side-search input, .toolbar .search input")].find((input) => input.offsetParent);
  if (field) {
    field.focus();
    field.select();
  }
}

document.addEventListener("keydown", (event) => {
  const help = document.querySelector("[data-shortcuts]");
  if (!help || event.defaultPrevented || event.metaKey || event.ctrlKey || event.altKey) return;
  if (event.target.closest && event.target.closest("input:not([type=checkbox], [type=radio]), textarea, select, [contenteditable]")) return;
  if (help.open || (drawer && drawer.open && !drawer.classList.contains("docked"))) return;
  const row = currentId && document.getElementById(currentId);
  const key = event.key.length === 1 ? event.key.toLowerCase() : event.key;
  if (key === "ArrowDown" || key === "ArrowUp") {
    event.preventDefault();
    document.body.classList.add("keys");  // the row they act on shows, until the pointer's back (below)
    moveCurrent(key === "ArrowDown" ? 1 : -1);
  } else if (key === "Enter" && row && !event.target.closest("a, button, summary, label")) {
    event.preventDefault();
    openRow(row);
  } else if (key === "x" && row) {
    const pick = row.querySelector("input.pick, input.pick-group");
    if (pick) pick.click();
  } else if (key === "r") {
    markReviewed(row);
  } else if (key === "c" && row) {
    event.preventDefault();
    pickCategory(row);
  } else if (key === "/") {
    event.preventDefault();
    focusSearch();
  } else if (key === "?") {
    help.showModal();
  } else if (key === "Escape" && picked.size) {
    picked.clear();  // NOTE: the menu and the pane take Escape first (above), so unticking comes once they're closed
    updatePicks();
  }
});

document.addEventListener("pointerdown", () => document.body.classList.remove("keys"));

// A row clicked or ticked is the one the keys act on from then on.
document.addEventListener("click", (event) => {
  const row = event.target.closest && event.target.closest("ul.txns > li.txn");
  if (row && document.querySelector("[data-shortcuts]")) {
    currentId = row.id;
    markCurrent(false);
  }
  if (event.target.closest && event.target.closest("[data-show-shortcuts]")) document.querySelector("[data-shortcuts]").showModal();
});

async function postJSON(url, body) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(data.error || data.detail || `Request failed (${response.status})`);
    error.data = data;
    throw error;
  }
  return data;
}

// Sends a new bank login from Link to the server to save. If the server says it duplicates an existing
// connection, asks first; returns null when the user decides not to connect it after all.
async function saveNewConnection(publicToken, metadata) {
  const body = {
    public_token: publicToken,
    institution: metadata.institution,
    accounts: (metadata.accounts || []).map((a) => ({ id: a.id, name: a.name, mask: a.mask, type: a.type, subtype: a.subtype })),
    link_session_id: metadata.link_session_id,
  };
  try {
    return await postJSON("/plaid/exchange", body);
  } catch (error) {
    if (!error.data || !error.data.duplicate) throw error;
    if (!window.confirm(`${error.message}\n\nConnect it anyway?`)) return null;
    return postJSON("/plaid/exchange", { ...body, allow_duplicate: true });
  }
}

let linkHandler = null;  // Link's handler from the last time it opened; destroyed before making another

// Opens Plaid Link to connect a new bank, or with itemId, to log in again to an existing one.
async function openPlaidLink(itemId) {
  if (!window.Plaid) {
    alert("Plaid's login window couldn't load. Check your internet connection and try again.");
    return;
  }
  let token;
  try {
    token = (await postJSON("/plaid/link-token", { item_id: itemId || null })).link_token;
  } catch (error) {
    alert(error.message);
    return;
  }
  if (linkHandler) linkHandler.destroy();
  linkHandler = window.Plaid.create({
    token,
    onSuccess: async (publicToken, metadata) => {
      try {
        const result = itemId
          ? await postJSON(`/plaid/items/${encodeURIComponent(itemId)}/repaired`, { link_session_id: metadata.link_session_id })
          : await saveNewConnection(publicToken, metadata);
        if (result) window.location = result.redirect;
      } catch (error) {
        alert(`Connecting failed: ${error.message}`);
      }
    },
    onExit: (error, metadata = {}) => {
      // The server logs Link's session id, which Plaid support needs to look into a failed connection.
      postJSON("/plaid/link-exit", {
        error,
        institution: metadata.institution,
        status: metadata.status,
        link_session_id: metadata.link_session_id,
        request_id: metadata.request_id,
      }).catch(() => {});
      if (error) alert(error.display_message || `Plaid couldn't finish connecting (${error.error_code}). Try again in a few minutes.`);
    },
  });
  linkHandler.open();
}

document.addEventListener("click", (event) => {
  const button = event.target.closest("[data-plaid-link]");
  if (!button) return;
  event.preventDefault();
  if (button.dataset.confirm && !window.confirm(button.dataset.confirm)) return;
  openPlaidLink(button.dataset.itemId);
});
