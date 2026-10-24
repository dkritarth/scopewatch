// Shared page behaviour: scroll reveals, the hero decision animation, copy buttons.
document.documentElement.classList.remove("no-js");

const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

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
    { rootMargin: "0px 0px -8% 0px", threshold: 0.08 },
  );
  revealTargets.forEach((el) => observer.observe(el));
}

const heroStages = document.querySelectorAll("#hero-stages .stage");
if (heroStages.length) {
  if (reduceMotion) {
    heroStages.forEach((el) => el.classList.add("on"));
  } else {
    let step = 0;
    const tick = () => {
      if (step === 0) heroStages.forEach((el) => el.classList.remove("on"));
      if (step < heroStages.length) heroStages[step].classList.add("on");
      step = (step + 1) % (heroStages.length + 3);
      setTimeout(tick, step === 0 ? 400 : 900);
    };
    setTimeout(tick, 500);
  }
}

document.querySelectorAll("[data-copy]").forEach((button) => {
  button.addEventListener("click", async () => {
    const source = document.getElementById(button.dataset.copy);
    if (!source) return;
    try {
      await navigator.clipboard.writeText(source.textContent);
      button.textContent = "Copied";
    } catch {
      button.textContent = "Select and copy";
    }
    setTimeout(() => (button.textContent = "Copy"), 1600);
  });
});
