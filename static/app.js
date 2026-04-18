const state = {
  selectedDate: new Date().toISOString().slice(0, 10),
  days: 14,
  foods: [],
  selectedFood: null,
  manualMode: false,
  authRequired: false,
  authenticated: false,
  username: "",
};

const registerServiceWorker = async () => {
  if (!("serviceWorker" in navigator)) {
    return;
  }

  try {
    await navigator.serviceWorker.register("/sw.js");
  } catch (error) {
    console.error("Service worker register failed", error);
  }
};

const refs = {
  selectedDate: document.querySelector("#selectedDate"),
  rangeTabs: document.querySelector("#rangeTabs"),
  mealForm: document.querySelector("#mealForm"),
  weightForm: document.querySelector("#weightForm"),
  foodQuery: document.querySelector("#foodQuery"),
  foodSuggestions: document.querySelector("#foodSuggestions"),
  foodBasisHint: document.querySelector("#foodBasisHint"),
  mealType: document.querySelector("#mealType"),
  mealAmount: document.querySelector("#mealAmount"),
  mealUnit: document.querySelector("#mealUnit"),
  manualMode: document.querySelector("#manualMode"),
  manualFields: document.querySelector("#manualFields"),
  manualCalories: document.querySelector("#manualCalories"),
  manualBasisAmount: document.querySelector("#manualBasisAmount"),
  manualBasisUnit: document.querySelector("#manualBasisUnit"),
  saveFoodRow: document.querySelector("#saveFoodRow"),
  saveCustomFood: document.querySelector("#saveCustomFood"),
  mealNote: document.querySelector("#mealNote"),
  calorieEstimate: document.querySelector("#calorieEstimate"),
  todayCalories: document.querySelector("#todayCalories"),
  mealCountText: document.querySelector("#mealCountText"),
  todayWeight: document.querySelector("#todayWeight"),
  latestWeightText: document.querySelector("#latestWeightText"),
  weeklyCalories: document.querySelector("#weeklyCalories"),
  weeklyWeightDelta: document.querySelector("#weeklyWeightDelta"),
  rangeWeightDelta: document.querySelector("#rangeWeightDelta"),
  mealBreakdown: document.querySelector("#mealBreakdown"),
  mealList: document.querySelector("#mealList"),
  weightValue: document.querySelector("#weightValue"),
  weightNote: document.querySelector("#weightNote"),
  recentWeights: document.querySelector("#recentWeights"),
  weightChart: document.querySelector("#weightChart"),
  toast: document.querySelector("#toast"),
  authOverlay: document.querySelector("#authOverlay"),
  loginForm: document.querySelector("#loginForm"),
  loginUsername: document.querySelector("#loginUsername"),
  loginPassword: document.querySelector("#loginPassword"),
  authMessage: document.querySelector("#authMessage"),
  sessionStatus: document.querySelector("#sessionStatus"),
  sessionMeta: document.querySelector("#sessionMeta"),
  logoutButton: document.querySelector("#logoutButton"),
};

const mealTypeLabels = {
  早餐: "早餐",
  午餐: "午餐",
  晚餐: "晚餐",
  加餐: "加餐",
};

const debounce = (callback, wait = 250) => {
  let timeout = 0;
  return (...args) => {
    window.clearTimeout(timeout);
    timeout = window.setTimeout(() => callback(...args), wait);
  };
};

const formatNumber = (value, digits = 1) => {
  if (value === null || value === undefined || Number.isNaN(Number(value))) {
    return "--";
  }
  return Number(value).toFixed(digits).replace(/\.0$/, "");
};

const formatDateLabel = (dateString) => {
  const date = new Date(`${dateString}T00:00:00`);
  return `${date.getMonth() + 1}/${date.getDate()}`;
};

const showToast = (message, isError = false) => {
  refs.toast.textContent = message;
  refs.toast.classList.remove("hidden", "toast-error");
  if (isError) {
    refs.toast.classList.add("toast-error");
  }

  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => {
    refs.toast.classList.add("hidden");
  }, 2800);
};

const setAuthMessage = (message, isError = false) => {
  refs.authMessage.textContent = message;
  refs.authMessage.classList.toggle("auth-message-error", isError);
};

