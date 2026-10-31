// Table sorting, auto-submit filters, auto refresh of the overview (CSP: own scripts only, no inline handlers).
(function () {
  document.querySelectorAll("table.sortable").forEach((table) => {
    const body = table.tBodies[0];
    if (!body) return;
    const val = (td) => td && td.dataset.v !== undefined ? Number(td.dataset.v) : (td ? td.textContent.trim().toLowerCase() : "");
    table.querySelectorAll("thead th").forEach((th, i) => {
      th.classList.add("sort");
      th.addEventListener("click", () => {
        const dir = th.dataset.dir === "desc" ? "asc" : "desc";
        table.querySelectorAll("thead th").forEach((x) => delete x.dataset.dir);
        th.dataset.dir = dir;
        const rows = Array.from(body.rows);
        rows.sort((a, b) => {
          let x = val(a.cells[i]), y = val(b.cells[i]);
          if (typeof x !== "number") { const nx = parseFloat(x), ny = parseFloat(y); if (!isNaN(nx) && !isNaN(ny) && /^[\d.\s%]+$/.test(x + y)) { x = nx; y = ny; } }
          const r = typeof x === "number" ? x - y : String(x).localeCompare(String(y), "ru");
          return dir === "asc" ? r : -r;
        });
        rows.forEach((r) => body.appendChild(r));
      });
    });
  });
  document.querySelectorAll("select[data-autosubmit]").forEach((s) => s.addEventListener("change", () => s.form.submit()));
  const live = document.querySelector("[data-refresh]");
  if (live) {
    let busy = Date.now();
    ["input", "change", "click", "keydown"].forEach((e) => document.addEventListener(e, () => { busy = Date.now(); }));
    const sec = Number(live.dataset.refresh) || 60;
    setInterval(() => {
      if (!document.hidden && Date.now() - busy > sec * 1000 && !document.querySelector("details[open]")) location.reload();
    }, sec * 1000);
  }
})();
