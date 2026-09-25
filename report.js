(function () {
  const filters = document.getElementById("filters");
  if (!filters) return;
  const buttons = filters.querySelectorAll("button");
  const cards = Array.from(document.querySelectorAll(".card[data-cat]"));

  function apply(f) {
    cards.forEach((card) => {
      const cats = (card.getAttribute("data-cat") || "").split(/\s+/);
      const show = f === "all" || cats.includes(f);
      card.classList.toggle("hidden", !show);
    });
    buttons.forEach((b) => {
      b.classList.toggle("active", b.getAttribute("data-f") === f);
    });
  }

  filters.addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-f]");
    if (!btn) return;
    apply(btn.getAttribute("data-f"));
  });

  // deep-link ?f=p0
  const q = new URLSearchParams(location.search).get("f");
  if (q) apply(q);
})();
