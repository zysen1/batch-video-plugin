let currentId = null;

async function api(path, method = "GET", body = null) {
  const opts = { method, headers: {} };
  if (body !== null) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  return res.json();
}

async function loadProjects() {
  const data = await api("/api/projects");
  const box = document.getElementById("project-list");
  box.innerHTML = (data.projects || []).map(p =>
    `<div class="proj" onclick="openProject(${p.id})"><b>${p.title}</b>
     <span>${p.status} / ${p.post_status}</span><br><small>${p.idea}</small></div>`
  ).join("") || "<p>暂无项目，先创建一个。</p>";
}

async function createProject() {
  const idea = document.getElementById("f-idea").value.trim();
  if (!idea) { alert("请先写一句话故事"); return; }
  const title = document.getElementById("f-title").value.trim() || idea.slice(0, 20);
  const res = await api("/api/projects", "POST", {
    title, idea,
    genre: document.getElementById("f-genre").value,
    orientation: document.getElementById("f-orientation").value,
    takesPerShot: parseInt(document.getElementById("f-takes").value, 10),
    autoMode: document.getElementById("f-auto").checked,
  });
  if (res.ok) { await loadProjects(); openProject(res.projectId); }
  else alert(res.error || "创建失败");
}

async function openProject(id) {
  currentId = id;
  document.getElementById("detail").style.display = "block";
  await refresh();
}

async function refresh() {
  if (!currentId) return;
  const d = await api(`/api/projects/${currentId}`);
  if (d.error) { alert(d.error); return; }
  const p = d.project;
  document.getElementById("d-title").textContent = `《${p.title}》`;
  document.getElementById("d-meta").textContent =
    `${p.idea} | ${p.genre} | ${p.orientation} | 每镜 ${p.takes_per_shot} 条 | 状态 ${p.status} / ${p.post_status}`;
  document.getElementById("lock-char").value = p.character_lock;
  document.getElementById("lock-loc").value = p.location_lock;
  document.getElementById("lock-vis").value = p.visual_lock;
  document.getElementById("publish-copy").textContent = p.publish_copy || "（成片后自动生成）";

  document.getElementById("shots").innerHTML = d.shots.map(s => `
    <div class="shot">
      <b>${s.title}</b> · ${s.camera}<br>
      <small>画面：${s.visual}</small><br>
      <small>对白：${s.dialogue}</small><br>
      <small>接续：${s.start_state} → ${s.end_state}</small>
      <div class="takes">${s.takes.map(t => `
        <div class="take ${s.selected_take_id === t.id ? "selected" : ""}">
          <b>第 ${t.take_no} 条</b> [${t.status}] ${t.attempts ? `尝试 ${t.attempts} 次` : ""}
          ${t.score_continuity != null ? `接缝 ${t.score_continuity} / 真实感 ${t.score_realism}` : ""}
          ${t.file_path ? `<video controls preload="none" src="/media/project_${currentId}/${t.file_path.split("/").pop()}"></video>` : ""}
          <br><small>${t.check_note || ""}</small><br>
          ${t.status === "done" ? `<button onclick="selectTake(${t.id})">选定本条</button>` : ""}
        </div>`).join("")}
      </div>
      <label>从磁盘导入本镜候选 <input type="file" onchange="importTake(${s.id}, this.files[0])"></label>
    </div>`).join("");

  document.getElementById("reviews").innerHTML = d.reviews.map(r => `
    <div class="review ${r.verdict}">
      镜头 ${r.shot_id}：<b>${r.verdict}</b> 真实感 ${r.realism_score} / 接缝 ${r.continuity_score}<br>
      <small>${r.issues}</small><br><small>修法：${r.edit_suggestion}</small>
      <button onclick="manualFail(${r.shot_id})">人工标为不通过</button>
    </div>`).join("") || "<p>暂无终审结果。</p>";

  document.getElementById("plan-json").value =
    d.plan ? JSON.stringify(d.plan, null, 2) : "";

  document.getElementById("finals").innerHTML = d.finals.map(f => `
    <div class="final">
      <video controls preload="none" src="/media/project_${currentId}/${f.file_path.split("/").pop()}"></video>
      <button onclick="approve(${f.id})" ${f.approved ? "disabled" : ""}>
        ${f.approved ? "已定稿 ✓" : "定稿"}</button>
    </div>`).join("") || "<p>暂无成片。</p>";
}

