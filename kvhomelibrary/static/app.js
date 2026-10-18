(function () {
  "use strict";

  const modal = document.getElementById("modal");
  const modalBody = document.getElementById("modal-body");
  const modalError = document.getElementById("modal-error");
  const toast = document.getElementById("toast");
  let toastTimer = null;

  function showToast(message, isError) {
    if (!toast) return;
    toast.textContent = message || "";
    toast.classList.toggle("error", Boolean(isError));
    toast.hidden = false;
    if (toastTimer) window.clearTimeout(toastTimer);
    toastTimer = window.setTimeout(function () {
      toast.hidden = true;
    }, isError ? 7000 : 4000);
  }

  function setModalError(message) {
    if (!modalError) return;
    modalError.textContent = message || "";
    modalError.hidden = !message;
  }

  function setDownloadError(form, message) {
    let box = form.querySelector(".download-error");
    if (!box) {
      box = document.createElement("div");
      box.className = "download-error form-error";
      box.setAttribute("role", "alert");
      form.appendChild(box);
    }
    box.textContent = message || "";
    box.hidden = !message;
  }

  function openModal() {
    if (!modal) return;
    modal.hidden = false;
    modal.setAttribute("aria-hidden", "false");
    document.body.classList.add("modal-open");
  }

  function closeModal() {
    if (!modal) return;
    modal.hidden = true;
    modal.setAttribute("aria-hidden", "true");
    document.body.classList.remove("modal-open");
    if (modalBody) modalBody.innerHTML = "";
    setModalError("");
  }

  async function postForm(form, submitter) {
    const data = new URLSearchParams();
    data.append("ajax", "1");
    new FormData(form).forEach(function (value, key) {
      if (typeof value === "string") data.append(key, value);
    });
    if (submitter && submitter.name) {
      data.append(submitter.name, submitter.value);
    }
    const response = await fetch(form.action, {
      method: "POST",
      body: data,
      headers: {
        "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
        "Accept": "application/json",
        "X-Requested-With": "XMLHttpRequest"
      }
    });
    let payload = null;
    try {
      payload = await response.json();
    } catch (error) {
      payload = null;
    }
    if (!response.ok || !payload || !payload.ok) {
      const message =
        (payload && payload.error) ||
        "Сервер вернул ошибку " + response.status + ". Попробуйте ещё раз.";
      throw new Error(message);
    }
    return payload;
  }

  async function openEdit(link) {
    if (!modal || !modalBody) return;
    setModalError("");
    modalBody.innerHTML = '<div class="muted">Загрузка формы…</div>';
    openModal();
    try {
      const url = new URL(link.href, window.location.origin);
      url.searchParams.set("ajax", "1");
      const response = await fetch(url.toString(), {
        headers: {
          "Accept": "text/html",
          "X-Requested-With": "XMLHttpRequest"
        }
      });
      if (!response.ok) {
        throw new Error("Не удалось открыть форму редактирования (" + response.status + ").");
      }
      modalBody.innerHTML = await response.text();
      const form = modalBody.querySelector("form");
      if (!form) throw new Error("Форма редактирования не найдена.");

      let clickedSubmitter = null;
      form.addEventListener("click", function (event) {
        const button = event.target.closest('button[type="submit"]');
        if (button) clickedSubmitter = button;
      });

      form.addEventListener("submit", async function (event) {
        event.preventDefault();
        setModalError("");
        const submitter = event.submitter || clickedSubmitter;
        clickedSubmitter = null;
        const buttons = form.querySelectorAll('button[type="submit"]');
        buttons.forEach(function (button) { button.disabled = true; });
        try {
          const payload = await postForm(form, submitter);
          showToast(payload.message || "Изменения сохранены.");
          if (payload.close) {
            closeModal();
            window.setTimeout(function () { window.location.reload(); }, 250);
          }
        } catch (error) {
          setModalError(error.message);
        } finally {
          buttons.forEach(function (button) { button.disabled = false; });
        }
      });

      const first = modalBody.querySelector("input, textarea, select");
      if (first) first.focus();
    } catch (error) {
      setModalError(error.message);
    }
  }

  document.addEventListener("click", function (event) {
    const editLink = event.target.closest(".js-edit-book");
    if (editLink) {
      event.preventDefault();
      openEdit(editLink);
      return;
    }
    const closeControl = event.target.closest("[data-modal-close]");
    if (closeControl && modal && !modal.hidden) {
      event.preventDefault();
      closeModal();
    }
  });

  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && modal && !modal.hidden) {
      closeModal();
    }
  });

  document.addEventListener("submit", async function (event) {
    const form = event.target.closest("form.js-download-form");
    if (!form) return;
    event.preventDefault();
    const button = form.querySelector('button[type="submit"]');
    const originalText = button ? button.textContent : "";
    setDownloadError(form, "");
    if (button) {
      button.disabled = true;
      button.textContent = "Скачиваю…";
    }
    try {
      const payload = await postForm(form, event.submitter);
      showToast(payload.message || "Книга скачана в каталог.");
      if (button) button.textContent = "Скачано";
      if (payload.book_url) {
        let link = form.querySelector(".download-result");
        if (!link) {
          link = document.createElement("a");
          link.className = "btn secondary small download-result";
          link.textContent = "Открыть книгу";
          form.appendChild(link);
        }
        link.href = payload.book_url;
      }
    } catch (error) {
      setDownloadError(form, error.message);
      showToast(error.message, true);
      if (button) {
        button.disabled = false;
        button.textContent = originalText;
      }
    }
  });

  const archiveForm = document.getElementById("archive-import-form");
  if (archiveForm) {
    const progressBox = document.getElementById("archive-progress");
    const progressBar = document.getElementById("archive-progress-bar");
    const progressTrack = progressBox ? progressBox.querySelector(".progress-track") : null;
    const progressText = document.getElementById("archive-progress-text");
    const progressPercent = document.getElementById("archive-progress-percent");
    const progressCurrent = document.getElementById("archive-progress-current");
    const submitButton = document.getElementById("archive-submit");

    function setArchiveProgress(job) {
      const percent = Math.max(0, Math.min(100, Number(job.progress) || 0));
      if (progressBar) progressBar.style.width = percent + "%";
      if (progressTrack) progressTrack.setAttribute("aria-valuenow", String(percent));
      if (progressPercent) progressPercent.textContent = percent + "%";
      if (progressText) {
        progressText.textContent =
          "Архивов: " + (job.archives_done || 0) + " / " + (job.archives_total || 0) +
          " · файлов: " + (job.files_seen || 0) +
          " · добавлено: " + (job.imported || 0) +
          " · обновлено: " + (job.updated || 0) +
          " · ошибок: " + (job.failed || 0);
      }
      if (progressCurrent) {
        progressCurrent.textContent = job.current_archive
          ? "Сейчас: " + job.current_archive
          : (job.state === "queued" ? "Задача в очереди…" : "Завершаю…");
      }
    }

    function finishArchiveProgress(message, isError) {
      if (progressBox) {
        progressBox.classList.toggle("is-error", Boolean(isError));
        progressBox.classList.toggle("is-done", !isError);
      }
      if (submitButton) {
        submitButton.disabled = false;
        submitButton.textContent = "Сканировать архивы";
      }
      if (message) showToast(message, Boolean(isError));
    }

    function pollArchive(jobId) {
      const url = "/admin/archive-scan/status?" + new URLSearchParams({ id: jobId }).toString();
      fetch(url, { headers: { "Accept": "application/json" } })
        .then(async function (response) {
          if (!response.ok) throw new Error("Не удалось получить статус импорта (" + response.status + ").");
          return response.json();
        })
        .then(function (job) {
          if (!job || !job.ok) throw new Error((job && job.error) || "Задача импорта не найдена.");
          setArchiveProgress(job);
          if (job.state === "done") {
            if (progressBar) progressBar.style.width = "100%";
            if (progressPercent) progressPercent.textContent = "100%";
            if (progressCurrent) progressCurrent.textContent = job.summary || "Импорт завершён.";
            finishArchiveProgress(job.summary || "Импорт завершён.", false);
            window.setTimeout(function () {
              const summary = encodeURIComponent(job.summary || "");
              window.location.assign("/admin?notice=archive_scanned&archive_summary=" + summary);
            }, 900);
            return;
          }
          if (job.state === "error") {
            throw new Error(job.error || "Ошибка импорта архивов.");
          }
          window.setTimeout(function () { pollArchive(jobId); }, 500);
        })
        .catch(function (error) {
          if (progressBox) progressBox.classList.add("is-error");
          if (progressCurrent) progressCurrent.textContent = error.message;
          finishArchiveProgress(error.message, true);
        });
    }

    archiveForm.addEventListener("submit", async function (event) {
      event.preventDefault();
      if (progressBox) {
        progressBox.hidden = false;
        progressBox.classList.remove("is-error", "is-done");
      }
      if (submitButton) {
        submitButton.disabled = true;
        submitButton.textContent = "Сканирую…";
      }
      setArchiveProgress({ state: "queued", progress: 0 });
      try {
        const payload = await postForm(archiveForm, event.submitter);
        if (!payload.job_id) throw new Error("Сервер не вернул идентификатор задачи.");
        pollArchive(payload.job_id);
      } catch (error) {
        if (progressCurrent) progressCurrent.textContent = error.message;
        finishArchiveProgress(error.message, true);
      }
    });
  }
})();