const updateAuthUI = (session) => {
  state.authRequired = Boolean(session.auth_required);
  state.authenticated = Boolean(session.authenticated);
  state.username = session.username || "";

  refs.authOverlay.classList.toggle(
    "hidden",
    !(state.authRequired && !state.authenticated)
  );
  refs.logoutButton.classList.toggle("hidden", !state.authenticated || !state.authRequired);

  if (!state.authRequired) {
    refs.sessionStatus.textContent = "本地开放模式";
    refs.sessionMeta.textContent = `${session.deploy_mode} · ${session.storage_mode}`;
    return;
  }

  if (state.authenticated) {
    refs.sessionStatus.textContent = `已登录：${state.username}`;
    refs.sessionMeta.textContent = `${session.deploy_mode} · ${session.storage_mode} · 已开启登录保护`;
    setAuthMessage("登录状态有效，刷新页面后仍会保持。");
    return;
  }

  refs.sessionStatus.textContent = "需要登录";
  refs.sessionMeta.textContent = `${session.deploy_mode} · ${session.storage_mode} · 未授权`;
  refs.loginUsername.value = session.login_hint || "";
  refs.loginPassword.value = "";
  setAuthMessage("请输入 Render 上配置的账号和密码。");
};

const api = async (path, options = {}) => {
  const response = await fetch(path, {
    headers: {
      "Content-Type": "application/json",
    },
    ...options,
  });

  const payload = await response.json().catch(() => ({}));
  if (response.status === 401) {
    updateAuthUI({
      auth_required: true,
      authenticated: false,
      username: "",
      login_hint: refs.loginUsername.value.trim(),
      deploy_mode: "Render",
      storage_mode: refs.sessionMeta.textContent || "",
    });
    throw new Error(payload.error || "请先登录");
  }

  if (!response.ok) {
    throw new Error(payload.error || "请求失败");
  }
  return payload;
};

const fetchSession = async () => {
  const session = await api("/api/session");
  updateAuthUI(session);
  return session;
};

const renderFoodSuggestions = (foods) => {
  state.foods = foods;
  if (!foods.length || state.manualMode) {
    refs.foodSuggestions.classList.add("hidden");
    refs.foodSuggestions.innerHTML = "";
    return;
  }

  refs.foodSuggestions.innerHTML = foods
    .map(
      (food) => `
        <button class="suggestion-item" type="button" data-food-id="${food.id}">
          <span>${food.name}</span>
          <small>${formatNumber(food.calories, 0)} kcal / ${formatNumber(food.basis_amount)} ${food.basis_unit}</small>
        </button>
      `
    )
    .join("");

  refs.foodSuggestions.classList.remove("hidden");
};

const describeFoodBasis = (food) =>
  `${formatNumber(food.calories, 0)} kcal / ${formatNumber(food.basis_amount)} ${food.basis_unit}`;

const updateFoodSelection = (food) => {
  state.selectedFood = food;
  refs.foodQuery.value = food.name;
  refs.foodBasisHint.textContent = `参考热量：${describeFoodBasis(food)}`;
  refs.mealUnit.value = food.basis_unit;
  refs.manualCalories.value = food.calories;
  refs.manualBasisAmount.value = food.basis_amount;
  refs.manualBasisUnit.value = food.basis_unit;
  refs.foodSuggestions.classList.add("hidden");
  updateCalorieEstimate();
};

const clearFoodSelection = () => {
  state.selectedFood = null;
  refs.foodBasisHint.textContent = state.manualMode ? "手动填写热量基准后即可计算" : "先搜索并选择一个食物";
  refs.mealUnit.value = state.manualMode ? refs.manualBasisUnit.value : "";
  updateCalorieEstimate();
};

const updateManualMode = () => {
  state.manualMode = refs.manualMode.checked;
  refs.manualFields.classList.toggle("hidden", !state.manualMode);
  refs.saveFoodRow.classList.toggle("hidden", !state.manualMode);

  if (state.manualMode) {
    refs.foodSuggestions.classList.add("hidden");
    refs.foodBasisHint.textContent = "手动填写热量基准后即可计算";
    refs.mealUnit.value = refs.manualBasisUnit.value;
  } else {
    refs.saveCustomFood.checked = false;
    clearFoodSelection();
  }
  updateCalorieEstimate();
};

const calculateEstimate = () => {
  const amount = Number(refs.mealAmount.value);
  const calories = Number(refs.manualCalories.value);
  const basisAmount = Number(refs.manualBasisAmount.value);

  if (!amount || !calories || !basisAmount) {
    return 0;
  }
  return (amount / basisAmount) * calories;
};

const updateCalorieEstimate = () => {
  if (!state.manualMode && !state.selectedFood) {
    refs.calorieEstimate.textContent = "0 kcal";
    return;
  }

  refs.mealUnit.value = refs.manualBasisUnit.value || refs.mealUnit.value;
  const estimate = calculateEstimate();
  refs.calorieEstimate.textContent = `${formatNumber(estimate, 0)} kcal`;
};

