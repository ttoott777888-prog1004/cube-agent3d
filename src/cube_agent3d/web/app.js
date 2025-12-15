import * as THREE from "https://unpkg.com/three@0.160.0/build/three.module.js";
import { OrbitControls } from "https://unpkg.com/three@0.160.0/examples/jsm/controls/OrbitControls.js";

const elCanvas = document.getElementById("canvas");
const elStart = document.getElementById("btnStart");
const elStop = document.getElementById("btnStop");
const elReset = document.getElementById("btnReset");
const elZoomReset = document.getElementById("btnZoomReset");
const elStatus = document.getElementById("status");
const elMeta = document.getElementById("meta");
const elLog = document.getElementById("log");

if (!elCanvas) throw new Error("Missing #canvas");

const LOG_MAX_LINES = 220;
const logBuf = [];

function logLine(s) {
  if (!elLog) return;
  const t = new Date().toLocaleTimeString();
  logBuf.unshift(`[${t}] ${s}`);
  if (logBuf.length > LOG_MAX_LINES) logBuf.length = LOG_MAX_LINES;
  elLog.innerText = logBuf.join("\n");
}

function setStatusText(s) {
  if (elStatus) elStatus.innerText = s;
}
function setMetaText(s) {
  if (elMeta) elMeta.innerText = s;
}

function parseColor(c) {
  try {
    if (Array.isArray(c) && c.length >= 3) {
      const r = c[0] > 1 ? c[0] / 255 : c[0];
      const g = c[1] > 1 ? c[1] / 255 : c[1];
      const b = c[2] > 1 ? c[2] / 255 : c[2];
      return new THREE.Color(r, g, b);
    }
    if (typeof c === "string" && c.trim().length > 0) return new THREE.Color(c);
  } catch {}
  return new THREE.Color("#7dd3fc");
}

let ws = null;
let reconnectTimer = null;
let reconnectDelayMs = 600;

// --- Three.js
const renderer = new THREE.WebGLRenderer({ canvas: elCanvas, antialias: true, alpha: false });
renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));

const scene = new THREE.Scene();
scene.background = new THREE.Color(0x0b0f17);

const camera = new THREE.PerspectiveCamera(60, 1, 0.05, 500);
camera.position.set(8, 8, 10);

const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;
controls.dampingFactor = 0.08;
controls.minDistance = 3;
controls.maxDistance = 60;
controls.target.set(0, 4, 0);

if (elZoomReset) {
  elZoomReset.addEventListener("click", () => {
    camera.position.set(8, 8, 10);
    controls.target.set(0, 4, 0);
    controls.update();
  });
}

scene.add(new THREE.AmbientLight(0xffffff, 0.55));
const dir = new THREE.DirectionalLight(0xffffff, 1.0);
dir.position.set(10, 20, 10);
scene.add(dir);

const grid = new THREE.GridHelper(40, 40, 0x223044, 0x121a27);
grid.position.y = 0;
scene.add(grid);

const axes = new THREE.AxesHelper(3);
axes.position.set(0, 0.01, 0);
scene.add(axes);

// Instanced cubes
const MAX_INST = 512;
const geom = new THREE.BoxGeometry(1, 1, 1);
const mat = new THREE.MeshStandardMaterial({ vertexColors: true, roughness: 0.65, metalness: 0.1 });

const inst = new THREE.InstancedMesh(geom, mat, MAX_INST);
inst.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
inst.instanceColor = new THREE.InstancedBufferAttribute(new Float32Array(MAX_INST * 3), 3);
inst.instanceColor.setUsage(THREE.DynamicDrawUsage);
scene.add(inst);

const _m = new THREE.Matrix4();
const _p = new THREE.Vector3();
const _q = new THREE.Quaternion();
const _s = new THREE.Vector3();

