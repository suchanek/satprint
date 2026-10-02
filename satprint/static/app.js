// three.js is loaded lazily (see initViewer) so the map, settings and STL
// download keep working even if the 3D-preview CDN is slow or unreachable.

// ───────────────────────── helpers ─────────────────────────
const $ = (id) => document.getElementById(id);
const fmt = (v, d = 1) => Number(v).toLocaleString(undefined, { maximumFractionDigits: d });
const status = (msg, cls = "") => { const el = $("status"); el.textContent = msg; el.className = "hint " + cls; };

// ───────────────────────── map ─────────────────────────
const hasLeaflet = typeof L !== "undefined";
const map = hasLeaflet ? L.map("map", { worldCopyJump: true }).setView([45.975, 7.65], 11) : null;
if (map) {
  const satellite = L.tileLayer(
    "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
    { maxZoom: 18, attribution: "Imagery © Esri, Maxar, Earthstar Geographics" });
  const streets = L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png",
    { maxZoom: 19, attribution: "© OpenStreetMap contributors" });
  satellite.addTo(map);
  $("basemap-toggle").addEventListener("change", (e) => {
    if (e.target.checked) { map.removeLayer(streets); satellite.addTo(map); }
    else { map.removeLayer(satellite); streets.addTo(map); }
  });
} else {
  $("map").innerHTML = '<div class="placeholder">Map unavailable (Leaflet could not be loaded).<br>Type the bounds below or pick a preset.</div>';
  $("btn-draw").disabled = true;
}

let rect = null;
function bboxFromInputs() {
  return { south: +$("south").value, west: +$("west").value, north: +$("north").value, east: +$("east").value };
}
function setBBox(b, { fit = false } = {}) {
  const south = Math.min(b.south, b.north), north = Math.max(b.south, b.north);
  const west = Math.min(b.west, b.east), east = Math.max(b.west, b.east);
  $("south").value = south.toFixed(4); $("north").value = north.toFixed(4);
  $("west").value = west.toFixed(4); $("east").value = east.toFixed(4);
  const bounds = [[south, west], [north, east]];
  if (map) {
    if (rect) rect.setBounds(bounds);
    else rect = L.rectangle(bounds, { color: "#f5a524", weight: 2, fillOpacity: 0.08 }).addTo(map);
    if (fit) map.fitBounds(bounds, { padding: [20, 20] });
  }
  updateAreaHint();
}
function groundSize(b) {
  const R = 6371008.8, rad = Math.PI / 180;
  const midLat = (b.south + b.north) / 2;
  const w = R * Math.cos(midLat * rad) * (b.east - b.west) * rad;
  const h = R * (b.north - b.south) * rad;
  return [w, h];
}
const MAX_BUILDING_KM2 = 40;   // matches MAX_BUILDING_AREA_KM2 on the server
function updateAreaHint() {
  const b = bboxFromInputs();
  const [w, h] = groundSize(b);
  if (!(w > 0 && h > 0)) { $("area-hint").textContent = "Invalid area."; return; }
  const km = (m) => (m / 1000).toFixed(1);
  const width = +$("width_mm").value || 100;
  const km2 = w * h / 1e6;
  const tooBig = $("buildings").checked && km2 > MAX_BUILDING_KM2;
  $("area-hint").textContent =
    `Area ≈ ${km(w)} × ${km(h)} km  →  model ${fmt(width, 0)} × ${fmt(width * h / w, 0)} mm  (plan scale 1:${fmt(w / width * 1000, 0)})` +
    (tooBig ? `  ·  too large for buildings (${fmt(km2, 0)} km², limit ${MAX_BUILDING_KM2})` : "");
  $("area-hint").className = "hint" + (tooBig ? " error" : "");
}
["south", "west", "north", "east"].forEach((id) => $(id).addEventListener("change", () => setBBox(bboxFromInputs(), { fit: true })));
$("width_mm").addEventListener("input", updateAreaHint);

