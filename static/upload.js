// Resumable uploader (ING-1). Progressively enhances the upload form: with
// JavaScript the file is sent in chunks to /uploads, retrying failed chunks
// and resuming from the server's offset, so large files (up to the server's
// limit, 20 GB by default) survive flaky connections and even a page
// reload (pick the same file again to continue). Without JavaScript the
// form posts normally.
(function () {
  "use strict";
  var form = document.getElementById("upload-form");
  if (!form || !window.fetch || !window.File || !window.Blob) return;

  var csrf = form.querySelector("input[name=csrf_token]").value;
  var progress = document.getElementById("upload-progress");
  var bar = progress.querySelector("progress");
  var label = progress.querySelector(".upload-progress-label");
  var button = form.querySelector("button[type=submit]");
  var maxBytes = Number(form.dataset.maxBytes);
  var STORE = "bjj-resumable-uploads";
  var MAX_RETRIES = 8;

  function saved() {
    try { return JSON.parse(localStorage.getItem(STORE)) || {}; } catch (e) { return {}; }
  }
  function remember(key, id) {
    try {
      var all = saved();
      if (id) { all[key] = id; } else { delete all[key]; }
      localStorage.setItem(STORE, JSON.stringify(all));
    } catch (e) { /* storage unavailable: resume within this page only */ }
  }
  function fingerprint(file) {
    return [file.name, file.size, file.lastModified].join("|");
  }
  function sleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
  function show(text, fraction) {
    progress.hidden = false;
    label.textContent = text;
    if (fraction !== undefined) bar.value = fraction;
  }

  function api(method, url, body, headers) {
    headers = headers || {};
    headers["X-CSRF-Token"] = csrf;
    return fetch(url, {
      method: method, body: body, headers: headers, credentials: "same-origin"
    }).then(function (resp) {
      return resp.json().catch(function () { return {}; }).then(function (data) {
        data.httpStatus = resp.status;
        return data;
      });
    });
  }

  function metadata(file) {
    var data = { filename: file.name, size: file.size };
    ["title", "description", "competition", "event_date", "participants",
     "source", "license"].forEach(function (name) {
      var field = form.elements[name];
      if (field) data[name] = field.value;
    });
    return data;
  }

  // Resume a saved session for this exact file, or start a new one.
  function openSession(file) {
    var id = saved()[fingerprint(file)];
    var resume = id
      ? api("GET", "/uploads/" + id).then(function (s) {
          return s.httpStatus === 200 && s.status === "open" ? s : null;
        }).catch(function () { return null; })
      : Promise.resolve(null);
    return resume.then(function (session) {
      if (session) return session;
      return api("POST", "/uploads", JSON.stringify(metadata(file)),
                 { "Content-Type": "application/json" }).then(function (s) {
        if (s.httpStatus !== 201) throw new Error(s.error || "Upload failed to start");
        remember(fingerprint(file), s.upload_id);
        return s;
      });
    });
  }

  function sendChunks(file, session) {
    var chunk = session.chunk_size || 8 * 1024 * 1024;
    var offset = session.offset;
    var retries = 0;

    function next() {
      if (offset >= file.size) return Promise.resolve(session);
      var end = Math.min(offset + chunk, file.size);
      show("Uploading… " + Math.floor(100 * offset / file.size) + "%", offset / file.size);
      return api("PUT", session.upload_url, file.slice(offset, end), {
        "Content-Type": "application/octet-stream",
        "Content-Range": "bytes " + offset + "-" + (end - 1) + "/" + file.size
      }).then(function (resp) {
        if (resp.httpStatus === 200 || resp.httpStatus === 201) {
          retries = 0;
          offset = resp.offset;
          session = Object.assign(session, resp);
          return resp.status === "complete" ? session : next();
        }
        if (resp.httpStatus === 409 && typeof resp.offset === "number") {
          offset = resp.offset;  // server knows best; continue from there
          return next();
        }
        var error = new Error(resp.error || ("server error " + resp.httpStatus));
        // 4xx means the request itself is wrong: retrying won't help.
        error.fatal = resp.httpStatus >= 400 && resp.httpStatus < 500;
        throw error;
      }).catch(function (err) {
        // Network failures (fetch rejects) and 5xx responses are retried.
        if (!err.fatal && retries < MAX_RETRIES) {
          retries += 1;
          var wait = Math.min(30000, 1000 * Math.pow(2, retries - 1));
          show("Connection problem — retrying in " + Math.round(wait / 1000) + "s…");
          return sleep(wait).then(function () {
            return api("GET", session.upload_url).then(function (s) {
              if (typeof s.offset === "number") offset = s.offset;
              if (s.status === "complete") return Object.assign(session, s);
              return next();
            }, next);
          });
        }
        throw err;
      });
    }
    return next();
  }

  form.addEventListener("submit", function (event) {
    var file = form.elements.video.files[0];
    if (!file) return;  // let the browser show its required-field message
    event.preventDefault();
    if (file.size > maxBytes) {
      show("That file is larger than the " + Math.round(maxBytes / 1073741824) + " GB limit.");
      return;
    }
    button.disabled = true;
    openSession(file).then(function (session) {
      return sendChunks(file, session);
    }).then(function (session) {
      remember(fingerprint(file), null);
      show("Upload complete — processing…", 1);
      window.location.href = session.watch_url;
    }).catch(function (err) {
      button.disabled = false;
      show("Upload stopped: " + err.message + ". Press Upload to resume.");
    });
  });
})();
