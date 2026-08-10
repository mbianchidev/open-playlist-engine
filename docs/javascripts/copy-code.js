const COPY_CONFIRMATION_MS = 2000;
const COPY_ICONS = {
  copy: `
    <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <rect x="9" y="9" width="11" height="11" rx="2"></rect>
      <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path>
    </svg>
  `,
  copied: `
    <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <path d="m5 12 4 4L19 6"></path>
    </svg>
  `,
  error: `
    <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <path d="M6 6l12 12M18 6 6 18"></path>
    </svg>
  `,
};

function copyWithFallback(text) {
  if (navigator.clipboard?.writeText) {
    return navigator.clipboard.writeText(text);
  }

  const textarea = document.createElement("textarea");
  textarea.value = text;
  textarea.setAttribute("readonly", "");
  textarea.style.position = "fixed";
  textarea.style.opacity = "0";
  document.body.append(textarea);
  textarea.select();

  try {
    if (!document.execCommand("copy")) {
      throw new Error("The browser rejected the clipboard command.");
    }
  } finally {
    textarea.remove();
  }

  return Promise.resolve();
}

function renderCopyState(button, icon, label) {
  button.querySelector("svg")?.remove();
  button.insertAdjacentHTML("afterbegin", COPY_ICONS[icon]);
  button.querySelector(".copy-code-status").textContent = label;
  button.title = label;
}

function setCopyState(button, icon, label, stateClass) {
  window.clearTimeout(button.copyStateTimeout);
  button.classList.remove("is-copied", "is-error");
  button.classList.add(stateClass);
  renderCopyState(button, icon, label);
  button.copyStateTimeout = window.setTimeout(() => {
    button.classList.remove(stateClass);
    renderCopyState(button, "copy", "Copy code to clipboard");
  }, COPY_CONFIRMATION_MS);
}

function addCodeCopyButtons() {
  document.querySelectorAll("pre > code").forEach((code) => {
    const block = code.parentElement;
    if (block.querySelector(".copy-code-button")) {
      return;
    }

    const button = document.createElement("button");
    button.type = "button";
    button.className = "copy-code-button";
    button.innerHTML = `${COPY_ICONS.copy}<span class="visually-hidden copy-code-status" aria-live="polite">Copy code to clipboard</span>`;
    button.title = "Copy code to clipboard";

    button.addEventListener("click", async () => {
      try {
        await copyWithFallback(code.textContent);
        setCopyState(button, "copied", "Copied to clipboard", "is-copied");
      } catch (error) {
        console.error("Unable to copy code block.", error);
        setCopyState(button, "error", "Copy failed", "is-error");
      }
    });

    block.append(button);
  });
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", addCodeCopyButtons);
} else {
  addCodeCopyButtons();
}
