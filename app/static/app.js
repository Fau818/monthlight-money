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
}

// Forms are posted to their action attribute: form.action would give a field named "action" instead, if one exists.
async function fetchPage(url, options) {
  const response = await fetch(url, options);
  const page = new DOMParser().parseFromString(await response.text(), "text/html");
  if (!page.querySelector("main")) throw new Error(`Unexpected answer (${response.status})`);
  return { page, url: response.url };
}

// Transactions open in a panel over the page instead of on their own page, so the list behind stays where it was.
const drawer = document.querySelector("[data-drawer]");

async function openTransaction(url) {
  const { page } = await fetchPage(url);
  const sheet = page.querySelector(".sheet");
  if (!sheet) throw new Error("No transaction on that page");
  drawer.querySelector(".drawer-body").replaceChildren(sheet);
  refreshSplits();
  if (!drawer.open) drawer.showModal();
  drawer.querySelector(".drawer-body").scrollTop = 0;
}

function closeDrawer() {
  if (drawer && drawer.open) drawer.close();
}

document.addEventListener("click", (event) => {
  if (!drawer || event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
  if (event.target === drawer || (drawer.contains(event.target) && event.target.closest("[data-close]"))) {
    event.preventDefault();  // the backdrop, the close button, or Cancel (on a transaction's own page, Cancel goes back)
    closeDrawer();
    return;
  }
  const link = event.target.closest && event.target.closest("a.txn-name, a.linked, a.link-badge:not([data-jump])");
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
  try {
    const { page, url } = await fetchPage(form.getAttribute("action"), { method: "POST", body: new FormData(form, event.submitter) });
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
    drawer.querySelector(".drawer-body").replaceChildren(sheet);
    refreshSplits();
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

// Close the phone's More menu when tapping elsewhere.
document.addEventListener("click", (event) => {
  for (const menu of document.querySelectorAll("details.more-menu[open]")) {
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

// Selecting transactions in the list: how many are ticked, and one box for every one on the page.
function updatePicks() {
  const bar = document.querySelector("[data-bulk]");
  if (!bar) return;
  const picks = [...document.querySelectorAll("input.pick")];
  const ticked = picks.filter((pick) => pick.checked).length;
  bar.querySelector("[data-picked]").textContent = ticked ? `${ticked} selected` : "Tick the transactions to change";
  for (const button of bar.querySelectorAll("button[name=action]")) button.disabled = !ticked;
  bar.classList.toggle("empty", !ticked);  // the actions show once something is ticked
  bar.querySelector("[data-pick-all]").checked = ticked > 0 && ticked === picks.length;
  // A split's box is ticked when all of its parts listed here are.
  for (const group of document.querySelectorAll("[data-pick-group]")) {
    const parts = [...document.querySelectorAll(`input.pick[data-part-of="${CSS.escape(group.dataset.pickGroup)}"]`)];
    group.checked = parts.length > 0 && parts.every((pick) => pick.checked);
  }
}

document.addEventListener("change", (event) => {
  if (event.target.matches("[data-pick-all]")) {
    for (const pick of document.querySelectorAll("input.pick")) pick.checked = event.target.checked;
  }
  if (event.target.matches("[data-pick-group]")) {
    const parts = document.querySelectorAll(`input.pick[data-part-of="${CSS.escape(event.target.dataset.pickGroup)}"]`);
    for (const pick of parts) pick.checked = event.target.checked;
  }
  if (event.target.matches("input.pick, [data-pick-all], [data-pick-group]")) updatePicks();
});
updatePicks();

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
