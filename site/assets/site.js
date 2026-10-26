// Shared page behaviour: theme toggle, scroll reveals, the hero decision
// animation, the recorded-decision ticker, and copy buttons.
document.documentElement.classList.add("js");

const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

// Theme: follows the OS until the visitor picks one, then remembers it.
const root = document.documentElement;
const darkQuery = window.matchMedia("(prefers-color-scheme: dark)");
const currentTheme = () => root.dataset.theme || (darkQuery.matches ? "dark" : "light");
document.querySelectorAll("[data-theme-toggle]").forEach((button) => {
  const sync = () => button.setAttribute("aria-label", `Switch to ${currentTheme() === "dark" ? "light" : "dark"} theme`);
  sync();
  darkQuery.addEventListener("change", sync);
  button.addEventListener("click", () => {
    root.dataset.theme = currentTheme() === "dark" ? "light" : "dark";
    try {
      localStorage.setItem("scopewatch-theme", root.dataset.theme);
    } catch {
      // Storage can be blocked; the choice then lasts for this page view only.
    }
    sync();
  });
});

const revealTargets = document.querySelectorAll(".reveal");
if (reduceMotion || !("IntersectionObserver" in window)) {
  revealTargets.forEach((el) => el.classList.add("in"));
} else {
  const observer = new IntersectionObserver(
    (entries) => {
      for (const entry of entries) {
        if (entry.isIntersecting) {
          entry.target.classList.add("in");
          observer.unobserve(entry.target);
        }
      }
    },
    { rootMargin: "0px 0px -6% 0px", threshold: 0.05 },
  );
  revealTargets.forEach((el) => observer.observe(el));
}

// Hero console: light the four checkpoints in order, then stamp the verdict.
const heroStages = document.querySelectorAll("#hero-stages .stage");
const heroStamp = document.getElementById("hero-stamp");
if (heroStages.length) {
  if (reduceMotion) {
    heroStages.forEach((el) => el.classList.add("on"));
    heroStamp?.classList.add("on");
  } else {
    let step = 0;
    const tick = () => {
      if (step === 0) {
        heroStages.forEach((el) => el.classList.remove("on"));
        heroStamp?.classList.remove("on");
      }
      if (step < heroStages.length) heroStages[step].classList.add("on");
      if (step === heroStages.length - 2) heroStamp?.classList.add("on");
      step = (step + 1) % (heroStages.length + 4);
      setTimeout(tick, step === 0 ? 400 : 900);
    };
    setTimeout(tick, 500);
  }
}

// Ticker: every action the real gateway recorded for the demo scenarios.
// The markup ships a static fallback; this replaces it with the full record.
const ticker = document.getElementById("ticker-track");
if (ticker) {
  fetch("assets/demo-data.json")
    .then((response) => (response.ok ? response.json() : Promise.reject(response.status)))
    .then((data) => {
      const kind = { ALLOW: "allow", DENY: "deny", HOLD: "hold" };
      const items = data.runs.flatMap((run) =>
        run.actions.map((action) => {
          const outcome = action.gateway_decision.outcome;
          const li = document.createElement("li");
          li.className = "tick";
          const op = document.createElement("span");
          op.className = "op";
          op.textContent = action.operation;
          const res = document.createElement("span");
          res.className = "res";
          res.textContent = action.resource;
          res.title = action.resource;
          const pill = document.createElement("span");
          pill.className = `pill ${kind[outcome] || "muted"}`;
          pill.textContent = `${outcome} · ${action.gateway_decision.reason_code}`;
          li.append(op, res, pill);
          return li;
        }),
      );
      if (!items.length) return;
      // Two copies so the marquee loops without a seam; the copy is hidden from screen readers.
      const copies = items.map((li) => {
        const clone = li.cloneNode(true);
        clone.setAttribute("aria-hidden", "true");
        return clone;
      });
      ticker.replaceChildren(...items, ...(reduceMotion ? [] : copies));
    })
    .catch(() => {
      // Keep the static fallback when the JSON can't be fetched (file:// preview).
    });
}

document.querySelectorAll("[data-copy]").forEach((button) => {
  button.addEventListener("click", async () => {
    const source = document.getElementById(button.dataset.copy);
    if (!source) return;
    try {
      await navigator.clipboard.writeText(source.textContent.replace(/^\$ /gm, ""));
      button.textContent = "Copied";
    } catch {
      const range = document.createRange();
      range.selectNodeContents(source);
      const selection = window.getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
      button.textContent = "Selected, press Ctrl+C";
    }
    setTimeout(() => (button.textContent = "Copy"), 1800);
  });
});
