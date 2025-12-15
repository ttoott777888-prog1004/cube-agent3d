import * as THREE from "https://esm.sh/three@0.160.0";
import { OrbitControls } from "https://esm.sh/three@0.160.0/examples/jsm/controls/OrbitControls.js";

const elCanvas = document.getElementById("canvas");
const elStart = document.getElementById("btnStart");
const elStop = document.getElementById("btnStop");
const elReset = document.getElementById("btnReset");
const elZoomReset = document.getElementById("btnZoomReset");
const elStatus = document.getElementById("status");
const elMeta = document.getElementById("meta");
const elLog = document.getElementById("log");

function logLine(s) {
  const t = new Date().toLocaleTimeString();
  elLog.innerText = `[${t}] ${s}\n` + elLog.innerText;
}

function hexToThreeColor(hex) {
  try { return new THREE.Color(hex); } catch { return new THREE.Color("#7dd3fc"); }
}

let ws = null;

// --- Three.js scene
const renderer = new THREE.WebGLRenderer({ canvas: elCanvas, antialias: true });
renderer.setPixelRatio(window.devicePixelRatio || 1);
renderer.outputColorSpace = THREE.SRGBColorSpace;

const scene = new THREE.Scene();
scene.background = new THREE.Color(0x0b0f17);

const camera = new THREE.PerspectiveCamera(60, 1, 0.05, 500);
camera.position.set(8, 8, 10);

const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;
controls.dampingFactor = 0.08;
controls.minDistance = 3;
controls.maxDistance = 80;
controls.target.set(0, 4, 0);

elZoomReset.addEventListener("click", () => {
  camera.position.set(8, 8, 10);
  controls.target.set(0, 4, 0);
  controls.update();
});

scene.add(new THREE.AmbientLight(0xffffff, 0.55));
const dir = new THREE.DirectionalLight(0xffffff, 1.0);
dir.position.set(10, 20, 10);
scene.add(dir);

const grid = new THREE.GridHelper(60, 60, 0x223044, 0x121a27);
grid.position.y = 0;
scene.add(grid);

// ===== Instanced cubes (solid)
let MAX_INST = 256;

const cubeGeom = new THREE.BoxGeometry(1, 1, 1);
const cubeMat = new THREE.MeshStandardMaterial({
  vertexColors: true,
  roughness: 0.65,
  metalness: 0.1,
});

let inst = new THREE.InstancedMesh(cubeGeom, cubeMat, MAX_INST);
inst.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
inst.instanceColor = new THREE.InstancedBufferAttribute(new Float32Array(MAX_INST * 3), 3);
scene.add(inst);

let currentCount = 0;

// ===== Probes (green transparent boxes)
const MAX_PROBES = 64;
const probeGeom = new THREE.BoxGeometry(1.02, 1.02, 1.02);
const probeMat = new THREE.MeshBasicMaterial({
  vertexColors: true,
  transparent: true,
  opacity: 0.22,
  depthWrite: false,
});
let probeInst = new THREE.InstancedMesh(probeGeom, probeMat, MAX_PROBES);
probeInst.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
probeInst.instanceColor = new THREE.InstancedBufferAttribute(new Float32Array(MAX_PROBES * 3), 3);
scene.add(probeInst);

const _m = new THREE.Matrix4();
const _p = new THREE.Vector3();
const _q = new THREE.Quaternion();
const _s = new THREE.Vector3(1, 1, 1);

function ensureCapacity(n) {
  if (n <= MAX_INST) return;

  const newMax = Math.max(n, MAX_INST * 2, 256);
  MAX_INST = newMax;

  scene.remove(inst);
  inst.dispose?.();

  inst = new THREE.InstancedMesh(cubeGeom, cubeMat, MAX_INST);
  inst.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
  inst.instanceColor = new THREE.InstancedBufferAttribute(new Float32Array(MAX_INST * 3), 3);
  scene.add(inst);

  logLine(`Instanced capacity expanded: ${MAX_INST}`);
}

function applyCubes(cubes) {
  const arr = cubes || [];
  ensureCapacity(arr.length);

  currentCount = Math.min(arr.length, MAX_INST);

  for (let i = 0; i < currentCount; i++) {
    const c = arr[i];

    _p.set(c.pos[0], c.pos[1], c.pos[2]);
    _q.set(c.rot[0], c.rot[1], c.rot[2], c.rot[3]);
    _s.set(c.scale[0], c.scale[1], c.scale[2]);

    _m.compose(_p, _q, _s);
    inst.setMatrixAt(i, _m);

    const col = hexToThreeColor(c.color || "#0f172a");
    inst.setColorAt(i, col);
  }

  for (let i = currentCount; i < MAX_INST; i++) {
    _m.identity();
    _m.makeScale(0, 0, 0);
    inst.setMatrixAt(i, _m);
  }

  inst.instanceMatrix.needsUpdate = true;
  if (inst.instanceColor) inst.instanceColor.needsUpdate = true;
}

