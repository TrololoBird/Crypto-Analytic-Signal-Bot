let sandboxState = {};

function renderSandbox() {
  const status = document.getElementById("sandbox-status");
  if (status) status.textContent = "Ready. Configure replay parameters below.";
}

function _onSandboxRun() {
  const hours = parseInt(document.getElementById("sandbox-hours")?.value || 24);
  const status = document.getElementById("sandbox-status");
  const result = document.getElementById("sandbox-result");
  status.textContent = "Running sandbox replay for " + hours + "h...";
  status.style.color = "var(--yellow)";
  fetch("/api/v1/sandbox/replay", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      hours,
      disabled_setups: [],
      weights: {},
    }),
  }).then(r => r.json()).then(res => {
    status.textContent = "Job queued: " + res.job_id;
    status.style.color = "var(--green)";
    result.textContent = JSON.stringify(res, null, 2);
  }).catch(err => {
    status.textContent = "Error: " + err;
    status.style.color = "var(--red)";
  });
}

function _onSandboxPoll() {
  const result = document.getElementById("sandbox-result");
  const jobId = result?.textContent ? JSON.parse(result.textContent)?.job_id : null;
  if (!jobId) return;
  fetch("/api/v1/sandbox/result/" + jobId)
    .then(r => r.json())
    .then(res => {
      const status = document.getElementById("sandbox-status");
      status.textContent = "Job " + jobId + " status: " + res.status + " (" + (res.progress || 0) + "%)";
    }).catch(() => {});
}