async function doPlan() {
  const n = parseInt(document.getElementById("plan-n").value, 10);
  const beats = document.getElementById("plan-beats").value.split("\n")
    .map(l => l.trim()).filter(Boolean).map(l => {
      const [visual, dialogue] = l.split("|").map(s => s.trim());
      return { visual, dialogue: dialogue || "" };
    });
  const res = await api(`/api/projects/${currentId}/plan`, "POST", { shotCount: n, beats });
  if (!res.ok) alert(res.error);
  await refresh();
}

async function saveLocks() {
  const res = await api(`/api/projects/${currentId}/locks`, "POST", {
    characterLock: document.getElementById("lock-char").value,
    locationLock: document.getElementById("lock-loc").value,
    visualLock: document.getElementById("lock-vis").value,
  });
  if (!res.ok) alert(res.error);
  await refresh();
}

async function launch() {
  const res = await api(`/api/projects/${currentId}/launch`, "POST", {});
  if (!res.ok) alert(res.error);
  await refresh();
}

async function selectTake(takeId) {
  await api(`/api/projects/${currentId}/takes/${takeId}/select`, "POST", {});
  await refresh();
}

async function doReview() {
  const res = await api(`/api/projects/${currentId}/review`, "POST", {});
  if (!res.ok) alert(res.error);
  await refresh();
}

async function manualFail(shotId) {
  await api(`/api/projects/${currentId}/review-manual`, "POST", {
    shotId, verdict: "fail", signals: ["identity_drift"],
    realismScore: 5, continuityScore: 3,
    editSuggestion: "不要硬剪：必须再抽，本条不进入二次剪辑。",
    issues: "人工复核：人物身份漂移。",
  });
  await refresh();
}

async function genPlan() {
  const res = await api(`/api/projects/${currentId}/edit-plan`, "POST", {});
  if (!res.ok) alert(res.error);
  await refresh();
}

async function savePlan() {
  try {
    const plan = JSON.parse(document.getElementById("plan-json").value);
    const res = await api(`/api/projects/${currentId}/edit-plan`, "PUT", { plan });
    if (!res.ok) alert(res.error);
  } catch (e) { alert("方案 JSON 解析失败：" + e.message); }
  await refresh();
}

async function doPolish() {
  const res = await api(`/api/projects/${currentId}/polish`, "POST", {});
  if (!res.ok) alert(res.error);
  else alert(`精剪完成，时长 ${res.duration.toFixed(2)} 秒`);
  await refresh();
}

async function doRoughcut() {
  const res = await api(`/api/projects/${currentId}/roughcut`, "POST", {});
  if (!res.ok) alert(res.error);
  else alert(`粗剪完成，时长 ${res.duration.toFixed(2)} 秒`);
  await refresh();
}

async function rerollFailed() {
  const res = await api(`/api/projects/${currentId}/reroll-failed`, "POST", {});
  if (!res.ok) alert(res.error);
  await refresh();
}

async function approve(finalVideoId) {
  await api(`/api/projects/${currentId}/approve`, "POST", { finalVideoId });
  await refresh();
}

async function importTake(shotId, file) {
  if (!file) return;
  const form = new FormData();
  form.append("file", file);
  const res = await fetch(`/api/projects/${currentId}/import/${shotId}`, { method: "POST", body: form });
  const data = await res.json();
  if (!data.ok) alert(data.error);
  await refresh();
}

loadProjects();
