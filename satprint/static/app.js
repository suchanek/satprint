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
function updateAreaHint() {
  const b = bboxFromInputs();
  const [w, h] = groundSize(b);
  if (!(w > 0 && h > 0)) { $("area-hint").textContent = "Invalid area."; return; }
  const km = (m) => (m / 1000).toFixed(1);
  const width = +$("width_mm").value || 100;
  $("area-hint").textContent =
    `Area ≈ ${km(w)} × ${km(h)} km  →  model ${fmt(width, 0)} × ${fmt(width * h / w, 0)} mm  (plan scale 1:${fmt(w / width * 1000, 0)})`;
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

// presets
fetch("/api/presets").then((r) => r.json()).then((presets) => {
  for (const p of presets) {
    const o = document.createElement("option");
    o.value = JSON.stringify(p.bbox); o.textContent = p.name;
    $("presets").appendChild(o);
  }
});
$("presets").addEventListener("change", (e) => {
  if (!e.target.value) return;
  const [south, west, north, east] = JSON.parse(e.target.value);
  setBBox({ south, west, north, east }, { fit: true });
  $("name").value = e.target.selectedOptions[0].textContent.split(",")[0].toLowerCase().replace(/\s+/g, "-");
  document.querySelector('input[name=source][value=terrarium]').checked = true;
});
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
let viewerPromise = null;

function initViewer() {
  if (viewerPromise) return viewerPromise;
  viewerPromise = (async () => {
    const [THREE, { OrbitControls }, { STLLoader }] = await Promise.all([
      import("three"),
      import("three/addons/controls/OrbitControls.js"),
      import("three/addons/loaders/STLLoader.js"),
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
    viewer = { THREE, STLLoader, scene, camera, controls, renderer, material, mesh: null, grid: null };
    return viewer;
  })();
  viewerPromise.catch(() => { viewerPromise = null; });
  return viewerPromise;
}
initViewer().catch(() => {});   // warm up in the background; failure is handled at preview time

async function showSTL(buffer, info) {
  let v;
  try { v = await initViewer(); }
  catch (err) {
    const ph = viewerEl.querySelector(".placeholder");
    if (ph) ph.innerHTML = "3D preview unavailable (could not load three.js from the CDN).<br>The STL download still works.";
    return;
  }
  const { THREE, STLLoader, scene, camera, controls, material } = v;
  if (v.mesh) { scene.remove(v.mesh); v.mesh.geometry.dispose(); }
  if (v.grid) scene.remove(v.grid);
  const geom = new STLLoader().parse(buffer);
  geom.computeVertexNormals();
  geom.rotateX(-Math.PI / 2);       // STL is Z-up; three.js is Y-up
  geom.center();
  v.mesh = new THREE.Mesh(geom, material);
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
$("btn-generate").addEventListener("click", async () => {
  const source = document.querySelector("input[name=source]:checked").value;
  const relief = $("relief_mm").value;
  const body = {
    source, bbox: bboxFromInputs(), upload_id: uploadId,
    ground_width_m: $("ground-width").value ? +$("ground-width").value : null,
    resolution: +$("resolution").value, width_mm: +$("width_mm").value, base_mm: +$("base_mm").value,
    exaggeration: +$("exaggeration").value, relief_mm: relief ? +relief : null,
    smoothing: +$("smoothing").value, clamp_sea_level: $("clamp").checked, name: $("name").value || "terrain",
  };
  $("btn-generate").disabled = true;
  status(source === "terrarium" ? "Downloading elevation tiles and building the mesh…" : "Building the mesh…");
  try {
    const r = await fetch("/api/model", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const j = await r.json();
    if (!r.ok) throw new Error(typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail));
    const i = j.info;
    $("hillshade").src = j.preview_png; $("hillshade").hidden = false;
    $("stats").innerHTML = [
      ["Model size", `${fmt(i.width_mm)} × ${fmt(i.depth_mm)} × ${fmt(i.height_mm)} mm`],
      ["Relief", `${fmt(i.relief_mm)} mm  (${fmt(i.relief_m, 0)} m real)`],
      ["Elevation", `${fmt(i.min_elev_m, 0)} – ${fmt(i.max_elev_m, 0)} m`],
      ["Plan scale", i.plan_scale],
      ["Vertical exaggeration", `${fmt(i.exaggeration, 2)}×`],
      ["Grid", `${i.cols} × ${i.rows}  (${fmt(i.triangles, 0)} triangles)`],
      ["Volume", `${fmt(i.volume_cm3)} cm³`],
      ["Est. PLA", `${fmt(i.est_weight_g_pla_20pct, 0)} g @ 20 % infill · ${fmt(i.est_weight_g_pla_solid, 0)} g solid`],
      ["Source", i.source + (i.source_meta?.zoom != null ? ` (zoom ${i.source_meta.zoom}, ${i.source_meta.tiles} tiles)` : "")],
    ].map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("");
    const stl = await fetch(j.stl_url);
    const buf = await stl.arrayBuffer();
    await showSTL(buf, i);
    const dl = $("btn-download");
    dl.href = j.stl_url; dl.download = j.stl_url.split("/").pop(); dl.hidden = false;
    dl.textContent = `Download STL (${fmt(buf.byteLength / 1048576, 1)} MB)`;
    status(`Done in ${i.generate_seconds}s.`, "ok");
  } catch (err) {
    status(err.message || String(err), "error");
  } finally {
    $("btn-generate").disabled = false;
  }
});
