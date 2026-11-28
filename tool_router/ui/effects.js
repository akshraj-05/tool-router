/* Movement only: the page works the same without this file.
 *
 * The colours and most animation are in style.css. Here is what needs a script.
 * Everything that moves is skipped when the visitor asks for reduced motion; the
 * menu marker is not movement, so it always runs. */
"use strict";
(() => {
  const still = window.matchMedia("(prefers-reduced-motion: reduce)");
  const sections = [...document.querySelectorAll("main section")];
  const links = [...document.querySelectorAll("nav a")];

  // The menu shows which section is on screen.
  function markMenu() {
    const passed = sections.filter((s) => s.getBoundingClientRect().top < 160);
    const atEnd = window.scrollY > 0 &&
      window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 4;
    const here = atEnd ? sections[sections.length - 1] : passed.pop() || sections[0];
    links.forEach((link) => link.classList.toggle("here", link.hash === `#${here.id}`));
  }

  // A line along the top shows how far down the page you are.
  const progress = document.createElement("div");
  progress.className = "progress";
  progress.setAttribute("aria-hidden", "true");
  document.body.append(progress);
  function onScroll() {
    markMenu();
    const room = document.documentElement.scrollHeight - window.innerHeight;
    progress.style.transform = `scaleX(${room > 0 ? Math.min(1, window.scrollY / room) : 0})`;
  }
  window.addEventListener("scroll", onScroll, { passive: true });
  window.addEventListener("resize", onScroll, { passive: true });
  // The page also grows when its data arrives.
  new ResizeObserver(onScroll).observe(document.body);

  // Parts of the page appear as they are scrolled into view.
  if (!still.matches) {
    const appear = new IntersectionObserver((entries) => entries.forEach((entry) => {
      if (!entry.isIntersecting) return;
      entry.target.classList.add("seen");
      appear.unobserve(entry.target);
    }));
    const changing = new Set(["empty", "answer", "found", "preview", "add-message", "more"]);
    document.querySelectorAll("main section > *").forEach((part) => {
      if (changing.has(part.id)) return;
      part.classList.add("reveal");
      appear.observe(part);
    });
  }

  // A light that follows the pointer over cards.
  document.addEventListener("pointermove", (event) => {
    const card = event.target.closest?.("main .card");
    if (!card) return;
    const box = card.getBoundingClientRect();
    card.style.setProperty("--mx", `${event.clientX - box.left}px`);
    card.style.setProperty("--my", `${event.clientY - box.top}px`);
  }, { passive: true });

  // A ripple from the point that was clicked.
  document.addEventListener("click", (event) => {
    const button = event.target.closest("button:not(.link), .button");
    if (!button || still.matches || !event.clientX) return;       // no ripple for the keyboard
    const box = button.getBoundingClientRect();
    const size = Math.max(box.width, box.height);
    const ripple = document.createElement("span");
    ripple.className = "ripple";
    ripple.style.width = ripple.style.height = `${size}px`;
    ripple.style.left = `${event.clientX - box.left - size / 2}px`;
    ripple.style.top = `${event.clientY - box.top - size / 2}px`;
    button.append(ripple);
    setTimeout(() => ripple.remove(), 650);
  });

  // Dashboard numbers count up when they change.
  const counted = new Map();
  function countUp(node, from) {
    const text = node.textContent;
    const found = text.match(/^([\d,]+(?:\.\d+)?)(.*)$/);
    if (!found) return;
    const target = Number(found[1].replace(/,/g, ""));
    if (target === from) return;
    const places = (found[1].split(".")[1] || "").length;
    const start = performance.now();
    const step = (now) => {
      const part = Math.min(1, Math.max(0, (now - start) / 800));
      const value = from + (target - from) * (1 - (1 - part) ** 3);
      node.textContent = part === 1 ? text
        : value.toLocaleString("en-US", { minimumFractionDigits: places, maximumFractionDigits: places }) + found[2];
      if (part < 1) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  }
  for (const id of ["registry-numbers", "request-numbers"]) {
    new MutationObserver(() => {
      document.querySelectorAll(`#${id} .card`).forEach((card) => {
        const node = card.querySelector("strong");
        const key = `${id} ${card.querySelector("span").textContent}`;
        const now = Number((node.textContent.match(/^[\d,.]+/) || ["0"])[0].replace(/,/g, "")) || 0;
        if (!still.matches) countUp(node, counted.get(key) || 0);
        counted.set(key, now);
      });
    }).observe(document.getElementById(id), { childList: true });
  }

  // A short burst of confetti after tools are saved.
  const message = document.getElementById("add-message");
  new MutationObserver(() => {
    if (still.matches || message.hidden || !message.classList.contains("good")) return;
    const box = message.getBoundingClientRect();
    const colours = ["#8f80ff", "#e879f9", "#67e8f9", "#55d99a", "#ffb454"];
    for (let i = 0; i < 28; i += 1) {
      const piece = document.createElement("span");
      piece.className = "confetti";
      piece.setAttribute("aria-hidden", "true");
      piece.style.background = colours[i % colours.length];
      piece.style.left = `${box.left + box.width / 2}px`;
      piece.style.top = `${Math.max(80, box.top)}px`;
      const angle = (Math.PI * 2 * i) / 28;
      const reach = 140 + ((i * 53) % 190);
      piece.style.setProperty("--dx", `${Math.cos(angle) * reach}px`);
      piece.style.setProperty("--dy", `${Math.sin(angle) * reach * 0.8 + 60}px`);
      piece.style.setProperty("--turn", `${((i * 97) % 360) - 180}deg`);
      document.body.append(piece);
      setTimeout(() => piece.remove(), 1400);
    }
  }).observe(message, { attributes: true, childList: true });

  onScroll();
})();