// rectangle drawing: click-drag while in draw mode
let drawing = false, dragStart = null;
if (map) $("btn-draw").addEventListener("click", () => {
  drawing = !drawing;
  $("btn-draw").classList.toggle("active", drawing);
  $("btn-draw").textContent = drawing ? "Drag on the map…  (click to cancel)" : "Draw rectangle";
  $("map").classList.toggle("drawing", drawing);
  if (drawing) map.dragging.disable(); else map.dragging.enable();
});
if (map) map.on("mousedown", (e) => { if (drawing) dragStart = e.latlng; });
if (map) map.on("mousemove", (e) => {
  if (!drawing || !dragStart) return;
  setBBox({ south: dragStart.lat, west: dragStart.lng, north: e.latlng.lat, east: e.latlng.lng });
});
if (map) map.on("mouseup", (e) => {
  if (!drawing || !dragStart) return;
  setBBox({ south: dragStart.lat, west: dragStart.lng, north: e.latlng.lat, east: e.latlng.lng });
  dragStart = null; drawing = false;
  $("btn-draw").classList.remove("active"); $("btn-draw").textContent = "Draw rectangle";
  $("map").classList.remove("drawing"); map.dragging.enable();
  document.querySelector('input[name=source][value=terrarium]').checked = true;
});

// presets, grouped; each suggests whether to add buildings
const slug = (s) => s.split(",")[0].toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") || "terrain";
fetch("/api/presets").then((r) => r.json()).then((presets) => {
  const groups = new Map();
  for (const p of presets) {
    if (!groups.has(p.group)) {
      const g = document.createElement("optgroup"); g.label = p.group;
      groups.set(p.group, g); $("presets").appendChild(g);
    }
    const o = document.createElement("option");
    o.value = JSON.stringify(p); o.textContent = p.name;
    groups.get(p.group).appendChild(o);
  }
});
$("presets").addEventListener("change", (e) => {
  if (!e.target.value) return;
  const p = JSON.parse(e.target.value);
  const [south, west, north, east] = p.bbox;
  $("buildings").checked = p.buildings;
  setBBox({ south, west, north, east }, { fit: true });
  $("name").value = slug(p.name);
  document.querySelector('input[name=source][value=terrarium]').checked = true;
});
$("buildings").addEventListener("change", updateAreaHint);

