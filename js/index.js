// Generate/store unique device ID
  function getDeviceID() {
    let id = localStorage.getItem("device_id");
    if (!id) {
      id = "dev-" + Math.random().toString(36).substr(2, 9);
      localStorage.setItem("device_id", id);
    }
    return id;
  }

  let totalPages = 0;

  // Upload button
  document.getElementById("upload-btn").addEventListener("click", async () => {
    const form = document.getElementById("upload-form");
    const formData = new FormData(form);
    formData.append("device_id", getDeviceID());

    const res = await fetch("/upload", {
      method: "POST",
      body: formData
    });
    const data = await res.json();

    if (data.status === "success") {
      totalPages = data.total_pages;
      document.getElementById("total-pages").textContent = totalPages;
      alert("PDF uploaded successfully!");
    } else {
      alert("Upload failed: " + data.error);
    }
  });

  const pageInput = document.getElementById("pages");
  const pageError = document.getElementById("page-error");
  const previewBtn = document.getElementById("preview-btn");

  // 🔹 Re-validate whenever user types in the range box
  pageInput.addEventListener("input", () => {
    if (!totalPages) return; // no file uploaded yet
    const pagesInput = pageInput.value;
    const maxPage = totalPages;

    let rangeOk = true;
    if (pagesInput.trim()) {
      rangeOk = pagesInput.split(",").every(part => {
        if (part.includes("-")) {
          const [s, e] = part.split("-").map(Number);
          return s >= 1 && e <= maxPage && s <= e;
        } else {
          const p = Number(part);
          return p >= 1 && p <= maxPage;
        }
      });
    }

    if (!rangeOk) {
      pageError.style.display = "block";
      previewBtn.disabled = true;
    } else {
      pageError.style.display = "none";
      previewBtn.disabled = false;
    }
  });

  // Preview button
  previewBtn.addEventListener("click", async () => {
    const form = document.getElementById("upload-form");
    const formData = new FormData(form);
    formData.append("device_id", getDeviceID());

    const res = await fetch("/preview", {
      method: "POST",
      body: formData
    });
    const data = await res.json();

    const previewBox = document.getElementById("preview-box");
    previewBox.innerHTML = "";
    if (data.previews) {
      data.previews.forEach(src => {
        const img = document.createElement("img");
        img.src = src;
        img.style.width = "100%";
        previewBox.appendChild(img);
      });
    }
  });