"use strict";

const ONBOARD_KEY = "dashboard_onboarding_v1";

function initOnboarding() {
  if (localStorage.getItem(ONBOARD_KEY) === "1") return;
  const steps = [
    {
      title: "Сигналы",
      body: "Здесь лента планов от бота: направление, вход, стоп и цели. Нажмите карточку — откроется разбор с графиком.",
    },
    {
      title: "Отслеживание",
      body: "Сигналы, которые бот уже ведёт: прогресс до цели, текущая цена и статус (ждём входа / в сделке).",
    },
    {
      title: "Дневник",
      body: "Записывайте, взяли ли вы сигнал и чем закончилась сделка — для личной статистики, не для автоторговли.",
    },
  ];
  let step = 0;

  const overlay = el("div", { class: "modal-overlay onboarding-overlay" });
  const card = el("div", { class: "modal onboarding-card" });
  const title = el("h2", { text: "" });
  const body = el("p", { class: "muted", style: "line-height:1.55", text: "" });
  const dots = el("div", { class: "onboarding-dots" });
  const btn = el("button", { type: "button", class: "primary", text: "Далее" });

  function renderStep() {
    title.textContent = steps[step].title;
    body.textContent = steps[step].body;
    btn.textContent = step < steps.length - 1 ? "Далее" : "Понятно";
    dots.replaceChildren(
      ...steps.map((_, i) =>
        el("span", {
          class: "onboarding-dot" + (i === step ? " active" : ""),
        })
      )
    );
  }

  btn.addEventListener("click", () => {
    if (step < steps.length - 1) {
      step += 1;
      renderStep();
      return;
    }
    localStorage.setItem(ONBOARD_KEY, "1");
    overlay.remove();
  });

  card.appendChild(title);
  card.appendChild(body);
  card.appendChild(dots);
  card.appendChild(el("div", { class: "modal-buttons", style: "justify-content:flex-end" }, [btn]));
  overlay.appendChild(card);
  renderStep();
  document.body.appendChild(overlay);
}

document.addEventListener("DOMContentLoaded", () => {
  setTimeout(initOnboarding, 600);
});