// place search (Nominatim, through the server)
function squareAround(lat, lon, km) {
  const dlat = km / 2 / 111.32, dlon = km / 2 / (111.32 * Math.cos(lat * Math.PI / 180));
  return { south: lat - dlat, west: lon - dlon, north: lat + dlat, east: lon + dlon };
}
// Always a square around the place, so a long thin feature (a bridge, a
// river) does not become a long thin model. Nature gets room for the
// landscape; everything else is clamped to a size where buildings work.
const NATURAL_TYPES = new Set(["peak", "volcano", "mountain_range", "ridge", "glacier", "valley", "massif", "crater"]);
function bboxForResult(r) {
  const [s, w, n, e] = r.bbox;
  const [wm, hm] = groundSize({ south: s, west: w, north: n, east: e });
  const km = Math.max(wm, hm) / 1000;
  // A big extent (Tokyo's includes islands 1000 km out) can be centered at
  // sea; use the place's own point then, and the extent center otherwise.
  const lat = km > 5 ? r.lat : (s + n) / 2, lon = km > 5 ? r.lon : (w + e) / 2;
  if (r.category === "natural" || NATURAL_TYPES.has(r.type)) return squareAround(lat, lon, Math.min(Math.max(km, 10), 30));
  return squareAround(lat, lon, Math.min(Math.max(km, 1.5), 5));
}
// OSM uses "yes" for an untyped feature, e.g. bridge=yes; show the category then.
const placeKind = (r) => (r.type && r.type !== "yes" ? r.type : r.category).replace(/_/g, " ");
const results = $("search-results");
function closeResults() { results.hidden = true; results.innerHTML = ""; }
function chooseResult(r) {
  setBBox(bboxForResult(r), { fit: true });
  $("name").value = slug(r.name);
  $("search-q").value = r.display_name;
  document.querySelector('input[name=source][value=terrarium]').checked = true;
  closeResults();
}
$("search-form").addEventListener("submit", async () => {
  const q = $("search-q").value.trim();
  if (q.length < 2) return;
  $("btn-search").disabled = true;
  results.hidden = false; results.innerHTML = "<li>Searching…</li>";
  try {
    const r = await fetch("/api/search?q=" + encodeURIComponent(q));
    const j = await r.json();
    if (!r.ok) throw new Error(j.detail || "search failed");
    if (!j.length) { results.innerHTML = "<li>No places found.</li>"; return; }
    results.innerHTML = "";
    for (const item of j) {
      const li = document.createElement("li");
      li.tabIndex = 0;
      const rest = item.display_name.split(",").slice(1).join(",").trim();
      li.innerHTML = `<strong></strong><small></small>`;
      li.querySelector("strong").textContent = item.name;
      li.querySelector("small").textContent = [placeKind(item), rest].filter(Boolean).join(" · ");
      li.addEventListener("click", () => chooseResult(item));
      li.addEventListener("keydown", (e) => { if (e.key === "Enter") chooseResult(item); });
      results.appendChild(li);
    }
  } catch (err) {
    results.innerHTML = "";
    const li = document.createElement("li"); li.textContent = err.message || String(err); results.appendChild(li);
  } finally {
    $("btn-search").disabled = false;
  }
});
document.addEventListener("click", (e) => { if (!$("search-form").contains(e.target)) closeResults(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeResults(); });
setBBox(bboxFromInputs(), { fit: true });

// ───────────────────────── upload ─────────────────────────
let uploadId = null;
$("btn-upload").addEventListener("click", async () => {
  const f = $("file").files[0];
  if (!f) { $("upload-hint").textContent = "Choose a file first."; return; }
  const fd = new FormData(); fd.append("file", f);
  $("upload-hint").textContent = "Uploading…";
  const r = await fetch("/api/upload", { method: "POST", body: fd });
  const j = await r.json();
  if (!r.ok) { $("upload-hint").textContent = j.detail || "upload failed"; $("upload-hint").className = "hint error"; return; }
  uploadId = j.upload_id;
  $("upload-hint").className = "hint ok";
  $("upload-hint").textContent = `Loaded ${j.cols}×${j.rows} px, ${fmt(j.min_elev_m, 0)}–${fmt(j.max_elev_m, 0)} m` +
    (j.meta.georeferenced ? " (georeferenced)" : j.meta.assumed_pixel_m ? ` (assuming ${j.meta.assumed_pixel_m} m/px — set ground width if known)` : "");
  $("source-upload").disabled = false; $("source-upload").checked = true;
});

// ───────────────────────── sliders ─────────────────────────
$("resolution").addEventListener("input", (e) => $("resolution-out").value = e.target.value);
$("smoothing").addEventListener("input", (e) => $("smoothing-out").value = e.target.value);

// ───────────────────────── 3D viewer ─────────────────────────
const viewerEl = $("viewer");
let viewer = null;          // { THREE, scene, camera, controls, renderer, material, mesh, grid }
// mesh is either a plain STL Mesh (shared material) or a GLB scene (own materials)
let viewerPromise = null;

function initViewer() {
  if (viewerPromise) return viewerPromise;
  viewerPromise = (async () => {
    const [THREE, { OrbitControls }, { STLLoader }, { GLTFLoader }] = await Promise.all([
      import("three"),
      import("three/addons/controls/OrbitControls.js"),
      import("three/addons/loaders/STLLoader.js"),
      import("three/addons/loaders/GLTFLoader.js"),
    ]);
    const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    viewerEl.appendChild(renderer.domElement);
    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(40, 1, 0.1, 5000);
    const controls = new OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;
    scene.add(new THREE.HemisphereLight(0xffffff, 0x334455, 1.1));
    const sun = new THREE.DirectionalLight(0xffffff, 1.6); sun.position.set(-1, 1.5, 1); scene.add(sun);
    const material = new THREE.MeshStandardMaterial({ color: 0xd9c9a8, metalness: 0.05, roughness: 0.85 });
    const resize = () => {
      const w = viewerEl.clientWidth, h = viewerEl.clientHeight;
      renderer.setSize(w, h); camera.aspect = w / h; camera.updateProjectionMatrix();
    };
    window.addEventListener("resize", resize); resize();
    (function animate() { requestAnimationFrame(animate); controls.update(); renderer.render(scene, camera); })();
    viewer = { THREE, STLLoader, GLTFLoader, scene, camera, controls, renderer, material, mesh: null, grid: null };
    return viewer;
  })();
  viewerPromise.catch(() => { viewerPromise = null; });
  return viewerPromise;
}
initViewer().catch(() => {});   // warm up in the background; failure is handled at preview time

function disposeModel(v) {
  if (!v.mesh) return;
  v.scene.remove(v.mesh);
  v.mesh.traverse((o) => {
    if (!o.isMesh) return;
    o.geometry.dispose();
    if (o.material !== v.material) {
      o.material.map?.dispose();
      o.material.dispose();
    }
  });
  v.mesh = null;
}

// Show the textured GLB when there is one, otherwise the STL.
// GLB is in meters, Y up; STL is in mm, Z up. Both end up in mm, Y up, centered.
async function showModel(buffer, info, isGLB) {
  let v;
  try { v = await initViewer(); }
  catch (err) {
    const ph = viewerEl.querySelector(".placeholder");
    if (ph) ph.innerHTML = "3D preview unavailable (could not load three.js from the CDN).<br>The STL download still works.";
    return;
  }
  const { THREE, STLLoader, GLTFLoader, scene, camera, controls, material } = v;
  disposeModel(v);
  if (v.grid) scene.remove(v.grid);
  if (isGLB) {
    const gltf = await new GLTFLoader().parseAsync(buffer, "");
    const model = gltf.scene;
    model.scale.setScalar(1000);    // meters -> mm, to match the grid and camera
    model.updateMatrixWorld(true);
    const center = new THREE.Box3().setFromObject(model).getCenter(new THREE.Vector3());
    model.position.sub(center);
    v.mesh = model;
  } else {
    const geom = new STLLoader().parse(buffer);
    geom.computeVertexNormals();
    geom.rotateX(-Math.PI / 2);       // STL is Z-up; three.js is Y-up
    geom.center();
    v.mesh = new THREE.Mesh(geom, material);
  }
  scene.add(v.mesh);
  const size = Math.max(info.width_mm, info.depth_mm);
  v.grid = new THREE.GridHelper(size * 1.6, 16, 0x3a4656, 0x263040);
  v.grid.position.y = -info.height_mm / 2;
  scene.add(v.grid);
  camera.position.set(size * 0.9, size * 0.75, size * 1.1);
  controls.target.set(0, 0, 0); controls.update();
  viewerEl.querySelector(".placeholder")?.remove();
}

// ───────────────────────── generate ─────────────────────────
// The build runs as a server-side job; poll it and show each stage.
const STAGE_LABELS = {
  starting: "Starting",
  elevation: "Elevation tiles",
  buildings: "Building data tiles",
  "building mesh": "Building solids",
  "terrain mesh": "Terrain mesh",
  imagery: "Imagery tiles",
  water: "Water data tiles",
  "writing files": "Writing files",
};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const errorText = (j) => (typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail));
function showProgress(job) {
  const bar = $("progress");
  bar.hidden = false;
  if (job.total > 0) { bar.max = job.total; bar.value = job.done; }
  else bar.removeAttribute("value");   // indeterminate
  const label = STAGE_LABELS[job.stage] || job.stage;
  const count = job.total > 0 ? ` ${fmt(job.done, 0)} / ${fmt(job.total, 0)}` : "…";
  status(`${label}${count}  (${fmt(job.elapsed_s, 0)} s)`);
}
async function runJob(body) {
  const r = await fetch("/api/jobs", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const start = await r.json();
  if (!r.ok) throw new Error(errorText(start));
  for (;;) {
    await sleep(400);
    const pr = await fetch(`/api/jobs/${start.job_id}`);
    const job = await pr.json();
    if (!pr.ok) throw new Error(errorText(job));
    if (job.status === "done") return job.result;
    if (job.status === "error") throw new Error(job.error);
    showProgress(job);
  }
}

$("btn-generate").addEventListener("click", async () => {
  const source = document.querySelector("input[name=source]:checked").value;
  const relief = $("relief_mm").value;
  const body = {
    source, bbox: bboxFromInputs(), upload_id: uploadId,
    ground_width_m: $("ground-width").value ? +$("ground-width").value : null,
    resolution: +$("resolution").value, width_mm: +$("width_mm").value, base_mm: +$("base_mm").value,
    exaggeration: +$("exaggeration").value, relief_mm: relief ? +relief : null,
    smoothing: +$("smoothing").value, clamp_sea_level: $("clamp").checked, texture: $("texture").checked,
    buildings: $("buildings").checked, building_scale: +$("building_scale").value || 1,
    building_source: $("building_source").value, multicolor: $("multicolor").checked,
    frame_mm: +$("frame_mm").value || 0,
    frame_height_mm: $("frame_height_mm").value ? +$("frame_height_mm").value : null,
    name: $("name").value || "terrain",
  };
  $("btn-generate").disabled = true;
  showProgress({ stage: "starting", done: 0, total: 0, elapsed_s: 0 });
  try {
    const j = await runJob(body);
    status("Loading the 3D preview…");
    const i = j.info;
    $("hillshade").src = j.preview_png; $("hillshade").hidden = false;
    $("stats").innerHTML = [
      ["Model size", `${fmt(i.outer_width_mm ?? i.width_mm)} × ${fmt(i.outer_depth_mm ?? i.depth_mm)} × ${fmt(i.height_mm)} mm` +
        (i.frame_mm ? ` (with ${fmt(i.frame_mm)} mm frame)` : "")],
      ["Relief", `${fmt(i.relief_mm)} mm  (${fmt(i.relief_m, 0)} m real)`],
      ["Elevation", `${fmt(i.min_elev_m, 0)} – ${fmt(i.max_elev_m, 0)} m`],
      ["Plan scale", i.plan_scale],
      ["Vertical exaggeration", `${fmt(i.exaggeration, 2)}×`],
      ["Grid", `${i.cols} × ${i.rows}  (${fmt(i.triangles, 0)} triangles)`],
      ["Volume", `${fmt(i.volume_cm3)} cm³`],
      ["Est. PLA", `${fmt(i.est_weight_g_pla_20pct, 0)} g @ 20 % infill · ${fmt(i.est_weight_g_pla_solid, 0)} g solid`],
      ["Source", i.source + (i.source_meta?.zoom != null ? ` (zoom ${i.source_meta.zoom}, ${i.source_meta.tiles} tiles)` : "")],
      ...(i.buildings != null ? [["Buildings", `${fmt(i.buildings, 0)} (${{ openfreemap: "OpenStreetMap via OpenFreeMap", overpass: "OpenStreetMap via Overpass", overture: "Overture Maps" }[i.building_source] || i.building_source})`]] : []),
      ...(i.multicolor_parts ? [["Multi-color", `${i.multicolor_parts.join(", ")} (${fmt(i.water_fraction * 100, 0)} % water)`]] : []),
      ...(i.textured ? [["Texture", `${i.texture_meta.px[1]} × ${i.texture_meta.px[0]} px (zoom ${i.texture_meta.zoom}, ${i.texture_meta.tiles} tiles)`]] : []),
    ].map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("");
    const previewUrl = j.glb_url || j.stl_url;
    const buf = await (await fetch(previewUrl)).arrayBuffer();
    await showModel(buf, i, Boolean(j.glb_url));
    const dl = $("btn-download");
    dl.href = j.stl_url; dl.download = j.stl_url.split("/").pop(); dl.hidden = false;
    dl.textContent = `Download STL (${fmt(i.stl_bytes / 1048576, 1)} MB)`;
    const glb = $("btn-download-glb");
    glb.hidden = !j.glb_url;
    if (j.glb_url) {
      glb.href = j.glb_url; glb.download = j.glb_url.split("/").pop();
      glb.textContent = `Download textured GLB (${fmt(i.glb_bytes / 1048576, 1)} MB)`;
    }
    const mc = $("btn-download-3mf");
    mc.hidden = !j.threemf_url;
    if (j.threemf_url) {
      mc.href = j.threemf_url; mc.download = j.threemf_url.split("/").pop();
      mc.textContent = `Download multi-color 3MF (${fmt(i.threemf_bytes / 1048576, 1)} MB)`;
    }
    const problems = [
      i.water_error && `No rivers or lakes: ${i.water_error}`,
      i.texture_error && `No texture: ${i.texture_error}`,
      i.building_error && `No buildings: ${i.building_error}`,
      i.building_warning && `Buildings: ${i.building_warning}`,
    ].filter(Boolean);
    status(`Done in ${i.generate_seconds}s.` + (problems.length ? " " + problems.join(" ") : ""), problems.length ? "error" : "ok");
  } catch (err) {
    status(err.message || String(err), "error");
  } finally {
    $("btn-generate").disabled = false;
    $("progress").hidden = true;
  }
});