function applyProbes(probes) {
  const arr = probes || [];
  const n = Math.min(arr.length, MAX_PROBES);

  for (let i = 0; i < n; i++) {
    const p = arr[i];
    _p.set(p.pos[0], p.pos[1], p.pos[2]);
    _q.identity();
    _s.set(1.02, 1.02, 1.02);

    _m.compose(_p, _q, _s);
    probeInst.setMatrixAt(i, _m);

    const alpha = (typeof p.alpha === "number") ? p.alpha : 0.22;
    const col = new THREE.Color(0x22c55e).lerp(new THREE.Color(0x86efac), Math.min(1, Math.max(0, alpha)));
    probeInst.setColorAt(i, col);
  }

  for (let i = n; i < MAX_PROBES; i++) {
    _m.identity();
    _m.makeScale(0, 0, 0);
    probeInst.setMatrixAt(i, _m);
  }

  probeInst.instanceMatrix.needsUpdate = true;
  if (probeInst.instanceColor) probeInst.instanceColor.needsUpdate = true;
}

function applySnapshot(payload) {
  applyCubes(payload.cubes || []);
  applyProbes(payload.probes || []);
}

function connectWS() {
  const proto = (location.protocol === "https:") ? "wss" : "ws";
  const url = `${proto}://${location.host}/ws`;
  ws = new WebSocket(url);

  ws.onopen = () => {
    elStatus.innerText = "연결됨";
    ws.send(JSON.stringify({ type: "HELLO", payload: { client: "web" } }));
    logLine("WebSocket 연결됨");
  };

  ws.onclose = () => {
    elStatus.innerText = "연결 끊김";
    logLine("WebSocket 연결 끊김");
    setTimeout(connectWS, 800);
  };

  ws.onerror = () => {
    elStatus.innerText = "오류";
  };

  ws.onmessage = (ev) => {
    let msg = null;
    try { msg = JSON.parse(ev.data); } catch { return; }

    const type = msg.type;
    const payload = msg.payload || {};

    if (type === "SERVER_STATUS") {
      const run = payload.running ? "RUN" : "STOP";
      elStatus.innerText = `${run} | tick=${payload.tick} | cubes=${payload.cube_count}`;
      elMeta.innerText = `session=${payload.session_id} | tick_hz=${payload.tick_hz} | max_cubes=${payload.max_cubes} | policy=${payload.policy || "-"}`;
    }

    if (type === "STATE_SNAPSHOT") {
      applySnapshot(payload);
    }

    if (type === "ACTION_BATCH") {
      const score = payload.score || 0;
      const actions = payload.actions || [];
      if (actions.length > 0) {
        const head = actions[0];
        logLine(`tick=${payload.tick} score=${Number(score).toFixed(2)} actions=${actions.length} head=${head.type}`);
      } else {
        logLine(`tick=${payload.tick} score=${Number(score).toFixed(2)} actions=0`);
      }
    }

    if (type === "ENGINE_EVENT") {
      const lvl = payload.level || "info";
      const message = payload.message || "";
      const reason = payload.reason ? ` reason=${payload.reason}` : "";
      logLine(`[${lvl}] ${message}${reason}`);
    }
  };
}

elStart.addEventListener("click", () => {
  if (!ws || ws.readyState !== 1) return;
  ws.send(JSON.stringify({ type: "UI_START", payload: {} }));
});

elStop.addEventListener("click", () => {
  if (!ws || ws.readyState !== 1) return;
  ws.send(JSON.stringify({ type: "UI_STOP", payload: {} }));
});

elReset.addEventListener("click", () => {
  if (!ws || ws.readyState !== 1) return;
  ws.send(JSON.stringify({ type: "RESET", payload: {} }));
  logLine("RESET 요청");
});

function resize() {
  const w = window.innerWidth;
  const h = window.innerHeight;
  renderer.setSize(w, h, false);
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
}
window.addEventListener("resize", resize);
resize();

function animate() {
  requestAnimationFrame(animate);
  controls.update();
  renderer.render(scene, camera);
}
animate();

connectWS();
