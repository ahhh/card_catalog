/* Card Catalog — small client-side glue. No framework. */

(function () {
  "use strict";

  // ---- Toast helpers --------------------------------------------------------
  const toastStack = () => {
    let el = document.getElementById("toast-stack");
    if (!el) {
      el = document.createElement("div");
      el.id = "toast-stack";
      el.className = "toast-stack";
      document.body.appendChild(el);
    }
    return el;
  };

  function showToast({ title, body, kind = "info", timeout = 4000 } = {}) {
    const el = document.createElement("div");
    el.className = `toast toast--${kind}`;
    el.innerHTML = `
      <div class="stack stack--tight" style="flex:1;">
        ${title ? `<p class="toast__title">${escapeHtml(title)}</p>` : ""}
        ${body ? `<p class="toast__body">${escapeHtml(body)}</p>` : ""}
      </div>
    `;
    toastStack().appendChild(el);
    setTimeout(() => {
      el.style.transition = "opacity 200ms ease, transform 200ms ease";
      el.style.opacity = "0";
      el.style.transform = "translateX(20px)";
      setTimeout(() => el.remove(), 220);
    }, timeout);
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
  }

  window.CC = window.CC || {};
  window.CC.toast = showToast;

  // ---- HTMX integration ----------------------------------------------------
  document.addEventListener("htmx:afterRequest", (evt) => {
    const xhr = evt.detail.xhr;
    if (!xhr) return;
    const flash = xhr.getResponseHeader("HX-Toast");
    if (flash) {
      try {
        const data = JSON.parse(flash);
        showToast(data);
      } catch (_) {
        showToast({ body: flash });
      }
    }
    if (!evt.detail.successful && xhr.status >= 400) {
      showToast({
        kind: "error",
        title: `Request failed (${xhr.status})`,
        body: xhr.statusText || "Try again or check the logs.",
      });
    }
  });

  // ---- Slide-over open/close -----------------------------------------------
  document.addEventListener("click", (e) => {
    const trigger = e.target.closest("[data-slideover-close]");
    if (trigger) {
      const root = trigger.closest(".slideover");
      if (root) closeSlideover(root);
    }
    const backdrop = e.target.closest(".slideover__backdrop");
    if (backdrop) {
      const root = backdrop.closest(".slideover");
      if (root) closeSlideover(root);
    }
  });

  function closeSlideover(el) {
    el.classList.remove("is-open");
    setTimeout(() => {
      // If the slide-over was injected dynamically, remove it.
      if (el.dataset.dynamic === "1") el.remove();
    }, 250);
  }

  document.addEventListener("htmx:afterSwap", (evt) => {
    // Auto-open any slide-over that was just swapped in
    const so = evt.detail.target.querySelector(".slideover[data-auto-open]");
    if (so) requestAnimationFrame(() => so.classList.add("is-open"));
  });

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      document.querySelectorAll(".slideover.is-open").forEach((el) => closeSlideover(el));
      document.querySelectorAll(".modal.is-open").forEach((el) => el.classList.remove("is-open"));
    }
    // "/" focuses the global search if one exists
    if (e.key === "/" && !["INPUT", "TEXTAREA"].includes(document.activeElement.tagName)) {
      const search = document.querySelector("[data-global-search]");
      if (search) {
        e.preventDefault();
        search.focus();
        search.select();
      }
    }
  });

  // ---- Filter chips (sidebar) ----------------------------------------------
  document.addEventListener("click", (e) => {
    const chip = e.target.closest("[data-chip-toggle]");
    if (!chip) return;
    const pressed = chip.getAttribute("aria-pressed") === "true";
    chip.setAttribute("aria-pressed", pressed ? "false" : "true");

    // Sync into hidden input
    const name = chip.dataset.name;
    const value = chip.dataset.value;
    const form = chip.closest("form");
    if (!form || !name) return;
    let input = form.querySelector(`input[type="hidden"][data-chip="${name}:${value}"]`);
    if (pressed) {
      if (input) input.remove();
    } else {
      if (!input) {
        input = document.createElement("input");
        input.type = "hidden";
        input.name = name;
        input.value = value;
        input.dataset.chip = `${name}:${value}`;
        form.appendChild(input);
      }
    }
    // Trigger HTMX request manually for the form
    htmx.trigger(form, "filter-change");
  });

  // ---- Bulk action selection -----------------------------------------------
  function updateBulkBar() {
    const checks = document.querySelectorAll(".row-check:checked");
    const bar = document.getElementById("bulk-bar");
    if (!bar) return;
    const count = checks.length;
    if (count > 0) {
      bar.classList.add("is-visible");
      const c = bar.querySelector(".bulk-bar__count");
      if (c) c.textContent = count;
      const idsField = bar.querySelector("[name='entry_ids']");
      if (idsField) idsField.value = Array.from(checks).map((c) => c.value).join(",");
    } else {
      bar.classList.remove("is-visible");
    }
  }
  document.addEventListener("change", (e) => {
    if (e.target.matches(".row-check, .row-check-all")) {
      if (e.target.matches(".row-check-all")) {
        document
          .querySelectorAll(".row-check")
          .forEach((c) => (c.checked = e.target.checked));
      }
      updateBulkBar();
    }
  });
  document.addEventListener("htmx:afterSwap", updateBulkBar);

  // ---- Dropzone (CSV import) -----------------------------------------------
  document.addEventListener("dragover", (e) => {
    if (e.target.closest(".dropzone")) {
      e.preventDefault();
      e.target.closest(".dropzone").classList.add("is-drag");
    }
  });
  document.addEventListener("dragleave", (e) => {
    if (e.target.closest(".dropzone")) {
      e.target.closest(".dropzone").classList.remove("is-drag");
    }
  });
  document.addEventListener("drop", (e) => {
    const zone = e.target.closest(".dropzone");
    if (zone) {
      e.preventDefault();
      zone.classList.remove("is-drag");
      const input = zone.querySelector("input[type=file]");
      if (input && e.dataTransfer.files.length) {
        input.files = e.dataTransfer.files;
        const evt = new Event("change", { bubbles: true });
        input.dispatchEvent(evt);
      }
    }
  });
})();