const searchFoods = debounce(async (query) => {
  if (state.manualMode || (state.authRequired && !state.authenticated)) {
    return;
  }
  try {
    const payload = await api(`/api/foods?query=${encodeURIComponent(query)}`);
    renderFoodSuggestions(payload.foods || []);
  } catch (error) {
    showToast(error.message, true);
  }
}, 220);

const renderBreakdown = (breakdown) => {
  if (!breakdown.length) {
    refs.mealBreakdown.innerHTML = "";
    return;
  }

  refs.mealBreakdown.innerHTML = breakdown
    .map(
      (item) => `
        <span class="breakdown-pill">
          ${mealTypeLabels[item.meal_type] || item.meal_type}
          <strong>${formatNumber(item.total, 0)} kcal</strong>
        </span>
      `
    )
    .join("");
};

const renderMeals = (meals) => {
  if (!meals.length) {
    refs.mealList.className = "meal-list empty-state";
    refs.mealList.textContent = "这一天还没有饮食记录";
    return;
  }

  refs.mealList.className = "meal-list";
  refs.mealList.innerHTML = meals
    .map(
      (meal) => `
        <article class="meal-item">
          <div class="meal-item-top">
            <div>
              <div class="meal-title-row">
                <span class="meal-type-badge">${meal.meal_type}</span>
                <strong>${meal.food_name}</strong>
              </div>
              <p>${formatNumber(meal.amount)} ${meal.basis_unit} · ${formatNumber(meal.total_calories, 0)} kcal</p>
              ${
                meal.note
                  ? `<p class="meal-note">${meal.note}</p>`
                  : `<p class="meal-note muted">无备注</p>`
              }
            </div>
            <button class="ghost-button" type="button" data-delete-meal="${meal.id}">删除</button>
          </div>
        </article>
      `
    )
    .join("");
};

const renderRecentWeights = (weights) => {
  if (!weights.length) {
    refs.recentWeights.className = "list-shell empty-state";
    refs.recentWeights.textContent = "还没有体重记录";
    return;
  }

  refs.recentWeights.className = "list-shell";
  refs.recentWeights.innerHTML = weights
    .map(
      (item) => `
        <div class="list-row">
          <div>
            <strong>${formatNumber(item.weight)} kg</strong>
            <p>${item.record_date}${item.note ? ` · ${item.note}` : ""}</p>
          </div>
          <button class="ghost-button" type="button" data-delete-weight="${item.record_date}">删除</button>
        </div>
      `
    )
    .join("");
};

const buildChartMarkup = (weights) => {
  if (weights.length < 2) {
    return `<div class="empty-state">至少记录两次体重后，这里会显示完整曲线</div>`;
  }

  const width = 720;
  const height = 320;
  const padding = { top: 28, right: 24, bottom: 48, left: 54 };
  const values = weights.map((item) => Number(item.weight));
  const min = Math.min(...values);
  const max = Math.max(...values);
  const range = Math.max(max - min, 1);

  const points = weights.map((item, index) => {
    const x =
      padding.left +
      (index / Math.max(weights.length - 1, 1)) * (width - padding.left - padding.right);
    const y =
      height -
      padding.bottom -
      ((Number(item.weight) - min) / range) * (height - padding.top - padding.bottom);
    return { x, y, item };
  });

  const polyline = points.map((point) => `${point.x},${point.y}`).join(" ");
  const gridValues = [0, 0.25, 0.5, 0.75, 1].map((ratio) => ({
    y: height - padding.bottom - ratio * (height - padding.top - padding.bottom),
    label: formatNumber(min + ratio * range, 1),
  }));

  const xLabelStep = Math.max(1, Math.ceil(points.length / 5));

  return `
    <svg viewBox="0 0 ${width} ${height}" class="chart-svg" aria-label="体重曲线图">
      <defs>
        <linearGradient id="weightLineGradient" x1="0%" y1="0%" x2="100%" y2="0%">
          <stop offset="0%" stop-color="#ff8a3d"></stop>
          <stop offset="100%" stop-color="#2f8f83"></stop>
        </linearGradient>
      </defs>

      ${gridValues
        .map(
          (tick) => `
            <line class="chart-grid" x1="${padding.left}" y1="${tick.y}" x2="${width - padding.right}" y2="${tick.y}"></line>
            <text class="chart-axis-label" x="${padding.left - 10}" y="${tick.y + 4}" text-anchor="end">${tick.label}</text>
          `
        )
        .join("")}

      <polyline class="chart-line-shadow" points="${polyline}"></polyline>
      <polyline class="chart-line" points="${polyline}"></polyline>

      ${points
        .map(
          (point, index) => `
            <circle class="chart-point" cx="${point.x}" cy="${point.y}" r="4.8"></circle>
            ${
              index % xLabelStep === 0 || index === points.length - 1
                ? `<text class="chart-axis-label" x="${point.x}" y="${height - 18}" text-anchor="middle">${formatDateLabel(
                    point.item.record_date
                  )}</text>`
                : ""
            }
            <text class="chart-value-label" x="${point.x}" y="${point.y - 14}" text-anchor="middle">${formatNumber(
              point.item.weight
            )}</text>
          `
        )
        .join("")}
    </svg>
  `;
};