function applySnapshot(payload) {
  const cubes = payload?.cubes || [];
  const count = Math.min(cubes.length, MAX_INST);

  inst.count = count;

  for (let i = 0; i < count; i++) {
    const c = cubes[i] || {};
    const pos = c.pos || [0, 0, 0];
    const rot = c.rot || [0, 0, 0, 1];
    const sca = c.scale || [1, 1, 1];

    _p.set(pos[0] || 0, pos[1] || 0, pos[2] || 0);
    _q.set(rot[0] || 0, rot[1] || 0, rot[2] || 0, rot[3] ?? 1);
    _s.set(sca[0] || 1, sca[1] || 1, sca[2] || 1);

    _m.compose(_p, _q, _s);
    inst.setMatrixAt(i, _m);
    inst.setColorAt(i, parseColor(c.color));
  }

  inst.instanceMatrix.needsUpdate = true;
  if (inst.instanceColor) inst.instanceColor.needsUpdate = true;
}

function setButtonsEnabled(connected) {
  if (elStart) elStart.disabled = !connected;
  if (elStop) elStop.disabled = !connected;
  if (elReset) elReset.disabled = !connected;
  if (elZoomReset) elZoomReset.disabled = !connected;
}

function scheduleReconnect() {
  if (reconnectTimer) return;
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    reconnectDelayMs = Math.min(Math.floor(reconnectDelayMs * 1.4), 5000);
    connectWS();
  }, reconnectDelayMs);
}

function connectWS() {
  if (reconnectTimer) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }

  const proto = location.protocol === "https:" ? "wss" : "ws";
  const url = `${proto}://${location.host}/ws`;

  try {
    ws = new WebSocket(url);
  } catch (e) {
    setStatusText("오류");
    logLine(`WebSocket 생성 실패: ${String(e)}`);
    scheduleReconnect();
    return;
  }

  ws.onopen = () => {
    reconnectDelayMs = 600;
    setButtonsEnabled(true);
    setStatusText("연결됨");
    logLine("WebSocket 연결됨");
    ws.send(JSON.stringify({ type: "HELLO", payload: { client: "web" } }));
  };

  ws.onclose = () => {
    setButtonsEnabled(false);
    setStatusText("연결 끊김");
    logLine("WebSocket 연결 끊김");
    scheduleReconnect();
  };

  ws.onerror = () => {
    setStatusText("오류");
  };

  ws.onmessage = (ev) => {
    let msg;
    try {
      msg = JSON.parse(ev.data);
    } catch {
      return;
    }

    const type = msg?.type;
    const payload = msg?.payload || {};

    if (type === "SERVER_STATUS") {
      const run = payload.running ? "RUN" : "STOP";
      setStatusText(`${run} | tick=${payload.tick} | cubes=${payload.cube_count}`);
      setMetaText(`session=${payload.session_id} | tick_hz=${payload.tick_hz} | max_cubes=${payload.max_cubes}`);
      return;
    }

    if (type === "STATE_SNAPSHOT") {
      applySnapshot(payload);
      return;
    }

    if (type === "ACTION_BATCH") {
      const score = payload.score ?? 0;
      const actions = payload.actions || [];
      if (actions.length > 0) {
        logLine(`tick=${payload.tick} score=${Number(score).toFixed(2)} actions=${actions.length} head=${actions[0].type}`);
      } else {
        logLine(`tick=${payload.tick} score=${Number(score).toFixed(2)} actions=0`);
      }
    }
  };
}

if (elStart) {
  elStart.addEventListener("click", () => {
    if (!ws || ws.readyState !== 1) return;
    ws.send(JSON.stringify({ type: "UI_START", payload: {} }));
  });
}
if (elStop) {
  elStop.addEventListener("click", () => {
    if (!ws || ws.readyState !== 1) return;
    ws.send(JSON.stringify({ type: "UI_STOP", payload: {} }));
  });
}
if (elReset) {
  elReset.addEventListener("click", () => {
    if (!ws || ws.readyState !== 1) return;
    ws.send(JSON.stringify({ type: "RESET", payload: {} }));
    logLine("RESET 요청");
  });
}

function resize() {
  const rect = elCanvas.getBoundingClientRect();
  const w = Math.max(1, Math.floor(rect.width));
  const h = Math.max(1, Math.floor(rect.height));
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

setButtonsEnabled(false);
connectWS();
