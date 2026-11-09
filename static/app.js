"use strict";

const appToken = document.querySelector('meta[name="app-token"]').content;
const form = document.querySelector("#add-form");
const formMessage = document.querySelector("#form-message");
const productList = document.querySelector("#product-list");
const emptyState = document.querySelector("#empty-state");
const count = document.querySelector("#product-count");

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {
      "X-App-Token": appToken,
      ...(options.body ? { "Content-Type": "application/json" } : {}),
      ...options.headers
    }
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || `Request failed (${response.status}).`);
  return payload;
}

function formatTime(seconds) {
  if (!seconds) return "Not checked yet";
  return `Checked ${new Date(seconds * 1000).toLocaleString()}`;
}

function statusLabel(status) {
  if (status === "in_stock") return "In stock";
  if (status === "out_of_stock") return "Out of stock";
  return "Checking";
}

function createProductCard(product) {
  const card = document.createElement("article");
  card.className = "product-card";

  const main = document.createElement("div");
  main.className = "product-main";

  const titleWrap = document.createElement("div");
  const title = document.createElement("h3");
  title.className = "product-title";
  title.textContent = product.title || "Product";
  const link = document.createElement("a");
  link.className = "product-link";
  link.href = product.url;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  link.textContent = product.url;
  titleWrap.append(title, link);

  const status = document.createElement("span");
  status.className = `status ${product.status}`;
  status.textContent = statusLabel(product.status);
  main.append(titleWrap, status);

  const meta = document.createElement("div");
  meta.className = "card-meta";
  const checked = document.createElement("span");
  checked.textContent = formatTime(product.last_checked);
  meta.append(checked);
  if (product.price) {
    const price = document.createElement("span");
    price.textContent = `Detected price: ₹${product.price}`;
    meta.append(price);
  }
  if (product.source) {
    const source = document.createElement("span");
    source.textContent = `Detection: ${product.source}`;
    meta.append(source);
  }
  if (product.last_error) {
    const error = document.createElement("span");
    error.className = "card-error";
    error.textContent = product.last_error;
    meta.append(error);
  }

  const actions = document.createElement("div");
  actions.className = "card-actions";
  const checkButton = document.createElement("button");
  checkButton.type = "button";
  checkButton.textContent = "Check now";
  checkButton.addEventListener("click", async () => {
    checkButton.disabled = true;
    try {
      await api(`/api/products/${product.id}/check`, { method: "POST" });
      checkButton.textContent = "Check requested";
      window.setTimeout(loadProducts, 1500);
    } catch (error) {
      formMessage.textContent = error.message;
      checkButton.disabled = false;
    }
  });
  const removeButton = document.createElement("button");
  removeButton.type = "button";
  removeButton.className = "remove-button";
  removeButton.textContent = "Remove";
  removeButton.addEventListener("click", async () => {
    if (!window.confirm(`Stop tracking "${product.title}"?`)) return;
    try {
      await api(`/api/products/${product.id}`, { method: "DELETE" });
      await loadProducts();
    } catch (error) {
      formMessage.textContent = error.message;
    }
  });
  actions.append(checkButton, removeButton);

  card.append(main, meta, actions);
  return card;
}

async function loadProducts() {
  try {
    const data = await api("/api/products");
    productList.replaceChildren(...data.products.map(createProductCard));
    count.textContent = String(data.products.length);
    emptyState.hidden = data.products.length > 0;
    formMessage.textContent = "";
  } catch (error) {
    formMessage.textContent = error.message;
  }
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  formMessage.textContent = "";
  const submit = form.querySelector('button[type="submit"]');
  submit.disabled = true;
  const formData = new FormData(form);
  const payload = {
    url: formData.get("url"),
    in_stock_selector: formData.get("in_stock_selector"),
    out_of_stock_selector: formData.get("out_of_stock_selector"),
    price_selector: formData.get("price_selector")
  };
  try {
    await api("/api/products", { method: "POST", body: JSON.stringify(payload) });
    form.reset();
    document.querySelector(".advanced").open = false;
    await loadProducts();
    formMessage.textContent = "Added. The first check will run shortly.";
  } catch (error) {
    formMessage.textContent = error.message;
  } finally {
    submit.disabled = false;
  }
});

document.querySelector("#refresh-button").addEventListener("click", loadProducts);
loadProducts();
window.setInterval(loadProducts, 15_000);