const renderWeightChart = (weights) => {
  refs.weightChart.className = "chart-shell";
  refs.weightChart.innerHTML = buildChartMarkup(weights);
};

const renderSummary = (dashboard) => {
  refs.todayCalories.textContent = `${formatNumber(dashboard.today.total_calories, 0)} kcal`;
  refs.mealCountText.textContent = `${dashboard.today.meal_count} 条饮食记录`;
  refs.todayWeight.textContent = dashboard.today.weight
    ? `${formatNumber(dashboard.today.weight.weight)} kg`
    : "未记录";

  refs.latestWeightText.textContent = dashboard.stats.latest_weight
    ? `最近一次：${dashboard.stats.latest_weight.record_date} · ${formatNumber(
        dashboard.stats.latest_weight.weight
      )} kg`
    : "暂无体重历史";

  refs.weeklyCalories.textContent = `${formatNumber(dashboard.stats.average_calories_7d, 0)} kcal`;
  refs.weeklyWeightDelta.textContent =
    dashboard.stats.weight_change_7d === null
      ? "暂无"
      : `${dashboard.stats.weight_change_7d > 0 ? "+" : ""}${formatNumber(
          dashboard.stats.weight_change_7d
        )} kg`;

  refs.rangeWeightDelta.textContent =
    dashboard.stats.weight_change_in_range === null
      ? `近 ${state.days} 天记录不足`
      : `近 ${state.days} 天：${
          dashboard.stats.weight_change_in_range > 0 ? "+" : ""
        }${formatNumber(dashboard.stats.weight_change_in_range)} kg`;
};

const fillWeightForm = (weightRecord) => {
  refs.weightValue.value = weightRecord ? formatNumber(weightRecord.weight) : "";
  refs.weightNote.value = weightRecord?.note || "";
};

const fetchDashboard = async () => {
  const dashboard = await api(
    `/api/dashboard?date=${encodeURIComponent(state.selectedDate)}&days=${state.days}`
  );

  renderSummary(dashboard);
  renderBreakdown(dashboard.today.breakdown);
  renderMeals(dashboard.meals);
  renderRecentWeights(dashboard.recent_weights);
  renderWeightChart(dashboard.weight_history);
  fillWeightForm(dashboard.today.weight);
};

const buildMealPayload = () => {
  const foodName = refs.foodQuery.value.trim();
  const amount = Number(refs.mealAmount.value);
  const manualCalories = Number(refs.manualCalories.value);
  const manualBasisAmount = Number(refs.manualBasisAmount.value);
  const manualBasisUnit = refs.manualBasisUnit.value.trim();

  if (!foodName) {
    throw new Error("请先填写食物名称");
  }

  if (!state.manualMode && !state.selectedFood) {
    throw new Error("请先从搜索结果里选择一个食物，或者切换到手动热量模式");
  }

  const source = state.manualMode
    ? {
        calories: manualCalories,
        basis_amount: manualBasisAmount,
        basis_unit: manualBasisUnit,
      }
    : {
        calories: state.selectedFood.calories,
        basis_amount: state.selectedFood.basis_amount,
        basis_unit: state.selectedFood.basis_unit,
      };

  return {
    record_date: state.selectedDate,
    meal_type: refs.mealType.value,
    food_name: foodName,
    amount,
    basis_amount: source.basis_amount,
    basis_unit: source.basis_unit,
    calories_per_basis: source.calories,
    note: refs.mealNote.value.trim(),
    save_custom_food: state.manualMode && refs.saveCustomFood.checked,
  };
};

const resetMealForm = () => {
  refs.mealForm.reset();
  refs.mealType.value = "早餐";
  refs.mealAmount.value = "";
  refs.foodQuery.value = "";
  refs.mealUnit.value = "";
  refs.foodSuggestions.classList.add("hidden");
  refs.foodSuggestions.innerHTML = "";
  refs.manualMode.checked = false;
  updateManualMode();
  clearFoodSelection();
};

