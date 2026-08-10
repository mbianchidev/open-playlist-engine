const COPY_CONFIRMATION_MS = 2000;

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

function setCopyState(button, label, stateClass) {
  window.clearTimeout(button.copyStateTimeout);
  button.textContent = label;
  button.classList.remove("is-copied", "is-error");
  button.classList.add(stateClass);
  button.copyStateTimeout = window.setTimeout(() => {
    button.textContent = "Copy";
    button.classList.remove(stateClass);
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
    button.textContent = "Copy";
    button.setAttribute("aria-label", "Copy code to clipboard");
    button.setAttribute("aria-live", "polite");

    button.addEventListener("click", async () => {
      try {
        await copyWithFallback(code.textContent);
        setCopyState(button, "Copied", "is-copied");
      } catch (error) {
        console.error("Unable to copy code block.", error);
        setCopyState(button, "Copy failed", "is-error");
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