const handleMealSubmit = async (event) => {
  event.preventDefault();
  try {
    const payload = buildMealPayload();
    await api("/api/meals", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    showToast("饮食记录已保存");
    resetMealForm();
    await fetchDashboard();
  } catch (error) {
    showToast(error.message, true);
  }
};

const handleWeightSubmit = async (event) => {
  event.preventDefault();
  try {
    await api("/api/weights", {
      method: "POST",
      body: JSON.stringify({
        record_date: state.selectedDate,
        weight: Number(refs.weightValue.value),
        note: refs.weightNote.value.trim(),
      }),
    });
    showToast("体重已保存");
    await fetchDashboard();
  } catch (error) {
    showToast(error.message, true);
  }
};

const handleDeleteClick = async (event) => {
  const mealId = event.target.dataset.deleteMeal;
  const weightDate = event.target.dataset.deleteWeight;

  if (mealId) {
    try {
      await api(`/api/meals/${mealId}`, { method: "DELETE" });
      showToast("饮食记录已删除");
      await fetchDashboard();
    } catch (error) {
      showToast(error.message, true);
    }
  }

  if (weightDate) {
    try {
      await api(`/api/weights/${weightDate}`, { method: "DELETE" });
      showToast("体重记录已删除");
      await fetchDashboard();
    } catch (error) {
      showToast(error.message, true);
    }
  }
};

const handleLoginSubmit = async (event) => {
  event.preventDefault();
  try {
    setAuthMessage("正在验证账号密码...");
    const session = await api("/api/login", {
      method: "POST",
      body: JSON.stringify({
        username: refs.loginUsername.value.trim(),
        password: refs.loginPassword.value,
      }),
    });
    updateAuthUI(session);
    showToast("登录成功");
    await fetchDashboard();
  } catch (error) {
    setAuthMessage(error.message, true);
  }
};

const handleLogout = async () => {
  try {
    const session = await api("/api/logout", {
      method: "POST",
      body: JSON.stringify({}),
    });
    updateAuthUI(session);
    refs.loginPassword.value = "";
    showToast("已退出登录");
  } catch (error) {
    showToast(error.message, true);
  }
};

const bindEvents = () => {
  refs.selectedDate.value = state.selectedDate;
  refs.selectedDate.addEventListener("change", async (event) => {
    state.selectedDate = event.target.value;
    if (!state.authRequired || state.authenticated) {
      await fetchDashboard();
    }
  });

  refs.rangeTabs.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-days]");
    if (!button) {
      return;
    }
    state.days = Number(button.dataset.days);
    refs.rangeTabs
      .querySelectorAll(".tab-button")
      .forEach((tab) => tab.classList.toggle("is-active", tab === button));
    if (!state.authRequired || state.authenticated) {
      await fetchDashboard();
    }
  });

  refs.foodQuery.addEventListener("input", (event) => {
    if (state.manualMode) {
      return;
    }
    clearFoodSelection();
    searchFoods(event.target.value.trim());
  });

  refs.foodQuery.addEventListener("focus", () => {
    if (!state.foods.length && !state.manualMode) {
      searchFoods("");
    }
  });

  document.addEventListener("click", (event) => {
    const item = event.target.closest("[data-food-id]");
    if (item) {
      const selectedFood = state.foods.find((food) => String(food.id) === item.dataset.foodId);
      if (selectedFood) {
        updateFoodSelection(selectedFood);
      }
      return;
    }

    if (!event.target.closest(".food-search-field")) {
      refs.foodSuggestions.classList.add("hidden");
    }
  });

  refs.manualMode.addEventListener("change", updateManualMode);
  refs.mealAmount.addEventListener("input", updateCalorieEstimate);
  refs.manualCalories.addEventListener("input", updateCalorieEstimate);
  refs.manualBasisAmount.addEventListener("input", updateCalorieEstimate);
  refs.manualBasisUnit.addEventListener("input", () => {
    refs.mealUnit.value = refs.manualBasisUnit.value.trim();
    updateCalorieEstimate();
  });

  refs.mealForm.addEventListener("submit", handleMealSubmit);
  refs.weightForm.addEventListener("submit", handleWeightSubmit);
  refs.mealList.addEventListener("click", handleDeleteClick);
  refs.recentWeights.addEventListener("click", handleDeleteClick);
  refs.loginForm.addEventListener("submit", handleLoginSubmit);
  refs.logoutButton.addEventListener("click", handleLogout);
};

const init = async () => {
  await registerServiceWorker();
  bindEvents();
  updateManualMode();

  try {
    const session = await fetchSession();
    if (!session.auth_required || session.authenticated) {
      await fetchDashboard();
    }
  } catch (error) {
    showToast(error.message, true);
  }
};

init();
